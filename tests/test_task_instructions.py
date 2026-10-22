from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from mcp import Client

from google_tasks_mcp import task_instructions
from google_tasks_mcp.config import Settings
from google_tasks_mcp.errors import InvalidArgument
from google_tasks_mcp.server import Components
from google_tasks_mcp.task_instructions import (
    DEFAULT_INSTRUCTIONS_FILE,
    TaskInstructionsStore,
    parse_instructions,
)

from .conftest import connect
from .fake_google import FakeGoogle
from .test_tools import call

FENCE = chr(96) * 3


def guide(default_due: str = "today", extra: str = "") -> str:
    return (
        f'# Guide\n\n{FENCE}toml\ndefault_due = "{default_due}"\n'
        '[lists.Jira]\ndefault_due = "none"\n[lists.SCC]\ndefault_due = "none"\n'
        f"{extra}\n{FENCE}\n\nUse concise titles.\n"
    )


@pytest.mark.anyio
async def test_creation_requires_current_guide_before_any_google_access(
    components: Components, google: FakeGoogle
) -> None:
    async with Client(components.mcp) as client:
        result = await client.call_tool("create_task", {"title": "debate dues due"})
        assert result.is_error
        assert result.structured_content["error"]["code"] == "task_instructions_required"
        assert google.requests == []
        read = (await client.call_tool("get_task_creation_instructions", {})).structured_content
        assert read["content"] == DEFAULT_INSTRUCTIONS_FILE.read_text(encoding="utf-8")
        assert read["timezone"] == "America/New_York"
        resource = await client.read_resource("task-creation://instructions")
        assert resource.contents[0].text == read["content"]
        # Direct user edits are visible immediately; an old revision cannot write.
        path = components.service.task_instructions.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(guide(), encoding="utf-8")
        stale = await client.call_tool(
            "create_task", {"title": "debate dues due", "instructions_revision": read["revision"]}
        )
        assert stale.is_error and google.requests == []
        fresh = (await client.call_tool("get_task_creation_instructions", {})).structured_content
        assert fresh["revision"] != read["revision"] and fresh["source_path"] == str(path)


@pytest.mark.anyio
async def test_client_without_revision_parameter_creates_after_seeing_the_guide(
    components: Components, google: FakeGoogle, monkeypatch
) -> None:
    """ChatGPT's cached tool list may lack instructions_revision and get_task_creation_instructions."""
    await connect(components, google)
    now = [1000.0]
    monkeypatch.setattr(task_instructions.time, "monotonic", lambda: now[0])
    async with Client(components.mcp) as client:
        first = await client.call_tool("create_task", {"title": "debate dues due"})
        assert first.is_error
        error = first.structured_content["error"]
        assert error["code"] == "task_instructions_required"
        assert DEFAULT_INSTRUCTIONS_FILE.read_text(encoding="utf-8") in error["message"]
        assert google.tasks["default-list"] == []

        retry = await client.call_tool("create_task", {"title": "debate dues due"})
        assert not retry.is_error, retry.structured_content
        assert [t["title"] for t in google.tasks["default-list"]] == ["debate dues due"]

        # A changed guide, or one shown too long ago, must be shown again first.
        path = components.service.task_instructions.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(guide(), encoding="utf-8")
        changed = await client.call_tool("create_task", {"title": "second"})
        assert changed.is_error and "Use concise titles." in changed.structured_content["error"]["message"]
        now[0] += task_instructions.GUIDE_SHOWN_SECONDS
        late = await client.call_tool("create_task", {"title": "second"})
        assert late.is_error
        assert not (await client.call_tool("create_task", {"title": "second"})).is_error
    assert len(google.tasks["default-list"]) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("list_name", "use_id", "due", "expected"),
    [
        ("School", False, None, "2026-10-06"),
        ("Tests", True, None, "2026-10-06"),
        ("Jira", False, None, None),
        ("SCC", True, None, None),
        ("New List", False, None, "2026-10-06"),
        ("Jira", True, "tomorrow", "2026-10-07"),
        ("SCC", False, "2026-10-16", "2026-10-16"),
        ("School", True, "none", None),
    ],
)
async def test_date_defaults_follow_resolved_list_and_preserve_explicit_dates(
    components: Components,
    google: FakeGoogle,
    monkeypatch,
    list_name: str,
    use_id: bool,
    due: str | None,
    expected: str | None,
) -> None:
    await connect(components, google)
    list_id = google.add_list(list_name)
    monkeypatch.setattr(components.service, "today", lambda: date(2026, 10, 6))
    async with Client(components.mcp) as client:
        error, data = await call(
            client,
            "create_task",
            {
                "title": "short task",
                "task_list": list_id if use_id else list_name.lower(),
                "due": due,
            },
        )
    assert not error, data
    assert data["task"]["due"] == expected
    assert data["task"]["title"] == "short task"


@pytest.mark.anyio
async def test_account_default_list_uses_its_own_date_rule(
    components: Components, google: FakeGoogle
) -> None:
    await connect(components, google)
    google.lists["default-list"]["title"] = "Jira"
    async with Client(components.mcp) as client:
        error, data = await call(client, "create_task", {"title": "IDE: fix url"})
    assert not error and data["task"]["due"] is None


@pytest.mark.anyio
async def test_approved_edit_updates_live_rules_and_does_not_mutate_tasks(
    components: Components, google: FakeGoogle, monkeypatch
) -> None:
    path = components.service.task_instructions.path
    async with Client(components.mcp) as client:
        _, current = await call(client, "get_task_creation_instructions")
        new_content = guide(extra='[lists."Future List"]\ndefault_due = "none"')
        error, proposal = await call(
            client,
            "propose_task_creation_instructions",
            {
                "content": new_content,
                "expected_revision": current["revision"],
            },
        )
        assert not error and not path.exists() and google.requests == []
        assert "+default_due" in proposal["diff"]
        denied, _ = await call(
            client,
            "update_task_creation_instructions",
            {
                "proposal_id": proposal["proposal_id"],
            },
        )
        assert denied and not path.exists()
        error, saved = await call(
            client,
            "update_task_creation_instructions",
            {
                "proposal_id": proposal["proposal_id"],
                "user_approved": True,
            },
        )
        assert not error and saved["revision"] != current["revision"]
        assert path.read_text(encoding="utf-8") == new_content and google.requests == []
        await connect(components, google)
        google.add_list("Future List")
        monkeypatch.setattr(components.service, "today", lambda: date(2026, 10, 6))
        error, created = await call(
            client,
            "create_task",
            {
                "title": "new item",
                "task_list": "Future List",
            },
        )
        assert not error and created["task"]["due"] is None


def test_stale_and_expired_proposals_cannot_change_the_guide(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "task_creation.md"
    path.write_text(guide(), encoding="utf-8")
    store = TaskInstructionsStore(path)
    current = store.read()
    proposal_id, _ = store.propose(guide("tomorrow"), current.revision)
    path.write_text(guide("next week"), encoding="utf-8")
    with pytest.raises(InvalidArgument, match="guide changed"):
        store.apply(proposal_id, user_approved=True)
    assert path.read_text(encoding="utf-8") == guide("next week")
    proposal_id, _ = store.propose(guide("tomorrow"), store.read().revision)
    proposal = store._proposals[proposal_id]
    monkeypatch.setitem(store._proposals, proposal_id, replace(proposal, expires_at=0))
    with pytest.raises(InvalidArgument, match="expired"):
        store.apply(proposal_id, user_approved=True)
    assert path.read_text(encoding="utf-8") == guide("next week")


def test_configured_file_is_authoritative_and_missing_file_fails(tmp_path: Path) -> None:
    path = tmp_path / "custom.md"
    settings = Settings.load({"GTASKS_TASK_INSTRUCTIONS_FILE": str(path)})
    assert settings.task_instructions_file == path
    store = TaskInstructionsStore(path, use_bundled_default=False)
    with pytest.raises(InvalidArgument, match="does not exist"):
        store.read()
    path.write_text(guide("tomorrow"), encoding="utf-8")
    assert store.read().default_due == "tomorrow"


@pytest.mark.parametrize(
    "content",
    [
        "# No defaults",
        guide("whenever"),
        guide(extra='[lists.jira]\ndefault_due = "today"'),
        f"{FENCE}toml\ndefault_due = true\n{FENCE}",
        f'{FENCE}toml\n[lists.Bad]\nunknown = "today"\n{FENCE}',
    ],
)
def test_invalid_guides_are_rejected(content: str, tmp_path: Path) -> None:
    with pytest.raises(InvalidArgument):
        parse_instructions(content, tmp_path / "guide.md")


@pytest.mark.anyio
async def test_guide_change_during_google_lookup_stops_creation(
    components: Components, google: FakeGoogle, monkeypatch
) -> None:
    await connect(components, google)
    path = components.service.task_instructions.path
    original = components.service.client.list_tasks

    async def change_guide(*args, **kwargs):
        result = await original(*args, **kwargs)
        path.write_text(guide(), encoding="utf-8")
        return result

    monkeypatch.setattr(components.service.client, "list_tasks", change_guide)
    async with Client(components.mcp) as client:
        error, result = await call(client, "create_task", {"title": "short task"})
    assert error and result["error"]["code"] == "task_instructions_required"
    assert google.tasks["default-list"] == []
