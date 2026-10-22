from __future__ import annotations

import re

import pytest
from mcp import Client
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID

from google_tasks_mcp.app_ui import APP_HTML, APP_URI
from google_tasks_mcp.server import Components

from .conftest import connect
from .fake_google import FakeGoogle

pytestmark = pytest.mark.anyio


async def test_ui_resource_is_registered_as_mcp_app(components: Components) -> None:
    async with Client(components.mcp) as client:
        resources = (await client.list_resources()).resources
        ui = [r for r in resources if str(r.uri).startswith("ui://")]
        assert [str(r.uri) for r in ui] == [APP_URI]
        assert ui[0].mime_type == APP_MIME_TYPE
        contents = (await client.read_resource(APP_URI)).contents
        assert contents[0].mime_type == APP_MIME_TYPE
        assert contents[0].text == APP_HTML
        meta = contents[0].meta or {}
        assert meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": [], "frameDomains": []}
        assert meta["ui"]["prefersBorder"] is True
        assert EXTENSION_ID in (client.server_capabilities.extensions or {})


async def test_ui_tools_bind_the_resource(components: Components) -> None:
    async with Client(components.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["open_tasks_app"].meta["ui"] == {"resourceUri": APP_URI, "visibility": ["model", "app"]}
    assert tools["tasks_app_state"].meta["ui"]["visibility"] == ["app"]
    # Only the app tool renders UI; ordinary task tools stay plain so they work without it.
    for name in ("create_task", "list_tasks", "complete_task", "delete_task", "update_task"):
        assert "resourceUri" not in (tools[name].meta or {}).get("ui", {}), name
        assert "app" in tools[name].meta["ui"]["visibility"], name


def test_ui_html_is_self_contained_and_uses_only_mcp_tools() -> None:
    # No external scripts, styles, images, or network calls: the CSP allows none.
    assert not re.search(r"""(src|href)\s*=\s*["']https?:""", APP_HTML)
    assert "fetch(" not in APP_HTML and "XMLHttpRequest" not in APP_HTML
    assert "googleapis.com" not in APP_HTML
    assert "localStorage" not in APP_HTML and "sessionStorage" not in APP_HTML
    for method in ("ui/initialize", "ui/notifications/initialized", "tools/call", "ui/open-link"):
        assert f'"{method}"' in APP_HTML
    assert '"2026-01-26"' in APP_HTML
    called = set(re.findall(r"""call(?:Tool)?\("([a-z_]+)\"""", APP_HTML))
    assert called == {
        "tasks_app_state",
        "get_task_creation_instructions",
        "create_task",
        "complete_task",
        "update_task",
        "delete_task",
        "auth_connect",
        "auth_status",
        "auth_disconnect",
    }


async def test_app_state_when_disconnected(components: Components) -> None:
    async with Client(components.mcp) as client:
        result = await client.call_tool("open_tasks_app", {})
    assert not result.is_error
    data = result.structured_content
    assert data["auth"]["state"] == "disconnected"
    assert data["tasks"] == [] and data["error"] is None


async def test_app_state_when_connected(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    google.add_list("School")
    google.add_task("default-list", "Laundry")
    async with Client(components.mcp) as client:
        result = await client.call_tool("tasks_app_state", {"task_list": "My Tasks"})
        data = result.structured_content
        assert data["auth"]["connected"]
        assert [lst["title"] for lst in data["task_lists"]] == ["My Tasks", "School"]
        assert data["selected_task_list"]["id"] == "default-list"
        assert [t["title"] for t in data["tasks"]] == ["Laundry"]
        missing = (await client.call_tool("tasks_app_state", {"task_list": "Gone"})).structured_content
        assert missing["error"]["code"] == "not_found"
        assert missing["selected_task_list"]["id"] == "default-list"
