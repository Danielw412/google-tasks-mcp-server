"""Live, editable task guidance and preview-before-approval updates. No Google calls."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import date
from difflib import unified_diff
from pathlib import Path

from .dates import resolve_due
from .errors import InvalidArgument

DEFAULT_INSTRUCTIONS_FILE = Path(__file__).with_name("task_creation.md")
MAX_INSTRUCTIONS_LENGTH = 40_000
PROPOSAL_LIFETIME_SECONDS = 1800
MAX_PROPOSALS = 20
# How long create_task without instructions_revision is accepted after the agent was shown the guide.
GUIDE_SHOWN_SECONDS = 600


@dataclass(frozen=True)
class TaskInstructions:
    content: str
    source_path: Path
    default_due: str
    list_defaults: dict[str, str]

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def due_for(self, list_title: str) -> str:
        return self.list_defaults.get(list_title.casefold(), self.default_due)


def parse_instructions(content: str, path: Path) -> TaskInstructions:
    if not content.strip() or len(content) > MAX_INSTRUCTIONS_LENGTH:
        raise InvalidArgument(f"Instructions must contain 1–{MAX_INSTRUCTIONS_LENGTH} characters.")
    match = re.search(r"^```toml\s*\n(.*?)^```\s*$", content, re.MULTILINE | re.DOTALL)
    if not match:
        raise InvalidArgument("Keep the TOML date-defaults block in the Markdown instructions.")
    try:
        config = tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError as exc:
        raise InvalidArgument(f"Invalid TOML date defaults: {exc}") from None
    if set(config) - {"default_due", "lists"}:
        raise InvalidArgument("Date defaults accept only default_due and lists.")
    default_due = config.get("default_due", "today")
    if not isinstance(default_due, str) or not default_due.strip():
        raise InvalidArgument("default_due must be a nonempty date phrase or 'none'.")
    lists = config.get("lists", {})
    if not isinstance(lists, dict):
        raise InvalidArgument("lists must contain named TOML tables.")
    list_defaults: dict[str, str] = {}
    for name, values in lists.items():
        if not name.strip() or not isinstance(values, dict) or set(values) != {"default_due"}:
            raise InvalidArgument("Each list table needs a name and exactly one default_due value.")
        due = values["default_due"]
        if not isinstance(due, str) or not due.strip():
            raise InvalidArgument(f"default_due for {name!r} must be a nonempty string.")
        key = name.strip().casefold()
        if key in list_defaults:
            raise InvalidArgument(f"Duplicate list date default for {name!r} (names ignore case).")
        list_defaults[key] = due.strip()
    # Reject unusable defaults during preview, before an approved guide can be saved.
    for due in [default_due, *list_defaults.values()]:
        resolve_due(due, date(2026, 10, 6))
    return TaskInstructions(content, path, default_due.strip(), list_defaults)


@dataclass(frozen=True)
class Proposal:
    content: str
    expected_revision: str
    expires_at: float


class TaskInstructionsStore:
    def __init__(self, path: Path, *, use_bundled_default: bool = True) -> None:
        self.path = path
        self.use_bundled_default = use_bundled_default
        self._proposals: dict[str, Proposal] = {}
        self._shown: tuple[str, float] | None = None  # Revision last shown to an agent, and when.

    def read(self) -> TaskInstructions:
        path = self.path
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if not self.use_bundled_default:
                raise InvalidArgument(f"Task instructions file does not exist: {path}") from None
            path = DEFAULT_INSTRUCTIONS_FILE
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise InvalidArgument(f"Cannot read task instructions: {path}") from exc
        except (OSError, UnicodeError) as exc:
            raise InvalidArgument(f"Cannot read task instructions: {path}") from exc
        return parse_instructions(content, path)

    def show(self) -> TaskInstructions:
        """Read the guide for an agent, remembering that this revision was just shown."""
        instructions = self.read()
        self._shown = (instructions.revision, time.monotonic())
        return instructions

    def check_revision(self, revision: str | None) -> TaskInstructions:
        instructions = self.read()
        if revision == instructions.revision:
            return instructions
        if revision is None:
            # ChatGPT keeps the tool list from when the connector was added or last refreshed, so a
            # client may have neither instructions_revision nor get_task_creation_instructions. It
            # is shown the guide in this error instead, and may create tasks shortly afterwards.
            shown = self._shown
            if (
                shown
                and shown[0] == instructions.revision
                and time.monotonic() - shown[1] < GUIDE_SHOWN_SECONDS
            ):
                return instructions
            self.show()
            raise InvalidArgument(
                "Follow the current task creation guide below, then call create_task again. No task was "
                f"created.\n\n{instructions.content}",
                code="task_instructions_required",
                action="Rebuild the task by this guide and call create_task again within 10 minutes, passing "
                f"instructions_revision={instructions.revision} if create_task accepts it.",
            )
        raise InvalidArgument(
            "Load the current task creation instructions before creating this task. No task was created.",
            code="task_instructions_required",
            action="Call get_task_creation_instructions, follow its content, and pass its revision "
            "as instructions_revision to create_task.",
        )

    def propose(self, content: str, expected_revision: str) -> tuple[str, str]:
        current = self.read()
        if current.revision != expected_revision:
            raise InvalidArgument("The guide changed. Read it again before preparing a new proposal.")
        parse_instructions(content, self.path)
        if content == current.content:
            raise InvalidArgument("The proposed instructions are unchanged.")
        now = time.monotonic()
        self._proposals = {key: value for key, value in self._proposals.items() if value.expires_at > now}
        if len(self._proposals) >= MAX_PROPOSALS:
            del self._proposals[next(iter(self._proposals))]
        proposal_id = secrets.token_urlsafe(24)
        self._proposals[proposal_id] = Proposal(content, expected_revision, now + PROPOSAL_LIFETIME_SECONDS)
        diff = "".join(
            unified_diff(
                current.content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile="current/task_creation.md",
                tofile="proposed/task_creation.md",
            )
        )
        return proposal_id, diff

    def apply(self, proposal_id: str, *, user_approved: bool) -> TaskInstructions:
        if not user_approved:
            raise InvalidArgument("The user must explicitly approve the proposed diff before applying it.")
        proposal = self._proposals.get(proposal_id)
        if proposal is None or proposal.expires_at <= time.monotonic():
            raise InvalidArgument(
                "This proposal expired or is unavailable. Prepare a new proposal for approval."
            )
        if self.read().revision != proposal.expected_revision:
            raise InvalidArgument(
                "The guide changed. Prepare a new proposal and obtain approval of its diff."
            )
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=self.path.parent, delete=False
            ) as stream:
                temporary = stream.name
                stream.write(proposal.content)
            os.replace(temporary, self.path)
        except OSError as exc:
            raise InvalidArgument(f"Cannot save task instructions to {self.path}.") from exc
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()
        del self._proposals[proposal_id]
        return self.read()
