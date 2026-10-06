"""Runtime configuration.

Each setting is read from the process environment first, then from the env file
(default ``~/.config/google-tasks-mcp/server.env``, the same file the systemd unit
loads), then falls back to a default. The CLI and the service therefore always
agree. Secrets never live in this module's output: ``Settings`` hides them from
``repr``.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_NAME = "google-tasks-mcp"
SERVER_NAME = "google-tasks"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MCP_PATH = "/mcp"
OAUTH_CALLBACK_PATH = "/oauth/google/callback"
DEFAULT_TUNNEL_PROFILE = "google-tasks-test"
DEFAULT_TUNNEL_CLIENT = "/usr/local/bin/tunnel-client"
MCP_SERVICE = "google-tasks-mcp.service"
TUNNEL_SERVICE = "google-tasks-tunnel.service"


class ConfigError(Exception):
    """The configuration is unusable (for example a non-loopback bind address)."""


def default_config_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if sys.platform == "win32":
        base = Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / APP_NAME


def read_env_file(path: Path) -> dict[str, str]:
    """Parse the KEY=VALUE subset shared by systemd ``EnvironmentFile`` and shells.

    Blank lines and ``#`` comments are skipped, an optional ``export`` prefix is
    allowed, and one layer of matching single or double quotes is removed.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    config_dir: Path = field(default_factory=default_config_dir)
    state_dir: Path = field(default_factory=default_state_dir)
    env_file: Path | None = None
    google_client_file: Path | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = field(default=None, repr=False)
    fallback_token_file: Path | None = None
    fallback_name: str = "another app"  # Messages say "the Google authorization from <name>".
    public_base_url: str | None = None
    timezone: str | None = None
    log_level: str = "INFO"
    tunnel_profile: str = DEFAULT_TUNNEL_PROFILE
    tunnel_client: str = DEFAULT_TUNNEL_CLIENT

    def __post_init__(self) -> None:
        if not is_loopback_host(self.host):
            raise ConfigError(
                f"GTASKS_HOST={self.host!r} is not a loopback address. The MCP server must stay on "
                "127.0.0.1; ChatGPT reaches it through the Secure MCP Tunnel."
            )
        if not 0 < self.port < 65536:
            raise ConfigError(f"GTASKS_PORT={self.port} is not a valid port")
        if self.public_base_url:
            parts = urlsplit(self.public_base_url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ConfigError("GTASKS_PUBLIC_BASE_URL must be an absolute http(s) URL")
            if parts.scheme == "http" and not is_loopback_host(parts.hostname):
                raise ConfigError(
                    "GTASKS_PUBLIC_BASE_URL must use https unless it points at a loopback address "
                    "(Google rejects plain-http redirect URIs on other hosts)"
                )
            if parts.query or parts.fragment:
                raise ConfigError("GTASKS_PUBLIC_BASE_URL must not contain a query or fragment")
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ConfigError(f"GTASKS_TIMEZONE={self.timezone!r} is not a known IANA timezone") from exc

    # Derived paths and URLs -------------------------------------------------

    @property
    def client_file(self) -> Path:
        return self.google_client_file or self.config_dir / "client_secret.json"

    @property
    def tunnel_env_file(self) -> Path:
        """Holds the tunnel's CONTROL_PLANE_API_KEY; loaded by google-tasks-tunnel.service."""
        return self.config_dir / "tunnel.env"

    @property
    def token_file(self) -> Path:
        return self.state_dir / "google-token.json"

    @property
    def account_file(self) -> Path:
        """Which Google account the native authorization belongs to (no secrets)."""
        return self.state_dir / "google-account.json"

    @property
    def pending_dir(self) -> Path:
        return self.state_dir / "pending-auth"

    @property
    def local_base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def mcp_url(self) -> str:
        return self.local_base_url + MCP_PATH

    @property
    def browser_base_url(self) -> str:
        """Base URL a browser uses to reach the OAuth callback."""
        return (self.public_base_url or self.local_base_url).rstrip("/")

    @property
    def redirect_uri(self) -> str:
        return self.browser_base_url + OAUTH_CALLBACK_PATH

    @property
    def redirect_is_loopback(self) -> bool:
        return is_loopback_host(urlsplit(self.redirect_uri).hostname or "")

    @property
    def allowed_hosts(self) -> list[str]:
        """Host header values accepted by the HTTP routes (DNS-rebinding guard)."""
        hosts = ["127.0.0.1", "localhost", "::1", "[::1]"]
        if self.public_base_url:
            public = urlsplit(self.public_base_url).hostname
            if public and public not in hosts:
                hosts.append(public)
        return hosts

    # Loading ------------------------------------------------------------------

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = dict(os.environ if environ is None else environ)
        config_dir = (
            Path(env["GTASKS_CONFIG_DIR"]).expanduser()
            if env.get("GTASKS_CONFIG_DIR")
            else default_config_dir(env)
        )
        env_file = Path(env.get("GTASKS_ENV_FILE") or config_dir / "server.env").expanduser()
        merged = {**read_env_file(env_file), **env}

        def get(name: str) -> str | None:
            value = merged.get(name, "").strip()
            return value or None

        try:
            port = int(get("GTASKS_PORT") or DEFAULT_PORT)
        except ValueError as exc:
            raise ConfigError("GTASKS_PORT must be an integer") from exc
        state_dir = get("GTASKS_STATE_DIR")
        client_file = get("GTASKS_GOOGLE_CLIENT_FILE")
        fallback_file = get("GTASKS_FALLBACK_TOKEN_FILE")
        return cls(
            host=get("GTASKS_HOST") or DEFAULT_HOST,
            port=port,
            config_dir=config_dir,
            state_dir=Path(state_dir).expanduser() if state_dir else default_state_dir(env),
            env_file=env_file,
            google_client_file=Path(client_file).expanduser() if client_file else None,
            google_client_id=get("GTASKS_GOOGLE_CLIENT_ID"),
            google_client_secret=get("GTASKS_GOOGLE_CLIENT_SECRET"),
            fallback_token_file=Path(fallback_file).expanduser() if fallback_file else None,
            fallback_name=get("GTASKS_FALLBACK_NAME") or Settings.fallback_name,
            public_base_url=get("GTASKS_PUBLIC_BASE_URL"),
            timezone=get("GTASKS_TIMEZONE"),
            log_level=(get("GTASKS_LOG_LEVEL") or "INFO").upper(),
            tunnel_profile=get("GTASKS_TUNNEL_PROFILE") or DEFAULT_TUNNEL_PROFILE,
            tunnel_client=get("GTASKS_TUNNEL_CLIENT") or DEFAULT_TUNNEL_CLIENT,
        )
