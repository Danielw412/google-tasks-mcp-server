from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest

from google_tasks_mcp.config import Settings
from google_tasks_mcp.oauth import GoogleAuth
from google_tasks_mcp.server import Components, build_components

from .fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    client_file = config_dir / "client_secret.json"
    client_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    client_file.chmod(0o600)
    return Settings(config_dir=config_dir, state_dir=tmp_path / "state", timezone="America/New_York")


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
async def http(google: FakeGoogle) -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(transport=google.transport()) as client:
        yield client


@pytest.fixture
def auth(settings: Settings, http: httpx2.AsyncClient) -> GoogleAuth:
    return GoogleAuth(settings, http)


@pytest.fixture
async def connected(auth: GoogleAuth, google: FakeGoogle) -> GoogleAuth:
    request = auth.start_authorization()
    await auth.complete_from_redirect(google.consent(request.url))
    return auth


@pytest.fixture
def components(settings: Settings, http: httpx2.AsyncClient) -> Components:
    return build_components(settings, http)


async def connect(components: Components, google: FakeGoogle) -> None:
    request = components.auth.start_authorization()
    await components.auth.complete_from_redirect(google.consent(request.url))
