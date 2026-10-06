"""Command line: ``google-tasks-mcp [serve|auth|auth-status|disconnect|doctor|wait-ready]``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import sys
import threading
import time

import httpx2

from . import __version__
from .config import OAUTH_CALLBACK_PATH, ConfigError, Settings
from .errors import ServiceError
from .logs import configure_logging
from .oauth import GoogleAuth
from .server import new_http_client, serve


def _port_open(host: str, port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=1):
        return True
    return False


def _token_mtime(auth: GoogleAuth) -> int | None:
    try:
        return auth.tokens.path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _stdin_lines(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str]) -> None:
    """Feed stdin lines into ``queue`` from a daemon thread (so a pending read never blocks exit)."""

    def pump() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)

    threading.Thread(target=pump, daemon=True).start()


async def _callback_listener(settings: Settings, auth: GoogleAuth):  # type: ignore[no-untyped-def]
    """A temporary loopback server for the OAuth redirect when the main server isn't running."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route

    from .http_routes import HostGuard, oauth_callback_endpoint

    app = Starlette(routes=[Route(OAUTH_CALLBACK_PATH, oauth_callback_endpoint(auth), methods=["GET"])])
    app.add_middleware(HostGuard, settings=settings)
    config = uvicorn.Config(
        app, host=settings.host, port=settings.port, log_config=None, access_log=False, lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    return server, task


async def cmd_auth(settings: Settings, redirect_url: str | None, timeout: float) -> int:
    async with new_http_client() as http:
        auth = GoogleAuth(settings, http)
        if redirect_url:
            try:
                await auth.complete_from_redirect(redirect_url)
            except ServiceError as exc:
                print(f"Not connected: {exc.message}", file=sys.stderr)
                return 1
            print("Google Tasks connected.")
            return 0

        try:
            request = auth.start_authorization()
        except ServiceError as exc:
            print(f"{exc.message}\n{exc.action or ''}", file=sys.stderr)
            return 2

        before = _token_mtime(auth)
        listener = None
        if settings.redirect_is_loopback and not _port_open(settings.host, settings.port):
            listener = await _callback_listener(settings, auth)
            receiver = "this command (the server is not running)"
        else:
            receiver = "the running server"

        print("Open this link in a browser and allow access to Google Tasks:\n")
        print(f"  {request.url}\n")
        print(f"Google then redirects the browser to {request.redirect_uri}, handled by {receiver}.")
        if settings.redirect_is_loopback:
            print(
                "If your browser is on another computer, either first run\n"
                f"  ssh -L {settings.port}:127.0.0.1:{settings.port} <this-server>\n"
                "or, after approving, copy the URL from the browser's address bar (it will show a\n"
                "connection error) and paste it here."
            )
        print(
            "\nWaiting (the link expires in 10 minutes)... Paste the redirect URL, or press Ctrl+C to stop."
        )

        loop = asyncio.get_running_loop()
        lines: asyncio.Queue[str] = asyncio.Queue()
        _stdin_lines(loop, lines)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                try:
                    line = (await asyncio.wait_for(lines.get(), timeout=1.0)).strip()
                except TimeoutError:
                    line = ""
                if line:
                    try:
                        await auth.complete_from_redirect(line)
                    except ServiceError as exc:
                        print(f"Not connected: {exc.message}", file=sys.stderr)
                        return 1
                    break
                if _token_mtime(auth) != before and auth.status().connected:
                    break
            else:
                print("Timed out waiting for Google sign-in.", file=sys.stderr)
                return 1
        finally:
            if listener:
                server, task = listener
                server.should_exit = True
                await task
        status = auth.status()
        print(f"\nGoogle Tasks connected ({' '.join(status.scopes)}).")
        return 0


async def cmd_auth_status(settings: Settings, as_json: bool, verify: bool) -> int:
    async with new_http_client() as http:
        auth = GoogleAuth(settings, http)
        status = auth.status()
        verified: str | None = None
        if verify and status.connected:
            from .google_tasks import GoogleTasksClient

            try:
                lists = await GoogleTasksClient(auth, http).list_tasklists()
                verified = f"OK: {len(lists)} task list(s) readable"
            except ServiceError as exc:
                verified = f"FAILED: {exc.code}: {exc.message}"
            status = auth.status()
        if as_json:
            data = status.model_dump()
            if verified:
                data["verification"] = verified
            print(json.dumps(data, indent=2))
        else:
            print(f"State:        {status.state}")
            print(f"Message:      {status.message}")
            if status.action:
                print(f"Next step:    {status.action}")
            if status.client_type:
                print(f"OAuth client: {status.client_type}")
            print(f"Redirect URI: {status.redirect_uri}")
            if status.scopes:
                print(f"Scopes:       {' '.join(status.scopes)}")
            if status.connected_at:
                print(f"Connected at: {status.connected_at}")
            if status.last_refreshed_at:
                print(f"Refreshed at: {status.last_refreshed_at}")
            if status.authorization_expires_at:
                print(f"Expires at:   {status.authorization_expires_at} (Testing-mode consent screen)")
            if verified:
                print(f"Verification: {verified}")
        return 0 if status.connected else 1


async def cmd_disconnect(settings: Settings, assume_yes: bool) -> int:
    if not assume_yes:
        if not sys.stdin.isatty():
            print("Refusing to disconnect without --yes in a non-interactive shell.", file=sys.stderr)
            return 2
        if input("Revoke Google access and delete stored credentials? [y/N] ").strip().lower() not in (
            "y",
            "yes",
        ):
            print("Cancelled.")
            return 1
    async with new_http_client() as http:
        result = await GoogleAuth(settings, http).disconnect()
    print(result.message)
    return 0


async def cmd_wait_ready(settings: Settings, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    async with httpx2.AsyncClient(timeout=2.0) as http:
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx2.HTTPError):
                if (await http.get(settings.local_base_url + "/readyz")).status_code == 200:
                    print(f"MCP server ready at {settings.mcp_url}")
                    return 0
            await asyncio.sleep(0.5)
    print(f"MCP server not ready at {settings.local_base_url} after {timeout:.0f}s", file=sys.stderr)
    return 1


async def cmd_doctor(settings: Settings, as_json: bool, offline: bool) -> int:
    from .diagnostics import Doctor, print_report

    checks = await Doctor(settings, offline=offline).run()
    return print_report(checks, as_json)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="google-tasks-mcp", description="Personal Google Tasks MCP server.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("serve", help="run the MCP server on 127.0.0.1 (the default command)")
    p_auth = sub.add_parser("auth", help="connect or reconnect a Google account")
    p_auth.add_argument(
        "--redirect-url", metavar="URL", help="finish a sign-in using the URL Google redirected to"
    )
    p_auth.add_argument(
        "--timeout", type=float, default=600, help="seconds to wait for sign-in (default 600)"
    )
    p_status = sub.add_parser("auth-status", help="show the Google connection state (no secrets)")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--verify", action="store_true", help="also make a read-only Google Tasks call")
    p_disc = sub.add_parser("disconnect", help="revoke Google access and delete stored credentials")
    p_disc.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p_doc = sub.add_parser("doctor", help="check configuration, Google auth, server, tunnel, and services")
    p_doc.add_argument("--json", action="store_true")
    p_doc.add_argument("--offline", action="store_true", help="skip the read-only Google API call")
    p_wait = sub.add_parser("wait-ready", help="wait until the local server answers /readyz")
    p_wait.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    command = args.command or "serve"
    configure_logging(settings.log_level if command == "serve" else "WARNING")
    try:
        if command == "serve":
            asyncio.run(serve(settings))
            code = 0
        elif command == "auth":
            code = asyncio.run(cmd_auth(settings, args.redirect_url, args.timeout))
        elif command == "auth-status":
            code = asyncio.run(cmd_auth_status(settings, args.json, args.verify))
        elif command == "disconnect":
            code = asyncio.run(cmd_disconnect(settings, args.yes))
        elif command == "doctor":
            code = asyncio.run(cmd_doctor(settings, args.json, args.offline))
        else:
            code = asyncio.run(cmd_wait_ready(settings, args.timeout))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
