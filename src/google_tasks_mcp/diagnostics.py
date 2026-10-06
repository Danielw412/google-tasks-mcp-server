"""`google-tasks-mcp doctor`: end-to-end health checks that never print secrets."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import httpx2

from .config import MCP_SERVICE, TUNNEL_SERVICE, Settings, read_env_file
from .errors import ServiceError
from .google_tasks import GoogleTasksClient
from .oauth import GoogleAuth, load_client_config

Status = Literal["PASS", "WARN", "FAIL", "SKIP"]
REQUIRED_TOOLS = {
    "auth_status", "auth_connect", "auth_disconnect", "list_task_lists", "list_tasks", "get_task",
    "create_task", "update_task", "complete_task", "delete_task", "move_task", "open_tasks_app",
}  # fmt: skip


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    hint: str | None = None


def _private(path: Path) -> bool:
    return os.name != "posix" or not (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def _short_id(client_id: str) -> str:
    head, _, tail = client_id.partition("-")
    return f"{head[:6]}…-{tail[:4]}…" if tail else f"{client_id[:8]}…"


def _systemctl(*args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["systemctl", "--user", *args],  # noqa: S607 - resolved from PATH like any admin tool
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (result.stdout.strip() or result.stderr.strip() or "unknown").splitlines()[0]


class Doctor:
    def __init__(self, settings: Settings, *, offline: bool = False) -> None:
        self.settings = settings
        self.offline = offline
        self.checks: list[Check] = []

    def add(self, name: str, status: Status, detail: str, hint: str | None = None) -> None:
        self.checks.append(Check(name, status, detail, hint))

    # Configuration ------------------------------------------------------------------

    def check_config(self) -> None:
        s = self.settings
        self.add("bind_address", "PASS", f"MCP endpoint {s.mcp_url} (loopback only)")
        if s.env_file and s.env_file.exists():
            if _private(s.env_file):
                self.add("env_file", "PASS", f"{s.env_file}")
            else:
                self.add(
                    "env_file", "WARN", f"{s.env_file} is readable by other users", f"chmod 600 {s.env_file}"
                )
        else:
            self.add("env_file", "SKIP", f"{s.env_file} not present (defaults in use)")

    def check_google_client(self) -> bool:
        s = self.settings
        try:
            client = load_client_config(s)
        except ServiceError as exc:
            self.add("google_client", "FAIL", exc.message, "See README: 'Google Cloud setup'.")
            return False
        kind = {"installed": "Desktop app", "web": "Web application", "environment": "environment"}[
            client.kind
        ]
        self.add(
            "google_client", "PASS", f"{kind} OAuth client {_short_id(client.client_id)} from {client.source}"
        )
        if client.kind != "environment" and not _private(Path(client.source)):
            self.add(
                "google_client_file",
                "WARN",
                "client JSON is readable by other users",
                f"chmod 600 {client.source}",
            )
        redirect = s.redirect_uri
        if client.kind == "installed" and not s.redirect_is_loopback:
            self.add(
                "redirect_uri",
                "FAIL",
                f"{redirect}: Desktop app clients only accept loopback redirects",
                "Use a Web application client for GTASKS_PUBLIC_BASE_URL, or unset it.",
            )
        elif client.kind == "web" and redirect not in client.redirect_uris:
            self.add(
                "redirect_uri",
                "WARN",
                f"{redirect} is not listed in the downloaded client JSON",
                "Add it under 'Authorized redirect URIs' for this client in the Cloud console.",
            )
        else:
            mode = (
                "loopback: browser on this machine or via SSH tunnel"
                if s.redirect_is_loopback
                else "public URL"
            )
            self.add("redirect_uri", "PASS", f"{redirect} ({mode})")
        return True

    async def check_google(self, configured: bool) -> None:
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(20.0, connect=10.0)) as http:
            auth = GoogleAuth(self.settings, http)
            status = auth.status()
            token_file = self.settings.token_file
            if status.state == "connected":
                detail = f"connected since {status.connected_at}; scopes: {' '.join(status.scopes)}"
                self.add("google_auth", "PASS", detail)
                if status.authorization_expires_at:
                    self.add(
                        "google_auth_expiry",
                        "WARN",
                        f"authorization expires {status.authorization_expires_at} (Testing-mode app)",
                        "Publish the OAuth consent screen ('In production') for non-expiring refresh tokens.",
                    )
            elif status.state == "disconnected":
                self.add("google_auth", "WARN", status.message, "Run: google-tasks-mcp auth")
            elif status.state == "reauth_required":
                self.add("google_auth", "FAIL", status.message, "Run: google-tasks-mcp auth")
            elif configured:
                self.add("google_auth", "FAIL", status.message)
            if token_file.exists() and not _private(token_file):
                self.add(
                    "google_token_file",
                    "FAIL",
                    f"{token_file} is readable by other users",
                    f"chmod 600 {token_file}",
                )
            if status.state != "connected":
                self.add("google_api", "SKIP", "Google account not connected")
                return
            if self.offline:
                self.add("google_api", "SKIP", "--offline")
                return
            try:
                lists = await GoogleTasksClient(auth, http).list_tasklists()
            except ServiceError as exc:
                self.add("google_api", "FAIL", f"{exc.code}: {exc.message}", exc.action)
                return
            self.add("google_api", "PASS", f"read-only call succeeded: {len(lists)} task list(s) visible")

    # Local server ------------------------------------------------------------------------

    async def check_server(self) -> None:
        s = self.settings
        try:
            async with httpx2.AsyncClient(timeout=5.0) as http:
                health = await http.get(s.local_base_url + "/healthz")
                ready = await http.get(s.local_base_url + "/readyz")
        except httpx2.HTTPError as exc:
            self.add(
                "mcp_server",
                "FAIL",
                f"{s.local_base_url} not reachable ({type(exc).__name__})",
                f"systemctl --user status {MCP_SERVICE}",
            )
            self.add("mcp_protocol", "SKIP", "server not reachable")
            return
        if health.status_code == 200 and ready.status_code == 200:
            self.add(
                "mcp_server", "PASS", f"/healthz and /readyz OK (Google: {ready.json()['google']['state']})"
            )
        else:
            self.add("mcp_server", "FAIL", f"/healthz {health.status_code}, /readyz {ready.status_code}")
        await self.check_protocol()

    async def check_protocol(self) -> None:
        from mcp import Client
        from mcp.server.apps import APP_MIME_TYPE

        try:
            async with asyncio.timeout(20), Client(self.settings.mcp_url) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                ping = await client.call_tool("ping", {"message": "doctor"})
                resources = (await client.list_resources()).resources
                ui = [r for r in resources if str(r.uri).startswith("ui://")]
                contents = (await client.read_resource(str(ui[0].uri))).contents if ui else []
        except Exception as exc:  # Anything here means the endpoint is unusable.
            self.add(
                "mcp_protocol", "FAIL", f"MCP request to {self.settings.mcp_url} failed: {type(exc).__name__}"
            )
            return
        missing = REQUIRED_TOOLS - tools
        if missing or ping.is_error:
            self.add("mcp_protocol", "FAIL", f"missing tools: {sorted(missing)}; ping error: {ping.is_error}")
        else:
            self.add("mcp_protocol", "PASS", f"initialize, tools/list ({len(tools)} tools), ping OK")
        if not contents:
            self.add("mcp_app_ui", "FAIL", "no ui:// resource registered")
        else:
            item = contents[0]
            text = getattr(item, "text", "") or ""
            ok = item.mime_type == APP_MIME_TYPE and "ui/initialize" in text
            self.add(
                "mcp_app_ui", "PASS" if ok else "FAIL", f"{ui[0].uri} ({item.mime_type}, {len(text)} bytes)"
            )

    # Tunnel ------------------------------------------------------------------------------

    def _tunnel_profile(self) -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return base / "tunnel-client" / f"{self.settings.tunnel_profile}.yaml"

    def _tunnel_env(self) -> dict[str, str]:
        return {**os.environ, **read_env_file(self.settings.tunnel_env_file)}

    async def check_tunnel(self) -> None:
        s = self.settings
        binary = s.tunnel_client if os.path.isabs(s.tunnel_client) else shutil.which(s.tunnel_client)
        if not binary or not os.access(binary, os.X_OK):
            self.add("tunnel_client", "FAIL", f"{s.tunnel_client} not found or not executable")
            return
        version = (
            subprocess.run(  # noqa: S603
                [binary, "--version"], capture_output=True, text=True, timeout=10, check=False
            )
            .stdout.split("+")[0]
            .strip()
        )
        self.add("tunnel_client", "PASS", f"{binary} {version}")

        profile = self._tunnel_profile()
        if not profile.exists():
            self.add("tunnel_profile", "FAIL", f"profile {s.tunnel_profile!r} not found at {profile}")
            return
        text = profile.read_text(encoding="utf-8")
        urls = re.findall(r"^\s*-?\s*url:\s*\"?([^\"\s]+)", text, re.MULTILINE)
        mcp_urls = [u for u in urls if u.startswith("http://") or u.startswith("https://")]
        if s.mcp_url in mcp_urls:
            self.add("tunnel_profile", "PASS", f"{s.tunnel_profile} → {s.mcp_url}")
        else:
            self.add(
                "tunnel_profile",
                "WARN",
                f"{s.tunnel_profile} targets {mcp_urls or 'unknown'}, not {s.mcp_url}",
            )
        if not _private(profile):
            self.add(
                "tunnel_profile_perms",
                "WARN",
                f"{profile} is readable by other users",
                f"chmod 600 {profile}",
            )

        key_ref = re.search(r"api_key:\s*\"?env:([A-Za-z_][A-Za-z0-9_]*)", text)
        if key_ref:
            var = key_ref.group(1)
            in_file = var in read_env_file(s.tunnel_env_file)
            if in_file or os.environ.get(var):
                where = f"in {s.tunnel_env_file}" if in_file else "in this shell only"
                status: Status = "PASS" if in_file else "WARN"
                hint = None if in_file else f"Put {var}=... in {s.tunnel_env_file} for the systemd service."
                self.add("tunnel_api_key", status, f"{var} is set ({where})", hint)
                if in_file and not _private(s.tunnel_env_file):
                    self.add(
                        "tunnel_env_perms",
                        "FAIL",
                        f"{s.tunnel_env_file} is readable by others",
                        f"chmod 600 {s.tunnel_env_file}",
                    )
            else:
                self.add(
                    "tunnel_api_key", "FAIL", f"{var} is not set", f"Add {var}=... to {s.tunnel_env_file}"
                )

        listen = re.search(r"listen_addr:\s*\"?([0-9.]+:\d+)", text)
        if listen:
            base = f"http://{listen.group(1)}"
            try:
                async with httpx2.AsyncClient(timeout=5.0) as http:
                    live = await http.get(base + "/healthz")
                    ready = await http.get(base + "/readyz")
                status = "PASS" if live.status_code == 200 and ready.status_code == 200 else "WARN"
                self.add(
                    "tunnel_health",
                    status,
                    f"{base}: healthz {live.text.strip()!r}, readyz {ready.text.strip()!r}",
                )
            except httpx2.HTTPError:
                self.add(
                    "tunnel_health",
                    "FAIL",
                    f"tunnel-client not answering on {base}",
                    f"systemctl --user status {TUNNEL_SERVICE}",
                )

        result = subprocess.run(  # noqa: S603
            [binary, "doctor", "--profile", s.tunnel_profile],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=self._tunnel_env(),
        )
        verdict = re.search(r"^RESULT\s+(\w+)", result.stdout, re.MULTILINE)
        failed = re.search(r"^FAILED_CHECKS\s+(.+)$", result.stdout, re.MULTILINE)
        if verdict and verdict.group(1) == "pass":
            self.add("tunnel_doctor", "PASS", f"tunnel-client doctor --profile {s.tunnel_profile}: pass")
        else:
            detail = failed.group(1) if failed else f"exit code {result.returncode}"
            self.add(
                "tunnel_doctor",
                "FAIL",
                f"tunnel-client doctor failed: {detail}",
                f"Run: tunnel-client doctor --profile {s.tunnel_profile} --explain",
            )

    # systemd -------------------------------------------------------------------------------

    def check_systemd(self) -> None:
        if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
            self.add("systemd", "SKIP", "systemd not available")
            return
        for unit in (MCP_SERVICE, TUNNEL_SERVICE):
            enabled, active = _systemctl("is-enabled", unit), _systemctl("is-active", unit)
            ok = enabled == "enabled" and active == "active"
            self.add(
                f"systemd:{unit}",
                "PASS" if ok else "WARN",
                f"{enabled}, {active}",
                None if ok else "Run: scripts/setup-services.sh",
            )
        user = os.environ.get("USER") or ""
        try:
            linger = subprocess.run(  # noqa: S603
                ["loginctl", "show-user", user, "--property=Linger", "--value"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
        except OSError:
            linger = "unknown"
        self.add(
            "linger",
            "PASS" if linger == "yes" else "WARN",
            f"Linger={linger}",
            None if linger == "yes" else f"sudo loginctl enable-linger {user}",
        )

    async def run(self) -> list[Check]:
        self.check_config()
        configured = self.check_google_client()
        await self.check_google(configured)
        await self.check_server()
        await self.check_tunnel()
        self.check_systemd()
        return self.checks


def print_report(checks: list[Check], as_json: bool) -> int:
    failed = any(c.status == "FAIL" for c in checks)
    if as_json:
        print(json.dumps({"ok": not failed, "checks": [asdict(c) for c in checks]}, indent=2))
        return 1 if failed else 0
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"{c.status:<4}  {c.name:<{width}}  {c.detail}")
        if c.hint and c.status in ("WARN", "FAIL"):
            print(f"{'':<4}  {'':<{width}}  → {c.hint}")
    counts = {s: sum(c.status == s for c in checks) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    print(
        "\n" + ", ".join(f"{n} {s.lower()}" for s, n in counts.items()) + f" — {'FAIL' if failed else 'OK'}"
    )
    return 1 if failed else 0
