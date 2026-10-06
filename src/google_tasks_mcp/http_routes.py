"""Plain HTTP routes next to ``/mcp``: health, readiness, and the Google OAuth callback.

Also provides ``HostGuard``, an ASGI middleware that rejects requests whose
``Host`` header is not loopback (DNS-rebinding protection). The one exception
is the OAuth callback, which also accepts the host of ``GTASKS_PUBLIC_BASE_URL``
when one is configured.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable

from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import MCP_PATH, OAUTH_CALLBACK_PATH, SERVER_NAME, Settings
from .errors import ServiceError
from .oauth import GoogleAuth

logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    ),
}

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1rem}}
h1{{font-size:1.3rem}} .ok{{color:#188038}} .bad{{color:#c5221f}}</style></head>
<body><h1 class="{cls}">{title}</h1><p>{message}</p></body></html>"""


def _page(title: str, message: str, *, ok: bool, status: int = 200) -> HTMLResponse:
    body = _PAGE.format(title=html.escape(title), message=html.escape(message), cls="ok" if ok else "bad")
    return HTMLResponse(body, status_code=status, headers=SECURITY_HEADERS)


def oauth_callback_endpoint(auth: GoogleAuth) -> Callable[[Request], Awaitable[HTMLResponse]]:
    """The callback handler, shared by the server and the temporary listener of `google-tasks-mcp auth`."""

    async def oauth_callback(request: Request) -> HTMLResponse:
        params = dict(request.query_params)
        if not params:
            return _page(
                "Nothing to do", "This address receives Google's sign-in response.", ok=False, status=400
            )
        try:
            await auth.complete_from_params(params)
        except ServiceError as exc:
            logger.info("Google sign-in callback failed: %s", exc.code)
            return _page(
                "Google Tasks was not connected", f"{exc.message} {exc.action or ''}", ok=False, status=400
            )
        return _page(
            "Google Tasks connected",
            "You can close this tab and return to ChatGPT. If the Google Tasks app is open, use Refresh.",
            ok=True,
        )

    return oauth_callback


def register_routes(mcp: MCPServer, auth: GoogleAuth) -> None:
    mcp.custom_route(OAUTH_CALLBACK_PATH, methods=["GET"], include_in_schema=False)(
        oauth_callback_endpoint(auth)
    )

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/readyz", methods=["GET"], include_in_schema=False)
    async def readyz(_: Request) -> JSONResponse:
        status = auth.status()
        return JSONResponse(
            {
                "status": "ready",
                "server": SERVER_NAME,
                "version": __version__,
                "mcp_path": MCP_PATH,
                "google": {"state": status.state, "connected": status.connected},
            },
            headers={"Cache-Control": "no-store"},
        )


def _host_without_port(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class HostGuard:
    """Reject HTTP requests whose Host header is not an allowed name."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.loopback = {"127.0.0.1", "localhost", "::1", _host_without_port(settings.host)}
        self.callback_hosts = self.loopback | {
            _host_without_port(h) for h in settings.allowed_hosts if h not in ("[::1]",)
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            host = _host_without_port(headers.get(b"host", b"").decode("latin-1"))
            allowed = self.callback_hosts if scope.get("path") == OAUTH_CALLBACK_PATH else self.loopback
            if host not in allowed:
                response = PlainTextResponse("Invalid Host header", status_code=421)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
