"""Advanced title/date hooks. User-editable guidance lives in task_creation.md.

The live guide is loaded by task_instructions.py. Code changes here still
require a server restart; edits to the active Markdown guide do not.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

# --- Defaults ------------------------------------------------------------------

# List that receives new tasks when the request names none. None means the
# account's default list ("My Tasks"); otherwise use a list title
# (case-insensitive) or a list id.
DEFAULT_TASK_LIST: str | None = None

# IANA timezone for "today", "tomorrow", weekdays, and so on. The GTASKS_TIMEZONE
# environment variable overrides it; None means the server's local timezone.
TIMEZONE: str | None = "America/New_York"

# Optional code-level override. None uses the Markdown guide's per-list defaults.
DEFAULT_DUE: str | None = None

# Add a warning (the task is still created) when an open task with the same
# title already exists in the target list. Costs one extra API call per task.
WARN_ON_DUPLICATE_TITLE = True

# --- Title clean-up (applied in prepare_new_task) ---------------------------------

CAPITALIZE_FIRST_LETTER = False
STRIP_TRAILING_PERIOD = True
# Google allows 1024 characters; longer titles are cut and the rest moved to notes.
MAX_TITLE_LENGTH = 1024

# --- Date phrases ----------------------------------------------------------------

# A bare weekday said on that same weekday ("friday" on a Friday):
# False means one week later, True means today.
SAME_WEEKDAY_MEANS_TODAY = False
# "next friday": False means the coming Friday (same as "friday");
# True means the Friday after that.
NEXT_WEEKDAY_SKIPS_A_WEEK = False
# "next week" and "end of week" land on these weekdays (0 = Monday ... 6 = Sunday).
NEXT_WEEK_WEEKDAY = 0
END_OF_WEEK_WEEKDAY = 4

# --- Guidance for ChatGPT --------------------------------------------------------

CREATION_GUIDANCE = """\
Creating tasks:
- Before EACH create_task call, call get_task_creation_instructions. Follow its live Markdown \
for list routing, title style, notes, and dates; pass its revision as instructions_revision.
- Do not list existing tasks to rediscover their style. The guide contains concrete examples.
- Use task tools directly for simple requests. Choose the list from the live guide unless the \
user specifies one. The live guide is authoritative over cached formatting or routing rules.
- For an unspecified date, omit due and use the guide's current per-list default, which the \
server enforces. Explicit dates or requests for no date take precedence. Put supplied times in notes.
- Never edit the guide while creating tasks. For requested instruction changes, preview with \
propose_task_creation_instructions, show the diff, obtain explicit user approval, then call \
update_task_creation_instructions with that proposal ID and user_approved=true.
- Confirm briefly with the returned title, list, and due date; mention any warnings.
"""


# --- Hook ------------------------------------------------------------------------


@dataclass(frozen=True)
class NewTask:
    """A task about to be created. ``due`` is already resolved to a date."""

    title: str
    notes: str | None
    due: date | None
    task_list: str | None


def prepare_new_task(task: NewTask) -> NewTask:
    """Last step before a new task is sent to Google. Customize freely.

    Return the task to create. Raise ``ValueError`` with a helpful message to
    reject it; the message is shown to ChatGPT or the app user.
    """
    title = " ".join(task.title.split()).strip(" \"'“”‘’")
    notes = task.notes.strip() if task.notes else None
    if STRIP_TRAILING_PERIOD and title.endswith(".") and not title.endswith(".."):
        title = title[:-1].rstrip()
    if CAPITALIZE_FIRST_LETTER and title:
        title = title[0].upper() + title[1:]
    if not title:
        raise ValueError("The task title is empty.")
    if len(title) > MAX_TITLE_LENGTH:
        overflow = title[MAX_TITLE_LENGTH:]
        title = title[:MAX_TITLE_LENGTH]
        notes = f"…{overflow}\n\n{notes}" if notes else f"…{overflow}"
    return replace(task, title=title, notes=notes or None)
