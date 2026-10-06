"""Task-creation behavior: the one file to edit to change how tasks are created.

Everything that decides *how a request like "add finish physics homework
tomorrow" becomes a Google Task* lives here:

* ``CREATION_GUIDANCE``: instructions ChatGPT reads (added to the server
  instructions and to the ``create_task`` tool description).
* Defaults: target list, timezone, default due date, duplicate warnings.
* Date-phrase rules used when ``due`` is a phrase such as "friday".
* ``prepare_new_task``: a hook that runs on every new task (from ChatGPT or
  the app UI) just before it is sent to Google.

Nothing in this file touches MCP transport, OAuth, or the Google API. After
editing, run ``pytest`` and restart the server (``systemctl --user restart
google-tasks-mcp``); then use "Refresh" on the plugin in ChatGPT so it reloads
the tool descriptions.
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
TIMEZONE: str | None = None

# Due date for tasks created without one. None means no due date; a phrase such
# as "today" is also accepted.
DEFAULT_DUE: str | None = None

# Add a warning (the task is still created) when an open task with the same
# title already exists in the target list. Costs one extra API call per task.
WARN_ON_DUPLICATE_TITLE = True

# --- Title clean-up (applied in prepare_new_task) ---------------------------------

CAPITALIZE_FIRST_LETTER = True
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
- For requests like "add X to my tasks", "remind me to X", or "I need to X by Friday", call \
create_task directly. Do not open the Google Tasks app for simple requests.
- title: a short imperative phrase in the user's words, without filler such as "remind me to", \
"add", or "to my Google Tasks", and without surrounding quotes. Example: "Finish physics homework".
- due: only when the user gives a date or day. Pass the user's phrase ("tomorrow", "friday", \
"in 3 days", "oct 12") or an ISO date (YYYY-MM-DD). The server resolves phrases in the user's \
timezone. Google Tasks stores dates only: if the user gives a time of day, keep the date and put \
the time in notes.
- notes: details the user actually gave (links, time of day, context). Never invent details.
- task_list: only when the user names a list; otherwise omit it so the default list is used.
- Several tasks in one request: call create_task once per task.
- Afterwards, confirm briefly using the title, list, and due date the tool returned. Mention any \
warnings, such as a possible duplicate.
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
