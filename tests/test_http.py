from __future__ import annotations

import asyncio
import socket
from urllib.parse import urlsplit

import httpx2
import pytest
import uvicorn
from mcp import Client

from google_tasks_mcp.config import Settings
from google_tasks_mcp.server import build_app

from .fake_google import FakeGoogle

pytestmark = pytest.mark.anyio
BASE = "http://127.0.0.1:8765"


@pytest.fixture
def app_and_components(settings: Settings, http: httpx2.AsyncClient):
    return build_app(settings, http)


@pytest.fixture
async def web(app_and_components):
    app, _ = app_and_components
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as client:
        yield client


async def test_health_and_readiness(web: httpx2.AsyncClient) -> None:
    assert (await web.get("/healthz")).json() == {"status": "ok"}
    ready = (await web.get("/readyz")).json()
    assert ready["status"] == "ready"
    assert ready["google"] == {"state": "disconnected", "connected": False}


async def test_non_loopback_host_header_is_rejected(web: httpx2.AsyncClient) -> None:
    for path in ("/healthz", "/mcp", "/oauth/google/callback"):
        response = await web.get(path, headers={"Host": "evil.example"})
        assert response.status_code == 421, path


async def test_public_host_only_allowed_for_oauth_callback(tmp_path, http) -> None:
    settings = Settings(
        config_dir=tmp_path, state_dir=tmp_path / "s", public_base_url="https://tasks.example.ts.net"
    )
    app, _ = build_app(settings, http)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as web:
        headers = {"Host": "tasks.example.ts.net"}
        assert (
            await web.get("/oauth/google/callback", headers=headers)
        ).status_code == 400  # Reached the handler.
        assert (await web.get("/healthz", headers=headers)).status_code == 421
        assert (await web.post("/mcp", headers=headers, json={})).status_code == 421


async def test_oauth_callback_completes_sign_in(web, app_and_components, google: FakeGoogle) -> None:
    _, components = app_and_components
    redirect = google.consent(components.auth.start_authorization().url)
    parts = urlsplit(redirect)
    response = await web.get(f"{parts.path}?{parts.query}")
    assert response.status_code == 200
    assert "Google Tasks connected" in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert components.auth.status().connected
    assert "ya29." not in response.text and "1//" not in response.text
    # Replaying the same redirect fails cleanly.
    again = await web.get(f"{parts.path}?{parts.query}")
    assert again.status_code == 400 and "already used" in again.text


async def test_oauth_callback_rejects_forged_state(web, google: FakeGoogle) -> None:
    response = await web.get("/oauth/google/callback?state=forged&code=4/abc")
    assert response.status_code == 400
    assert "not connected" in response.text.lower()


async def test_oauth_callback_escapes_html(web) -> None:
    response = await web.get("/oauth/google/callback?error=<script>alert(1)</script>")
    assert "<script>alert" not in response.text


async def test_mcp_over_real_http(settings: Settings, http) -> None:
    """Start uvicorn on an ephemeral loopback port and talk Streamable HTTP to /mcp."""
    app, _ = build_app(settings, http)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.05)
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert {"create_task", "open_tasks_app", "ping"} <= tools
            result = await client.call_tool("ping", {"message": "over http"})
            assert result.structured_content["message"] == "over http"
            ui = [r for r in (await client.list_resources()).resources if str(r.uri).startswith("ui://")]
            assert ui
    finally:
        server.should_exit = True
        await task


async def test_other_loopback_bind_address_passes_host_guard(tmp_path, http) -> None:
    app, _ = build_app(Settings(host="127.0.0.2", config_dir=tmp_path, state_dir=tmp_path / "s"), http)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.2:8765"
    ) as web:
        assert (await web.get("/readyz")).status_code == 200
