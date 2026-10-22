from __future__ import annotations

import json
import os
import stat
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from google_tasks_mcp import cli
from google_tasks_mcp.config import Settings
from google_tasks_mcp.errors import AuthFlowError, NotConfigured, NotConnected, ReauthRequired
from google_tasks_mcp.oauth import TASKS_SCOPE, GoogleAuth, iso

from .fake_google import CLIENT_ID, FakeGoogle

pytestmark = pytest.mark.anyio


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def test_authorization_url_requests_only_tasks_scope_with_pkce(auth: GoogleAuth) -> None:
    request = auth.start_authorization()
    params = query(request.url)
    assert request.url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert params["scope"] == TASKS_SCOPE
    assert params["client_id"] == CLIENT_ID
    assert params["code_challenge_method"] == "S256"
    assert len(params["code_challenge"]) == 43
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["redirect_uri"] == "http://127.0.0.1:8765/oauth/google/callback"
    assert len(params["state"]) >= 40
    # The pending record holds a hash of the state, never the state itself.
    pending = list(auth.settings.pending_dir.iterdir())
    assert len(pending) == 1
    assert params["state"] not in pending[0].read_text()


async def test_completing_the_flow_stores_private_credentials(auth: GoogleAuth, google: FakeGoogle) -> None:
    request = auth.start_authorization()
    token = await auth.complete_from_redirect(google.consent(request.url))
    path = auth.settings.token_file
    assert path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    status = auth.status()
    assert status.state == "connected" and status.connected
    assert status.scopes == [TASKS_SCOPE]
    dumped = status.model_dump_json()
    assert token.access_token not in dumped and token.refresh_token not in dumped
    assert token.access_token not in repr(token) and token.refresh_token not in repr(token)
    assert list(auth.settings.pending_dir.iterdir()) == []


async def test_state_is_single_use(auth: GoogleAuth, google: FakeGoogle) -> None:
    redirect = google.consent(auth.start_authorization().url)
    await auth.complete_from_redirect(redirect)
    with pytest.raises(AuthFlowError) as err:
        await auth.complete_from_redirect(redirect)
    assert err.value.code == "invalid_state"


async def test_forged_state_is_rejected_without_contacting_google(
    auth: GoogleAuth, google: FakeGoogle
) -> None:
    redirect = google.consent(auth.start_authorization().url)
    forged = redirect.replace("state=", "state=x")
    with pytest.raises(AuthFlowError) as err:
        await auth.complete_from_redirect(forged)
    assert err.value.code == "invalid_state"
    assert not any(r.url.path == "/token" for r in google.requests)
    assert auth.status().state == "disconnected"


async def test_expired_state_is_rejected(settings: Settings, http, google: FakeGoogle) -> None:
    now = [1_000_000.0]
    auth = GoogleAuth(settings, http, clock=lambda: now[0])
    redirect = google.consent(auth.start_authorization().url)
    now[0] += 601
    with pytest.raises(AuthFlowError) as err:
        await auth.complete_from_redirect(redirect)
    assert err.value.code == "expired_state"


async def test_access_denied_is_reported_and_consumes_state(auth: GoogleAuth) -> None:
    state = query(auth.start_authorization().url)["state"]
    with pytest.raises(AuthFlowError) as err:
        await auth.complete_from_params({"error": "access_denied", "state": state})
    assert err.value.code == "access_denied"
    assert list(auth.settings.pending_dir.iterdir()) == []


async def test_missing_tasks_scope_is_refused(auth: GoogleAuth, google: FakeGoogle) -> None:
    redirect = google.consent(auth.start_authorization().url, scope="openid")
    with pytest.raises(AuthFlowError) as err:
        await auth.complete_from_redirect(redirect)
    assert err.value.code == "scope_not_granted"
    assert not auth.settings.token_file.exists()
    assert google.revoked  # The unusable grant is cleaned up.


async def test_access_token_refreshes_when_expired(settings: Settings, http, google: FakeGoogle) -> None:
    now = [1_000_000.0]
    auth = GoogleAuth(settings, http, clock=lambda: now[0])
    await auth.complete_from_redirect(google.consent(auth.start_authorization().url))
    first = await auth.access_token()
    assert await auth.access_token() == first  # Still fresh: no refresh.
    now[0] += 3600
    second = await auth.access_token()
    assert second != first
    stored = json.loads(settings.token_file.read_text())
    assert stored["access_token"] == second
    assert auth.status().last_refreshed_at is not None


async def test_revoked_refresh_token_requires_reauth(connected: GoogleAuth, google: FakeGoogle) -> None:
    google.revoke_all()
    with pytest.raises(ReauthRequired):
        await connected.access_token(force_refresh=True)
    status = connected.status()
    assert status.state == "reauth_required" and not status.connected
    with pytest.raises(ReauthRequired):
        await connected.access_token()


async def test_token_from_another_client_requires_reauth(connected: GoogleAuth) -> None:
    data = json.loads(connected.settings.token_file.read_text())
    data["client_id"] = "someone-else.apps.googleusercontent.com"
    connected.settings.token_file.write_text(json.dumps(data))
    assert connected.status().state == "reauth_required"


async def test_testing_mode_expiry_is_surfaced(settings: Settings, http, google: FakeGoogle, caplog) -> None:
    google.refresh_token_expires_in = 604799
    now = [1_000_000.0]
    auth = GoogleAuth(settings, http, clock=lambda: now[0])
    await auth.complete_from_redirect(google.consent(auth.start_authorization().url))
    assert auth.status().authorization_expires_at is not None
    assert "Testing mode" in caplog.text and iso(1_000_000 + 604799) in caplog.text
    now[0] += 604800
    assert auth.status().state == "reauth_required"


async def test_published_app_authorization_has_no_expiry(connected: GoogleAuth, caplog) -> None:
    assert connected.status().authorization_expires_at is None
    assert "Testing mode" not in caplog.text


async def test_cli_auth_warns_when_the_authorization_is_time_limited(
    settings: Settings, http, google: FakeGoogle, monkeypatch, capsys
) -> None:
    google.refresh_token_expires_in = 604799
    monkeypatch.setattr(cli, "new_http_client", lambda: httpx2.AsyncClient(transport=google.transport()))
    redirect = google.consent(GoogleAuth(settings, http).start_authorization().url)
    assert await cli.cmd_auth(settings, redirect, timeout=1) == 0
    out = capsys.readouterr()
    assert "Google Tasks connected." in out.out
    assert "Testing mode" in out.err and "Publish the app" in out.err


async def test_disconnect_revokes_and_deletes(connected: GoogleAuth, google: FakeGoogle) -> None:
    refresh = json.loads(connected.settings.token_file.read_text())["refresh_token"]
    result = await connected.disconnect()
    assert result.disconnected and result.revoked
    assert google.revoked == [refresh]
    assert not connected.settings.token_file.exists()
    assert connected.status().state == "disconnected"
    with pytest.raises(NotConnected):
        await connected.access_token()


async def test_disconnect_deletes_locally_even_if_google_is_unreachable(
    connected: GoogleAuth, google: FakeGoogle
) -> None:
    google.revoke_unreachable = True
    result = await connected.disconnect()
    assert result.disconnected and not result.revoked
    assert "myaccount.google.com" in result.message
    assert not connected.settings.token_file.exists()


async def test_not_configured(tmp_path, http) -> None:
    auth = GoogleAuth(Settings(config_dir=tmp_path / "none", state_dir=tmp_path / "state"), http)
    assert auth.status().state == "not_configured"
    with pytest.raises(NotConfigured):
        auth.start_authorization()
    with pytest.raises(NotConfigured):
        await auth.access_token()


async def test_client_from_environment(tmp_path, http, google: FakeGoogle) -> None:
    from .fake_google import CLIENT_SECRET

    settings = Settings(
        config_dir=tmp_path / "none",
        state_dir=tmp_path / "state",
        google_client_id=CLIENT_ID,
        google_client_secret=CLIENT_SECRET,
    )
    assert CLIENT_SECRET not in repr(settings)
    auth = GoogleAuth(settings, http)
    await auth.complete_from_redirect(google.consent(auth.start_authorization().url))
    assert auth.status().client_type == "environment"


async def test_refresh_does_not_overwrite_a_concurrent_reconnect(connected: GoogleAuth) -> None:
    original = connected._token_request

    async def racing(form: dict[str, str]):
        data = await original(form)
        if form["grant_type"] == "refresh_token":  # Meanwhile, the CLI reconnects.
            other = connected.tokens.load()
            other.refresh_token, other.access_token = "1//from-cli", "ya29.from-cli"
            other.connected_at = "2030-01-01T00:00:00Z"
            connected.tokens.save(other)
        return data

    connected._token_request = racing
    assert await connected.access_token(force_refresh=True) == "ya29.from-cli"
    assert connected.tokens.load().refresh_token == "1//from-cli"


async def test_refresh_does_not_resurrect_a_concurrent_disconnect(connected: GoogleAuth) -> None:
    original = connected._token_request

    async def racing(form: dict[str, str]):
        data = await original(form)
        connected.tokens.delete()  # Meanwhile, `google-tasks-mcp disconnect` ran.
        return data

    connected._token_request = racing
    with pytest.raises(NotConnected):
        await connected.access_token(force_refresh=True)
    assert not connected.settings.token_file.exists()
