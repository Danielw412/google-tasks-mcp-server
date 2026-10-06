"""The fallback authorization: another app's token.json, used only when the native one is unusable."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client

from google_tasks_mcp.config import Settings
from google_tasks_mcp.server import Components, build_components

from .conftest import connect
from .fake_google import OTHER_CLIENT_ID, OTHER_CLIENT_SECRET, TASKS_SCOPE, FakeGoogle
from .test_tools import call

pytestmark = pytest.mark.anyio


def write_fallback(
    path: Path, google: FakeGoogle, *, fresh: bool = False, scopes: list[str] | None = None
) -> str:
    """Write a google-auth authorized-user token.json, as Canvas Task Sync does. Returns its refresh token."""
    access, refresh = google.issue_tokens()
    expiry = datetime.now(UTC) + (timedelta(hours=1) if fresh else timedelta(hours=-1))
    path.write_text(
        json.dumps(
            {
                "token": access,
                "refresh_token": refresh,
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": OTHER_CLIENT_ID,
                "client_secret": OTHER_CLIENT_SECRET,
                "scopes": scopes or [TASKS_SCOPE, "https://www.googleapis.com/auth/presentations.readonly"],
                "universe_domain": "googleapis.com",
                "account": "",
                # google-auth writes naive UTC with a trailing Z.
                "expiry": expiry.replace(tzinfo=None).isoformat() + "Z",
            }
        )
    )
    return refresh


@pytest.fixture
def fallback_file(tmp_path: Path) -> Path:
    return tmp_path / "other-app" / "token.json"


@pytest.fixture
def components(settings: Settings, http: httpx2.AsyncClient, fallback_file: Path) -> Components:
    fallback_file.parent.mkdir()
    settings = dataclasses.replace(
        settings, fallback_token_file=fallback_file, fallback_name="Canvas Task Sync"
    )
    return build_components(settings, http)


def expire_native(components: Components) -> None:
    """What Testing mode does after 7 days: Google stops honouring the native refresh token."""
    path = components.auth.settings.token_file
    data = json.loads(path.read_text())
    data["refresh_token_expires_at"] = 1.0
    path.write_text(json.dumps(data))


def refreshes_with(google: FakeGoogle, refresh_token: str) -> int:
    return sum(
        1
        for r in google.requests
        if r.url.path == "/token"
        and f"refresh_token={refresh_token.replace('/', '%2F')}" in r.content.decode()
    )


async def test_native_authorization_is_preferred(
    components: Components, google: FakeGoogle, fallback_file: Path
) -> None:
    await connect(components, google)
    refresh = write_fallback(fallback_file, google)
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "create_task", {"title": "Physics homework"})
    assert not is_error, data
    assert data["auth_notice"] is None
    assert refreshes_with(google, refresh) == 0
    account = json.loads(components.auth.settings.account_file.read_text())
    assert account["default_task_list_id"] == google.default_id


async def test_expired_native_auth_falls_back_and_notifies_after_the_task(
    components: Components, google: FakeGoogle, fallback_file: Path
) -> None:
    await connect(components, google)
    async with Client(components.mcp) as client:
        await call(client, "list_tasks")  # Records the native account.
    refresh = write_fallback(fallback_file, google)
    before = fallback_file.read_bytes()  # noqa: ASYNC240
    expire_native(components)

    async with Client(components.mcp) as client:
        is_error, data = await call(client, "create_task", {"title": "Physics homework"})
        assert not is_error, data
        notice = data["auth_notice"]
        assert notice["code"] == "google_native_auth_expired"
        assert "Canvas Task Sync" in notice["message"]
        assert "finish" in notice["instructions"] and "auth_connect" in notice["instructions"]
        assert [t["title"] for t in google.tasks[google.default_id]] == ["Physics homework"]

        _, status = await call(client, "auth_status")
        assert status["fallback_active"] and status["connected"]
        assert status["state"] == "reauth_required"

        # Reconnecting the native authorization ends the fallback.
        await connect(components, google)
        _, data = await call(client, "list_tasks")
        assert data["auth_notice"] is None

    # The stale access token was refreshed once, in memory; the other app's file is never written.
    assert refreshes_with(google, refresh) == 1
    assert fallback_file.read_bytes() == before  # noqa: ASYNC240


async def test_fresh_fallback_access_token_is_used_without_refreshing(
    components: Components, google: FakeGoogle, fallback_file: Path
) -> None:
    refresh = write_fallback(fallback_file, google, fresh=True)
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "list_task_lists")
    assert not is_error, data
    assert data["auth_notice"] is not None
    assert refreshes_with(google, refresh) == 0


async def test_fallback_for_another_google_account_is_refused(
    components: Components, google: FakeGoogle, fallback_file: Path
) -> None:
    await connect(components, google)
    async with Client(components.mcp) as client:
        await call(client, "list_tasks")
    write_fallback(fallback_file, google)
    expire_native(components)
    google.default_id = google.add_list("Someone else's tasks")  # The fallback sees another account.

    async with Client(components.mcp) as client:
        is_error, data = await call(client, "create_task", {"title": "Physics homework"})
        assert is_error
        assert data["error"]["code"] == "google_reauth_required"
        assert "different Google account" in data["error"]["message"]
        _, status = await call(client, "auth_status")
    assert not status["connected"] and not status["fallback_active"]
    assert all(not tasks for tasks in google.tasks.values())


async def test_both_authorizations_unusable_asks_to_reconnect(
    components: Components, google: FakeGoogle, fallback_file: Path
) -> None:
    await connect(components, google)
    google.revoke(write_fallback(fallback_file, google))
    expire_native(components)
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "list_tasks")
    assert is_error
    assert data["error"]["code"] == "google_reauth_required"
    assert "auth_connect" in data["error"]["action"]
    assert "unavailable too" in data["error"]["message"]


@pytest.mark.parametrize(
    "content",
    [None, "not json", {"client_id": OTHER_CLIENT_ID}, "no-tasks-scope"],
    ids=["missing", "garbage", "incomplete", "no-tasks-scope"],
)
async def test_unusable_fallback_file_is_reported(
    components: Components, google: FakeGoogle, fallback_file: Path, content: Any
) -> None:
    if content == "no-tasks-scope":
        write_fallback(fallback_file, google, scopes=["openid"])
    elif content is not None:
        fallback_file.write_text(content if isinstance(content, str) else json.dumps(content))  # noqa: ASYNC240
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "list_tasks")
        assert is_error and data["error"]["code"] == "google_not_connected"
        _, status = await call(client, "auth_status")
    assert not status["connected"] and "is unavailable" in status["message"]


async def test_disconnect_mentions_the_fallback(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    result = await components.auth.disconnect()
    assert "Canvas Task Sync" in result.message


def test_fallback_settings_load_from_environment(tmp_path: Path) -> None:
    settings = Settings.load(
        {
            "GTASKS_CONFIG_DIR": str(tmp_path),
            "GTASKS_FALLBACK_TOKEN_FILE": "~/projects/Canvas-Task-Sync/token.json",
            "GTASKS_FALLBACK_NAME": "Canvas Task Sync",
        }
    )
    assert settings.fallback_token_file == Path.home() / "projects/Canvas-Task-Sync/token.json"
    assert settings.fallback_name == "Canvas Task Sync"
    assert Settings.load({"GTASKS_CONFIG_DIR": str(tmp_path)}).fallback_token_file is None
