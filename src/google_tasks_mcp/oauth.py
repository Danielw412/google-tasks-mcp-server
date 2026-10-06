"""Server-side Google OAuth 2.0 for the Tasks API.

Flow: authorization code + PKCE (S256), ``access_type=offline``, a single-use,
ten-minute ``state``, and only the ``tasks`` scope.

* ``start_authorization`` records the pending request (a hash of ``state`` and
  the PKCE verifier) as one small 0600 file. Whichever process receives the
  redirect can then finish it: the server's callback route, or
  ``google-tasks-mcp auth`` when the user pastes the redirect URL.
* Credentials live in one 0600 JSON file in the state directory. They never
  leave this module except as an ``Authorization`` header to Google.
* Access tokens refresh automatically; a refresh token Google rejects marks the
  connection as needing reauthorization instead of failing obscurely.
* Optional fallback (``GTASKS_FALLBACK_TOKEN_FILE``): when this server's own
  authorization is missing or unusable, calls borrow another app's google-auth
  ``token.json`` (Canvas Task Sync's, say). That file is only ever read: access
  tokens refreshed from it stay in memory. It is used only for the Google account
  the native authorization belonged to, checked by comparing default task lists.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

from .config import Settings
from .errors import (
    AuthFlowError,
    GoogleApiError,
    GoogleUnavailable,
    NotConfigured,
    NotConnected,
    ReauthRequired,
)
from .models import AuthNotice, AuthStatus, DisconnectResult

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint URL, not a secret
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_PERMISSIONS_URL = "https://myaccount.google.com/connections"
TASKS_DEFAULT_LIST_URL = "https://tasks.googleapis.com/tasks/v1/users/@me/lists/@default"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
SCOPES: tuple[str, ...] = (TASKS_SCOPE,)
PENDING_TTL_SECONDS = 600
REFRESH_MARGIN_SECONDS = 120


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# Private file helpers ------------------------------------------------------------


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write ``data`` to ``path`` with owner-only permissions."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# OAuth client configuration --------------------------------------------------------


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret: str = field(repr=False)
    kind: str  # "installed" (Desktop app), "web", or "environment"
    redirect_uris: tuple[str, ...]
    source: str


def load_client_config(settings: Settings) -> ClientConfig:
    if settings.google_client_id:
        if not settings.google_client_secret:
            raise NotConfigured("GTASKS_GOOGLE_CLIENT_ID is set but GTASKS_GOOGLE_CLIENT_SECRET is not.")
        return ClientConfig(
            settings.google_client_id, settings.google_client_secret, "environment", (), "environment"
        )
    path = settings.client_file
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NotConfigured(
            f"No Google OAuth client is configured (expected the downloaded client JSON at {path})."
        ) from None
    except (OSError, ValueError) as exc:
        raise NotConfigured(f"The Google OAuth client file {path} could not be read: {exc}") from None
    kind = next((k for k in ("installed", "web") if isinstance(raw, dict) and k in raw), None)
    if kind is None:
        raise NotConfigured(
            f"{path} is not a Google OAuth client file (expected an 'installed' or 'web' section). "
            "Create a 'Desktop app' OAuth client and download its JSON."
        )
    section = raw[kind]
    client_id, client_secret = section.get("client_id"), section.get("client_secret")
    if not client_id or not client_secret:
        raise NotConfigured(f"{path} is missing client_id or client_secret.")
    return ClientConfig(client_id, client_secret, kind, tuple(section.get("redirect_uris") or ()), str(path))


# Stored credentials -----------------------------------------------------------------


@dataclass
class StoredToken:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    scopes: list[str]
    client_id: str
    connected_at: str
    refreshed_at: str | None = None
    refresh_token_expires_at: float | None = None
    invalid_reason: str | None = None
    version: int = 1


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> StoredToken | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            known = StoredToken.__dataclass_fields__
            return StoredToken(**{k: v for k, v in data.items() if k in known})
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            logger.warning("Ignoring unreadable Google token file %s; reconnect to replace it", self.path)
            return None

    def save(self, token: StoredToken) -> None:
        write_private_json(self.path, asdict(token))

    def delete(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


class PendingStore:
    """Pending authorizations, one file per request, named by the SHA-256 of ``state``.

    Claiming renames the file before reading it, so a state can be used once even
    when two processes race for it.
    """

    def __init__(self, directory: Path, ttl: float, clock: Callable[[], float]) -> None:
        self.directory = directory
        self.ttl = ttl
        self.clock = clock

    @staticmethod
    def _digest(state: str) -> str:
        return hashlib.sha256(state.encode()).hexdigest()

    def create(self, state: str, verifier: str, redirect_uri: str) -> float:
        self.purge_expired()
        now = self.clock()
        expires_at = now + self.ttl
        digest = self._digest(state)
        write_private_json(
            self.directory / f"{digest}.json",
            {
                "state_sha256": digest,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "created_at": now,
                "expires_at": expires_at,
            },
        )
        return expires_at

    def claim(self, state: str | None) -> dict[str, Any]:
        if not state or len(state) > 512:
            raise AuthFlowError("The sign-in response did not include a valid state.", code="invalid_state")
        digest = self._digest(state)
        path = self.directory / f"{digest}.json"
        claimed = path.with_name(f"{path.name}.claimed-{secrets.token_hex(4)}")
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            raise AuthFlowError(
                "This sign-in link is unknown or was already used.", code="invalid_state"
            ) from None
        try:
            data = json.loads(claimed.read_text(encoding="utf-8"))
        finally:
            claimed.unlink(missing_ok=True)
        if not secrets.compare_digest(str(data.get("state_sha256", "")), digest):
            raise AuthFlowError("The sign-in state did not match.", code="invalid_state")
        if float(data.get("expires_at", 0)) < self.clock():
            raise AuthFlowError("This sign-in link expired (links last 10 minutes).", code="expired_state")
        return data

    def purge_expired(self) -> None:
        if not self.directory.is_dir():
            return
        now = self.clock()
        for path in self.directory.glob("*.json"):
            try:
                if float(json.loads(path.read_text(encoding="utf-8")).get("expires_at", 0)) < now:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                path.unlink(missing_ok=True)

    def clear(self) -> None:
        if self.directory.is_dir():
            for path in self.directory.iterdir():
                path.unlink(missing_ok=True)


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    redirect_uri: str
    expires_at: float


@dataclass(frozen=True)
class AccessGrant:
    token: str = field(repr=False)
    source: Literal["native", "fallback"]


@dataclass(frozen=True)
class FallbackToken:
    """The fields used from a google-auth authorized-user ``token.json``."""

    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.client_id}|{self.refresh_token}".encode()).hexdigest()


class FallbackUnavailable(Exception):
    """The fallback authorization cannot be used; the message says why (never a secret)."""


def _parse_expiry(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:  # google-auth stores naive UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def load_fallback_token(path: Path) -> FallbackToken:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FallbackUnavailable(f"{path} does not exist") from None
    except (OSError, ValueError) as exc:
        raise FallbackUnavailable(f"{path} could not be read ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise FallbackUnavailable(f"{path} is not a Google token file")
    client_id, client_secret, refresh_token = (
        data.get("client_id"),
        data.get("client_secret"),
        data.get("refresh_token"),
    )
    if not (isinstance(client_id, str) and isinstance(client_secret, str) and isinstance(refresh_token, str)):
        raise FallbackUnavailable(f"{path} has no client_id, client_secret, and refresh_token")
    if TASKS_SCOPE not in (data.get("scopes") or []):
        raise FallbackUnavailable(f"{path} does not include Google Tasks access")
    token = data.get("token")
    return FallbackToken(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        access_token=token if isinstance(token, str) and token else None,
        expires_at=_parse_expiry(data.get("expiry")),
    )


class _TokenEndpointError(Exception):
    def __init__(self, status: int, error: str, description: str) -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.status = status
        self.error = error
        self.description = description


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class GoogleAuth:
    def __init__(
        self, settings: Settings, http: httpx2.AsyncClient, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.settings = settings
        self._http = http
        self._clock = clock
        self.tokens = TokenStore(settings.token_file)
        self.pending = PendingStore(settings.pending_dir, PENDING_TTL_SECONDS, clock)
        self._lock = asyncio.Lock()
        # Fallback state, all keyed by FallbackToken.key so a new fallback token starts clean.
        self._fallback_cache: tuple[str, str, float] | None = None  # key, access token, expiry
        self._fallback_failure: tuple[str, str] | None = None  # key, reason
        self._fallback_verified: str | None = None  # key|default list id
        self._using_fallback = False

    def client_config(self) -> ClientConfig:
        return load_client_config(self.settings)

    def account_marker(self) -> str | None:
        """Changes whenever a (re)connection replaces the stored authorization."""
        token = self.tokens.load()
        if token is None:
            return None
        return hashlib.sha256(
            f"{token.client_id}|{token.connected_at}|{token.refresh_token}".encode()
        ).hexdigest()

    def _replaced(self, token: StoredToken) -> StoredToken | None:
        """The stored token if another process (the CLI) reconnected meanwhile; raises if it disconnected."""
        current = self.tokens.load()
        if current is None:
            raise NotConnected("Google was disconnected.")
        if current.refresh_token != token.refresh_token or current.connected_at != token.connected_at:
            return current
        return None

    # Status -------------------------------------------------------------------

    def _token_problem(self, token: StoredToken, client: ClientConfig) -> str | None:
        if token.invalid_reason:
            return token.invalid_reason
        if token.client_id != client.client_id:
            return "The saved Google authorization belongs to a different OAuth client."
        if TASKS_SCOPE not in token.scopes:
            return "The saved Google authorization does not include Google Tasks access."
        if token.refresh_token_expires_at and token.refresh_token_expires_at < self._clock():
            return (
                "The saved Google authorization expired. Consent screens in 'Testing' mode issue "
                "refresh tokens that last 7 days; publish the app to stop this."
            )
        return None

    def status(self) -> AuthStatus:
        native = self._native_status()
        if native.connected or self.settings.fallback_token_file is None:
            return native
        name = self.settings.fallback_name
        problem = self._fallback_problem()
        if problem:
            return native.model_copy(
                update={
                    "message": f"{native.message} The fallback Google authorization from {name} "
                    f"is unavailable: {problem}."
                }
            )
        return native.model_copy(
            update={
                "connected": True,
                "fallback_active": True,
                "message": f"This server's own Google sign-in is not usable ({native.message}) "
                f"Google Tasks calls use the Google authorization from {name} until you reconnect.",
                "action": ReauthRequired.default_action,
            }
        )

    def notice(self) -> AuthNotice | None:
        """What to tell the user while the fallback is in use, or None."""
        status = self.status()
        if not status.fallback_active:
            return None
        native = self._native_status()
        return AuthNotice(
            message=(
                "The Google Tasks connector's own Google sign-in has expired or is unavailable "
                f"({native.message.rstrip('.')}). This request used the Google authorization from "
                f"{self.settings.fallback_name} instead. Reconnect so the connector stops depending on it."
            ),
            instructions=(
                "Do not stop or ask the user about this now. First finish everything the user asked "
                "for, including any remaining tool calls. Then, at the end of your reply, tell the user "
                "once that the Google Tasks connector's sign-in needs renewing, call auth_connect, and "
                "show them the sign-in link with its instructions."
            ),
        )

    def _native_status(self) -> AuthStatus:
        redirect_uri = self.settings.redirect_uri
        try:
            client = self.client_config()
        except NotConfigured as exc:
            return AuthStatus(
                state="not_configured",
                configured=False,
                connected=False,
                message=exc.message,
                action=exc.action,
                redirect_uri=redirect_uri,
            )
        token = self.tokens.load()
        common: dict[str, Any] = {
            "configured": True,
            "redirect_uri": redirect_uri,
            "client_type": client.kind,
        }
        if token is None:
            return AuthStatus(
                state="disconnected",
                connected=False,
                message="No Google account is connected.",
                action=NotConnected.default_action,
                **common,
            )
        details: dict[str, Any] = {
            "scopes": token.scopes,
            "connected_at": token.connected_at,
            "last_refreshed_at": token.refreshed_at,
            "authorization_expires_at": iso(token.refresh_token_expires_at)
            if token.refresh_token_expires_at
            else None,
        }
        problem = self._token_problem(token, client)
        if problem:
            return AuthStatus(
                state="reauth_required",
                connected=False,
                message=problem,
                action=ReauthRequired.default_action,
                **common,
                **details,
            )
        return AuthStatus(
            state="connected", connected=True, message="Google Tasks is connected.", **common, **details
        )

    # Authorization flow ---------------------------------------------------------

    def start_authorization(self) -> AuthorizationRequest:
        client = self.client_config()
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        redirect_uri = self.settings.redirect_uri
        expires_at = self.pending.create(state, verifier, redirect_uri)
        query = urlencode(
            {
                "client_id": client.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                # Always show consent so Google issues a fresh refresh token on reconnect.
                "prompt": "consent",
            }
        )
        return AuthorizationRequest(f"{GOOGLE_AUTH_URL}?{query}", redirect_uri, expires_at)

    async def complete(self, state: str | None, code: str | None) -> StoredToken:
        pending = self.pending.claim(state)
        if not code:
            raise AuthFlowError("The sign-in response did not include an authorization code.")
        client = self.client_config()
        try:
            data = await self._token_request(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": pending["redirect_uri"],
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                    "code_verifier": pending["code_verifier"],
                }
            )
        except _TokenEndpointError as exc:
            raise AuthFlowError(f"Google rejected the sign-in ({exc}).") from None
        granted = set(str(data.get("scope", "")).split())
        if TASKS_SCOPE not in granted:
            await self._revoke(str(data.get("access_token", "")))
            raise AuthFlowError(
                "Google Tasks access was not granted. Start again and allow access to Google Tasks.",
                code="scope_not_granted",
            )
        refresh_token = data.get("refresh_token")
        if not refresh_token or not data.get("access_token"):
            raise AuthFlowError(
                "Google did not return a refresh token. Remove this app at "
                f"{GOOGLE_PERMISSIONS_URL} and connect again."
            )
        now = self._clock()
        refresh_ttl = data.get("refresh_token_expires_in")
        token = StoredToken(
            access_token=data["access_token"],
            refresh_token=refresh_token,
            expires_at=now + float(data.get("expires_in", 3600)),
            scopes=sorted(granted),
            client_id=client.client_id,
            connected_at=iso(now),
            refresh_token_expires_at=now + float(refresh_ttl) if refresh_ttl else None,
        )
        async with self._lock:
            self.tokens.save(token)
        logger.info("Google account connected (scopes: %s)", " ".join(token.scopes))
        return token

    async def complete_from_redirect(self, redirect: str) -> StoredToken:
        """Finish the flow from a redirect URL (or its query string) pasted by the user."""
        text = redirect.strip()
        query = urlsplit(text).query if "://" in text else text.lstrip("?")
        params = {k: v[0] for k, v in parse_qs(query).items() if v}
        return await self.complete_from_params(params)

    async def complete_from_params(self, params: dict[str, str]) -> StoredToken:
        if "error" in params:
            # Consume the state so the link cannot be replayed, then report Google's answer.
            with contextlib.suppress(AuthFlowError):
                self.pending.claim(params.get("state"))
            if params["error"] == "access_denied":
                raise AuthFlowError(
                    "Google sign-in was cancelled or access was denied.", code="access_denied"
                )
            raise AuthFlowError(f"Google returned an error: {params['error'][:100]}")
        return await self.complete(params.get("state"), params.get("code"))

    # Access tokens --------------------------------------------------------------------

    async def access_token(self, *, force_refresh: bool = False) -> str:
        return (await self.grant(force_refresh=force_refresh)).token

    async def grant(self, *, force_refresh: bool = False) -> AccessGrant:
        """An access token from this server's own authorization, else from the fallback."""
        async with self._lock:
            try:
                token = await self._native_access_token(force_refresh)
            except (NotConfigured, NotConnected, ReauthRequired) as exc:
                fallback_file = self.settings.fallback_token_file
                if fallback_file is None:
                    raise
                try:
                    access = await self._fallback_access_token(fallback_file, force_refresh)
                except FallbackUnavailable as fallback:
                    raise type(exc)(
                        f"{exc.message} The fallback Google authorization from {self.settings.fallback_name} "
                        "is unavailable too: "
                        f"{fallback}.",
                        code=exc.code,
                    ) from None
                if not self._using_fallback:
                    logger.warning(
                        "Own Google authorization unusable (%s); using the fallback from %s",
                        exc.code,
                        self.settings.fallback_name,
                    )
                    self._using_fallback = True
                return AccessGrant(access, "fallback")
            if self._using_fallback:
                logger.info("Own Google authorization works again; fallback no longer used")
                self._using_fallback = False
            return AccessGrant(token, "native")

    async def _native_access_token(self, force_refresh: bool) -> str:
        client = self.client_config()
        token = self.tokens.load()
        if token is None:
            raise NotConnected("No Google account is connected.")
        problem = self._token_problem(token, client)
        if problem:
            raise ReauthRequired(problem)
        if force_refresh or token.expires_at - REFRESH_MARGIN_SECONDS <= self._clock():
            token = await self._refresh(token, client)
        if self.settings.fallback_token_file is not None:
            await self._remember_account(token)
        return token.access_token

    # Fallback authorization ----------------------------------------------------------

    def _fallback_problem(self) -> str | None:
        """Why the fallback cannot be used, judged without contacting Google."""
        path = self.settings.fallback_token_file
        if path is None:
            return "not configured"
        try:
            fallback = load_fallback_token(path)
        except FallbackUnavailable as exc:
            return str(exc)
        if self._fallback_failure and self._fallback_failure[0] == fallback.key:
            return self._fallback_failure[1]
        return None

    async def _fallback_access_token(self, path: Path, force_refresh: bool) -> str:
        fallback = load_fallback_token(path)
        key = fallback.key
        if self._fallback_failure and self._fallback_failure[0] == key:
            raise FallbackUnavailable(self._fallback_failure[1])
        now = self._clock()
        cached = self._fallback_cache
        if not force_refresh and cached and cached[0] == key and cached[2] - REFRESH_MARGIN_SECONDS > now:
            access = cached[1]
        elif (
            not force_refresh
            and fallback.access_token
            and fallback.expires_at
            and fallback.expires_at - REFRESH_MARGIN_SECONDS > now
        ):
            access = fallback.access_token  # The other app refreshed it recently.
        else:
            access = await self._refresh_fallback(fallback)
        await self._verify_fallback_account(key, access)
        return access

    async def _refresh_fallback(self, fallback: FallbackToken) -> str:
        """Refresh in memory only: the other app owns its token file."""
        try:
            data = await self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": fallback.refresh_token,
                    "client_id": fallback.client_id,
                    "client_secret": fallback.client_secret,
                }
            )
        except _TokenEndpointError as exc:
            if exc.error in ("invalid_grant", "invalid_client", "unauthorized_client"):
                reason = f"Google rejected it ({exc.error})"
                self._fallback_failure = (fallback.key, reason)
                raise FallbackUnavailable(reason) from None
            raise FallbackUnavailable(f"refreshing it failed ({exc.error})") from None
        if data.get("scope") and TASKS_SCOPE not in str(data["scope"]).split():
            reason = "it no longer includes Google Tasks access"
            self._fallback_failure = (fallback.key, reason)
            raise FallbackUnavailable(reason)
        access = str(data["access_token"])
        self._fallback_cache = (fallback.key, access, self._clock() + float(data.get("expires_in", 3600)))
        logger.info(
            "Refreshed the fallback access token from %s (kept in memory)", self.settings.fallback_name
        )
        return access

    async def _default_list_id(self, access_token: str) -> str:
        """The default task list id, which identifies a Google account."""
        try:
            response = await self._http.get(
                TASKS_DEFAULT_LIST_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
        except httpx2.HTTPError as exc:
            raise GoogleUnavailable(f"Could not reach Google Tasks ({type(exc).__name__}).") from None
        if response.status_code == 401:
            raise FallbackUnavailable("Google did not accept its access token")
        try:
            list_id = response.json().get("id") if response.status_code == 200 else None
        except ValueError:
            list_id = None
        if not isinstance(list_id, str) or not list_id:
            raise GoogleUnavailable(
                f"Google Tasks returned HTTP {response.status_code} for the default list."
            )
        return list_id

    async def _remember_account(self, token: StoredToken) -> None:
        """Record which account the native authorization belongs to, once per connection."""
        path = self.settings.account_file
        with contextlib.suppress(OSError, ValueError):
            if json.loads(path.read_text(encoding="utf-8")).get("connected_at") == token.connected_at:
                return
        try:
            list_id = await self._default_list_id(token.access_token)
        except (FallbackUnavailable, GoogleUnavailable):
            return  # Try again on the next call; the tool call itself goes ahead.
        write_private_json(path, {"connected_at": token.connected_at, "default_task_list_id": list_id})

    async def _verify_fallback_account(self, key: str, access_token: str) -> None:
        try:
            remembered = json.loads(self.settings.account_file.read_text(encoding="utf-8"))
            expected = str(remembered["default_task_list_id"])
        except (OSError, ValueError, KeyError, TypeError):
            # The native authorization never worked here, so there is no account to protect.
            return
        if self._fallback_verified == f"{key}|{expected}":
            return
        if await self._default_list_id(access_token) != expected:
            reason = "it belongs to a different Google account than this server's own sign-in"
            self._fallback_failure = (key, reason)
            raise FallbackUnavailable(reason)
        self._fallback_verified = f"{key}|{expected}"

    async def _refresh(self, token: StoredToken, client: ClientConfig) -> StoredToken:
        try:
            data = await self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                }
            )
        except _TokenEndpointError as exc:
            if exc.error == "invalid_grant":
                if (newer := self._replaced(token)) is not None and not newer.invalid_reason:
                    return newer
                token.invalid_reason = "Google rejected the saved authorization (it was revoked or expired)."
                self.tokens.save(token)
                logger.warning("Google refresh token rejected; reauthorization required")
                raise ReauthRequired(token.invalid_reason) from None
            if exc.error in ("invalid_client", "unauthorized_client"):
                raise NotConfigured(
                    "Google rejected the OAuth client credentials (the client was deleted or its secret "
                    "changed). Update the client JSON and reconnect."
                ) from None
            raise GoogleApiError(f"Refreshing the Google access token failed ({exc.error}).") from None
        if (newer := self._replaced(token)) is not None:
            return newer
        now = self._clock()
        token.access_token = data["access_token"]
        token.expires_at = now + float(data.get("expires_in", 3600))
        token.refreshed_at = iso(now)
        if data.get("refresh_token"):
            token.refresh_token = data["refresh_token"]
        if data.get("scope"):
            token.scopes = sorted(str(data["scope"]).split())
        if data.get("refresh_token_expires_in"):
            token.refresh_token_expires_at = now + float(data["refresh_token_expires_in"])
        self.tokens.save(token)
        logger.info("Refreshed Google access token")
        return token

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._http.post(GOOGLE_TOKEN_URL, data=form)
        except httpx2.HTTPError as exc:
            raise GoogleUnavailable(
                f"Could not reach Google's token endpoint ({type(exc).__name__})."
            ) from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200 or not isinstance(data, dict) or "access_token" not in data:
            error = str(data.get("error", f"http_{response.status_code}")) if isinstance(data, dict) else ""
            description = str(data.get("error_description", "")) if isinstance(data, dict) else ""
            raise _TokenEndpointError(response.status_code, error[:100], description[:200])
        return data

    # Disconnect -------------------------------------------------------------------------

    async def _revoke(self, token: str) -> tuple[bool, str | None]:
        if not token:
            return False, None
        try:
            response = await self._http.post(GOOGLE_REVOKE_URL, data={"token": token})
        except httpx2.HTTPError:
            return False, "Google could not be reached to revoke access."
        if response.status_code == 200:
            return True, None
        try:
            error = response.json().get("error")
        except ValueError:
            error = None
        if error == "invalid_token":
            return True, None  # Already revoked or expired.
        return False, f"Google refused the revocation (HTTP {response.status_code})."

    async def disconnect(self) -> DisconnectResult:
        async with self._lock:
            token = self.tokens.load()
            self.pending.clear()
            if token is None:
                removed = self.tokens.delete()  # Clears an unreadable file, if any.
                return DisconnectResult(
                    disconnected=removed, revoked=False, message="No Google account was connected."
                )
            revoked, problem = await self._revoke(token.refresh_token)
            self.tokens.delete()
        logger.info("Google account disconnected (revoked=%s)", revoked)
        if revoked:
            message = "Disconnected Google Tasks and revoked this server's access."
        else:
            message = (
                f"Deleted the stored Google credentials. {problem or ''} "
                f"Remove access manually at {GOOGLE_PERMISSIONS_URL} if needed."
            ).replace("  ", " ")
        if self.settings.fallback_token_file is not None:
            message += (
                f" Google Tasks calls will use the Google authorization from {self.settings.fallback_name} "
                "until you reconnect; remove GTASKS_FALLBACK_TOKEN_FILE to stop that."
            )
        return DisconnectResult(disconnected=True, revoked=revoked, message=message)
