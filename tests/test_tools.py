from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest
from mcp import Client

from google_tasks_mcp import task_behavior
from google_tasks_mcp.dates import today_in
from google_tasks_mcp.server import Components

from .conftest import connect
from .fake_google import FakeGoogle

pytestmark = pytest.mark.anyio


async def call(client: Client, name: str, args: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    result = await client.call_tool(name, args or {})
    assert result.structured_content is not None, result
    # Text content mirrors the structured content (MCP backwards-compatibility rule).
    assert json.loads(result.content[0].text) == result.structured_content
    return result.is_error, result.structured_content


def today(components: Components) -> date:
    return today_in(components.service.tz)


async def test_tools_report_not_connected_cleanly(components: Components) -> None:
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "list_tasks")
        assert is_error
        assert data["error"]["code"] == "google_not_connected"
        assert "auth_connect" in data["error"]["action"]
        is_error, status = await call(client, "auth_status")
        assert not is_error and status["state"] == "disconnected"


async def test_auth_connect_returns_link_and_instructions(components: Components) -> None:
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "auth_connect")
    assert not is_error
    assert data["authorization_url"].startswith("https://accounts.google.com/")
    assert "ssh -L 8765:127.0.0.1:8765" in data["instructions"]


async def test_create_task_from_natural_request(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    async with Client(components.mcp) as client:
        is_error, data = await call(
            client, "create_task", {"title": " finish physics homework.", "due": "tomorrow"}
        )
    assert not is_error, data
    tomorrow = (today(components) + timedelta(days=1)).isoformat()
    assert data["task"]["title"] == "Finish physics homework"
    assert data["task"]["due"] == tomorrow
    assert data["task_list"]["title"] == "My Tasks" and data["task_list"]["is_default"]
    assert data["message"] == f"Created “Finish physics homework” in My Tasks, due {tomorrow}."
    stored = google.tasks["default-list"][0]
    assert stored["due"] == f"{tomorrow}T00:00:00.000Z"


async def test_task_lifecycle(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    school = google.add_list("School")
    async with Client(components.mcp) as client:
        _, created = await call(
            client, "create_task", {"title": "Read chapter 4", "task_list": "school", "notes": "pp. 80-95"}
        )
        task_id = created["task"]["id"]
        assert created["task_list"]["id"] == school

        _, listed = await call(client, "list_tasks", {"task_list": "School"})
        assert [t["title"] for t in listed["tasks"]] == ["Read chapter 4"]
        assert listed["today"] == today(components).isoformat()

        # Located without naming the list.
        _, got = await call(client, "get_task", {"task_id": task_id})
        assert got["task"]["notes"] == "pp. 80-95"

        _, updated = await call(
            client,
            "update_task",
            {"task_id": task_id, "title": "Read chapter 5", "due": "2026-12-01", "notes": ""},
        )
        assert updated["task"]["title"] == "Read chapter 5"
        assert updated["task"]["due"] == "2026-12-01"
        assert updated["task"]["notes"] is None

        _, cleared = await call(client, "update_task", {"task_id": task_id, "due": "none"})
        assert cleared["task"]["due"] is None

        _, done = await call(client, "complete_task", {"task_id": task_id, "task_list": school})
        assert done["task"]["completed"] and done["task"]["status"] == "completed"
        _, open_only = await call(client, "list_tasks", {"task_list": school})
        assert open_only["count"] == 0
        _, with_done = await call(client, "list_tasks", {"task_list": school, "include_completed": True})
        assert with_done["count"] == 1

        _, reopened = await call(client, "complete_task", {"task_id": task_id, "completed": False})
        assert reopened["task"]["status"] == "needsAction" and reopened["task"]["completed_at"] is None

        _, deleted = await call(client, "delete_task", {"task_id": task_id})
        assert deleted["deleted"]
        is_error, missing = await call(client, "get_task", {"task_id": task_id})
        assert is_error and missing["error"]["code"] == "not_found"


async def test_move_and_reorder(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    a = google.add_task("default-list", "A")
    b = google.add_task("default-list", "B")  # Inserted first: order is B, A.
    other = google.add_list("Errands")
    async with Client(components.mcp) as client:
        _, moved = await call(client, "move_task", {"task_id": b["id"], "after_task_id": a["id"]})
        assert moved["task_list"]["title"] == "My Tasks"
        _, listed = await call(client, "list_tasks")
        assert [t["title"] for t in listed["tasks"]] == ["A", "B"]

        _, nested = await call(client, "move_task", {"task_id": b["id"], "parent_task_id": a["id"]})
        assert nested["task"]["parent"] == a["id"]

        _, across = await call(client, "move_task", {"task_id": a["id"], "destination_task_list": "Errands"})
        assert across["task_list"]["id"] == other
        assert google._find(other, a["id"]) is not None


async def test_search_across_lists_and_due_filters(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    work = google.add_list("Work")
    google.add_task("default-list", "Buy milk", due="2026-10-10T00:00:00.000Z")
    google.add_task(work, "Write report", due="2026-10-20T00:00:00.000Z", notes="quarterly")
    async with Client(components.mcp) as client:
        _, found = await call(client, "list_tasks", {"all_lists": True, "query": "QUARTERLY"})
        assert [t["title"] for t in found["tasks"]] == ["Write report"]
        assert found["tasks"][0]["task_list_title"] == "Work"
        assert found["task_list"] is None
        _, early = await call(client, "list_tasks", {"all_lists": True, "due_before": "2026-10-15"})
        assert [t["title"] for t in early["tasks"]] == ["Buy milk"]


async def test_task_list_management(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    async with Client(components.mcp) as client:
        _, created = await call(client, "create_task_list", {"title": "Groceries"})
        _, renamed = await call(
            client, "rename_task_list", {"task_list": "groceries", "new_title": "Shopping"}
        )
        assert renamed["task_list"]["id"] == created["task_list"]["id"]
        _, lists = await call(client, "list_task_lists")
        assert [lst["title"] for lst in lists["task_lists"]] == ["My Tasks", "Shopping"]
        assert lists["default_task_list_id"] == "default-list"
        is_error, refused = await call(client, "delete_task_list", {"task_list": "My Tasks"})
        assert is_error and refused["error"]["code"] == "invalid_argument"
        _, deleted = await call(client, "delete_task_list", {"task_list": "Shopping"})
        assert deleted["deleted"]


async def test_unknown_list_lists_alternatives(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "create_task", {"title": "x", "task_list": "Nope"})
    assert is_error and data["error"]["code"] == "not_found"
    assert "'My Tasks'" in data["error"]["message"]


async def test_bad_due_date_is_an_argument_error(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "create_task", {"title": "x", "due": "whenever"})
    assert is_error and data["error"]["code"] == "invalid_argument"
    assert "YYYY-MM-DD" in data["error"]["message"]


async def test_duplicate_and_past_due_warnings(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    google.add_task("default-list", "Call mom")
    async with Client(components.mcp) as client:
        _, data = await call(client, "create_task", {"title": "call mom", "due": "yesterday"})
    assert len(data["warnings"]) == 2


async def test_expired_access_token_is_refreshed_transparently(
    components: Components, google: FakeGoogle
) -> None:
    await connect(components, google)
    google.expire_access_tokens()  # Google forgets the token before its stated expiry.
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "list_task_lists")
    assert not is_error and data["task_lists"]


async def test_revoked_access_returns_reauth_error(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    google.revoke_all()
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "list_tasks")
        assert is_error and data["error"]["code"] == "google_reauth_required"
        _, status = await call(client, "auth_status")
        assert status["state"] == "reauth_required"


async def test_transient_google_failure_is_retried(
    components: Components, google: FakeGoogle, monkeypatch
) -> None:
    monkeypatch.setattr("google_tasks_mcp.google_tasks.RETRY_DELAYS", (0, 0))
    await connect(components, google)
    google.fail_next_api_with = 503
    async with Client(components.mcp) as client:
        is_error, _ = await call(client, "list_task_lists")
    assert not is_error


async def test_disconnect_tool(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    async with Client(components.mcp) as client:
        _, result = await call(client, "auth_disconnect")
        assert result["disconnected"] and result["revoked"]
        is_error, data = await call(client, "list_tasks")
        assert is_error and data["error"]["code"] == "google_not_connected"


async def test_no_credentials_in_any_tool_output(components: Components, google: FakeGoogle) -> None:
    await connect(components, google)
    stored = json.loads(components.auth.settings.token_file.read_text())
    secrets = [stored["access_token"], stored["refresh_token"], "GOCSPX-"]
    async with Client(components.mcp) as client:
        outputs = []
        for name, args in [
            ("auth_status", {}),
            ("list_task_lists", {}),
            ("list_tasks", {}),
            ("open_tasks_app", {}),
            ("tasks_app_state", {}),
            ("create_task", {"title": "t"}),
        ]:
            result = await client.call_tool(name, args)
            outputs.append(result.model_dump_json())
    blob = "\n".join(outputs)
    for secret in secrets:
        assert secret not in blob


async def test_annotations_mark_read_only_and_destructive_tools(components: Components) -> None:
    async with Client(components.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    read_only = {
        "ping",
        "auth_status",
        "auth_connect",
        "list_task_lists",
        "list_tasks",
        "get_task",
        "open_tasks_app",
        "tasks_app_state",
    }
    destructive = {"auth_disconnect", "delete_task", "delete_task_list"}
    for name, tool in tools.items():
        ann = tool.annotations
        assert ann is not None, name
        assert ann.read_only_hint is (name in read_only), name
        assert ann.destructive_hint is (name in destructive), name
        assert ann.open_world_hint is False, name
        if name not in read_only:
            assert tool.output_schema is not None, name


async def test_create_task_description_carries_editable_guidance(components: Components) -> None:
    async with Client(components.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert task_behavior.CREATION_GUIDANCE.strip() in tools["create_task"].description
        assert task_behavior.CREATION_GUIDANCE.strip() in (client.instructions or "")


async def test_ping(components: Components) -> None:
    async with Client(components.mcp) as client:
        is_error, data = await call(client, "ping", {"message": "hi"})
    assert not is_error and data["message"] == "hi" and data["success"]


async def test_reconnecting_to_another_account_drops_cached_lists(
    components: Components, google: FakeGoogle
) -> None:
    await connect(components, google)
    async with Client(components.mcp) as client:
        _, before = await call(client, "list_task_lists")
        assert before["default_task_list_id"] == "default-list"
        # The user reconnects as a different Google account with different lists.
        google.lists.clear()
        google.tasks.clear()
        google.default_id = google.add_list("Other account's tasks", list_id="other-default")
        await connect(components, google)
        is_error, created = await call(client, "create_task", {"title": "x"})
        assert not is_error and created["task_list"]["id"] == "other-default"
        _, after = await call(client, "list_task_lists")
        assert after["default_task_list_id"] == "other-default"


async def test_truncation_from_pagination_is_reported(
    components: Components, google: FakeGoogle, monkeypatch
) -> None:
    monkeypatch.setattr("google_tasks_mcp.google_tasks.PAGE_SIZE", 2)
    monkeypatch.setattr("google_tasks_mcp.google_tasks.MAX_TASKS_PER_LIST", 3)
    await connect(components, google)
    for i in range(6):
        google.add_task("default-list", f"task {i}")
    async with Client(components.mcp) as client:
        _, data = await call(client, "list_tasks", {"query": "task"})
    assert data["truncated"] is True


async def test_403_rate_limit_is_retried(components: Components, google: FakeGoogle, monkeypatch) -> None:
    monkeypatch.setattr("google_tasks_mcp.google_tasks.RETRY_DELAYS", (0, 0))
    await connect(components, google)
    google.fail_next_api_with, google.fail_reason = 403, "userRateLimitExceeded"
    async with Client(components.mcp) as client:
        is_error, _ = await call(client, "list_task_lists")
    assert not is_error


async def test_retried_delete_that_already_succeeded_reports_success(
    components: Components, google: FakeGoogle, monkeypatch
) -> None:
    monkeypatch.setattr("google_tasks_mcp.google_tasks.RETRY_DELAYS", (0, 0))
    await connect(components, google)
    task = google.add_task("default-list", "Old")
    async with Client(components.mcp) as client:
        await call(client, "get_task", {"task_id": task["id"]})  # Warm the list cache.
        google.fail_after_applying = ("DELETE", 503)  # Google deletes it, but the response is lost.
        is_error, data = await call(
            client, "delete_task", {"task_id": task["id"], "task_list": "default-list"}
        )
    assert not is_error and data["deleted"]
    assert google._find("default-list", task["id"]) is None
