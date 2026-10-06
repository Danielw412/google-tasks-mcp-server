from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest
from mcp import Client

from google_tasks_mcp import task_behavior
from google_tasks_mcp.config import ConfigError, Settings, read_env_file
from google_tasks_mcp.dates import resolve_due, to_google_due
from google_tasks_mcp.errors import InvalidArgument
from google_tasks_mcp.logs import RedactingFormatter, redact
from google_tasks_mcp.server import Components
from google_tasks_mcp.task_behavior import NewTask, prepare_new_task

from .conftest import connect
from .fake_google import FakeGoogle

TUESDAY = date(2026, 10, 6)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("today", date(2026, 10, 6)),
        ("Tomorrow", date(2026, 10, 7)),
        ("by tomorrow.", date(2026, 10, 7)),
        ("day after tomorrow", date(2026, 10, 8)),
        ("friday", date(2026, 10, 9)),
        ("next friday", date(2026, 10, 9)),
        ("tuesday", date(2026, 10, 13)),  # Same weekday means next week by default.
        ("in 3 days", date(2026, 10, 9)),
        ("in two weeks", date(2026, 10, 20)),
        ("in 1 month", date(2026, 11, 6)),
        ("next week", date(2026, 10, 12)),
        ("end of week", date(2026, 10, 9)),
        ("end of month", date(2026, 10, 31)),
        ("oct 12", date(2026, 10, 12)),
        ("October 12th", date(2026, 10, 12)),
        ("12 october", date(2026, 10, 12)),
        ("jan 5", date(2027, 1, 5)),  # Past dates without a year roll forward.
        ("2026-12-25", date(2026, 12, 25)),
        ("2026-12-25T15:00:00Z", date(2026, 12, 25)),
        ("none", None),
        ("", None),
    ],
)
def test_due_phrases(phrase: str, expected: date | None) -> None:
    assert resolve_due(phrase, TUESDAY) == expected


def test_due_phrase_rules_are_configurable(monkeypatch) -> None:
    monkeypatch.setattr(task_behavior, "SAME_WEEKDAY_MEANS_TODAY", True)
    monkeypatch.setattr(task_behavior, "NEXT_WEEKDAY_SKIPS_A_WEEK", True)
    assert resolve_due("tuesday", TUESDAY) == TUESDAY
    assert resolve_due("next friday", TUESDAY) == date(2026, 10, 16)


@pytest.mark.parametrize(
    "phrase",
    [
        "whenever",
        "2026-02-30",
        "feb 30",
        "someday soon",
        "in 99999 months",
        "in 999999 weeks",
        "in 9999999999 days",
    ],
)
def test_bad_due_phrases(phrase: str) -> None:
    with pytest.raises(InvalidArgument):
        resolve_due(phrase, TUESDAY)


def test_google_due_format() -> None:
    assert to_google_due(date(2026, 10, 7)) == "2026-10-07T00:00:00.000Z"


def test_prepare_new_task_cleans_titles() -> None:
    task = prepare_new_task(
        NewTask(title='  "finish   physics homework."  ', notes="  ", due=None, task_list=None)
    )
    assert task.title == "Finish physics homework"
    assert task.notes is None
    with pytest.raises(ValueError):
        prepare_new_task(NewTask(title=' "" ', notes=None, due=None, task_list=None))


@pytest.mark.anyio
async def test_behavior_defaults_apply_to_create_task(
    components: Components, google: FakeGoogle, monkeypatch
) -> None:
    google.add_list("School")
    monkeypatch.setattr(task_behavior, "DEFAULT_TASK_LIST", "School")
    monkeypatch.setattr(task_behavior, "DEFAULT_DUE", "2026-11-01")

    def shout(task: NewTask) -> NewTask:
        return NewTask(title=task.title.upper(), notes=task.notes, due=task.due, task_list=task.task_list)

    monkeypatch.setattr(task_behavior, "prepare_new_task", shout)
    await connect(components, google)
    async with Client(components.mcp) as client:
        result = await client.call_tool("create_task", {"title": "essay"})
    data = result.structured_content
    assert data["task"]["title"] == "ESSAY"
    assert data["task_list"]["title"] == "School"
    assert data["task"]["due"] == "2026-11-01"


def test_settings_refuse_non_loopback_bind() -> None:
    with pytest.raises(ConfigError):
        Settings(host="0.0.0.0")  # noqa: S104 - the point of the test
    with pytest.raises(ConfigError):
        Settings(public_base_url="http://example.com")
    assert Settings(host="::1").mcp_url == "http://[::1]:8765/mcp"


def test_settings_load_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "server.env"
    env_file.write_text(
        "# comment\nexport GTASKS_PORT=9000\nGTASKS_PUBLIC_BASE_URL='https://tasks.example.ts.net/gt/'\n"
        "GTASKS_TIMEZONE=Europe/Berlin\n"
    )
    settings = Settings.load({"GTASKS_ENV_FILE": str(env_file), "GTASKS_PORT": "9100"})
    assert settings.port == 9100  # Process environment wins over the file.
    assert settings.redirect_uri == "https://tasks.example.ts.net/gt/oauth/google/callback"
    assert not settings.redirect_is_loopback
    assert settings.timezone == "Europe/Berlin"
    assert read_env_file(tmp_path / "missing.env") == {}


def test_settings_defaults() -> None:
    settings = Settings.load({"HOME": "/home/x", "XDG_CONFIG_HOME": "/cfg", "XDG_STATE_HOME": "/state"})
    assert settings.redirect_uri == "http://127.0.0.1:8765/oauth/google/callback"
    assert settings.client_file == Path("/cfg/google-tasks-mcp/client_secret.json")
    assert settings.token_file == Path("/state/google-tasks-mcp/google-token.json")


def test_log_redaction() -> None:
    line = (
        "GET /oauth/google/callback?state=abc123&code=4/0Adeu5 token ya29.a0AfB_byC "
        "refresh 1//0gAbCdEfGhIjKl "
        '{"refresh_token": "1//zzz", "client_secret": "GOCSPX-abc"} '
        "Authorization: Bearer ya29.xyz sk-proj-abcdefghijklmnop12"
    )
    out = redact(line)
    for secret in (
        "abc123",
        "4/0Adeu5",
        "ya29.a0AfB_byC",
        "0gAbCdEfGhIjKl",
        "GOCSPX-abc",
        "abcdefghijklmnop12",
    ):
        assert secret not in out


def test_redacting_formatter_covers_tracebacks() -> None:
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError("boom ya29.secretvalue")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    assert "secretvalue" not in formatter.format(record)
