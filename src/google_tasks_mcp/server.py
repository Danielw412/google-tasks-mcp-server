"""Assemble the MCP server and its HTTP app, and run it on loopback.

Layout of the HTTP app (all on 127.0.0.1:8765 by default):

* ``/mcp``: MCP Streamable HTTP (stateless, JSON responses). The Secure MCP
  Tunnel forwards ChatGPT's requests here.
* ``/healthz`` and ``/readyz``: liveness and readiness for systemd and ``doctor``.
* ``/oauth/google/callback``: where Google sends the browser after sign-in.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx2
import uvicorn
from mcp.server import MCPServer
from starlette.applications import Starlette

from . import __version__
from .app_ui import build_apps
from .config import MCP_PATH, SERVER_NAME, Settings
from .google_tasks import GoogleTasksClient
from .http_routes import HostGuard, register_routes
from .oauth import GoogleAuth
from .service import TasksService
from .tools import SERVER_INSTRUCTIONS, register_tools


def new_http_client() -> httpx2.AsyncClient:
    """HTTP client for Google. TLS verification stays on (httpx2 uses the system trust store)."""
    return httpx2.AsyncClient(
        timeout=httpx2.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": f"google-tasks-mcp/{__version__}"},
    )


@dataclass
class Components:
    mcp: MCPServer
    service: TasksService
    auth: GoogleAuth


def build_components(settings: Settings, http: httpx2.AsyncClient) -> Components:
    auth = GoogleAuth(settings, http)
    service = TasksService(settings, auth, GoogleTasksClient(auth, http))
    mcp = MCPServer(
        SERVER_NAME,
        title="Google Tasks",
        description="Read and manage your Google Tasks.",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        extensions=[build_apps(service)],
    )
    register_tools(mcp, service)
    register_routes(mcp, auth)
    return Components(mcp=mcp, service=service, auth=auth)


def build_app(settings: Settings, http: httpx2.AsyncClient) -> tuple[Starlette, Components]:
    components = build_components(settings, http)
    app = components.mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        host=settings.host,
    )
    app.add_middleware(HostGuard, settings=settings)
    return app, components


async def serve(settings: Settings) -> None:
    async with new_http_client() as http:
        app, _ = build_app(settings, http)
        config = uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_config=None,  # Use our redacting root logger.
            access_log=False,  # Request lines can contain OAuth codes.
            server_header=False,
            proxy_headers=False,
            log_level=settings.log_level.lower(),
        )
        await uvicorn.Server(config).serve()
