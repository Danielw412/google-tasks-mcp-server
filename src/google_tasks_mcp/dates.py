"""Due-date phrases and Google Tasks date conversion.

The phrase rules (weekday handling, "next week", and so on) are configured in
``task_behavior``. Google Tasks keeps only the date part of ``due``; the API
expects an RFC 3339 timestamp at midnight UTC.
"""

from __future__ import annotations

import calendar
import os
import re
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from . import task_behavior as behavior
from .errors import InvalidArgument

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}  # fmt: skip
MONTHS = {
    name.lower(): number
    for number in range(1, 13)
    for name in (calendar.month_name[number], calendar.month_abbr[number])
} | {"sept": 9}
NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
CLEAR_WORDS = {"", "none", "no", "no due date", "clear", "remove", "null"}

PHRASE_HELP = (
    "Use YYYY-MM-DD or a phrase like 'today', 'tomorrow', 'friday', 'next week', 'in 3 days', "
    "'end of month', or 'oct 12'."
)


def _system_zone_name() -> str | None:
    if os.environ.get("TZ"):
        return os.environ["TZ"].lstrip(":")
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        return target.split("zoneinfo/", 1)[1]
    try:
        return Path("/etc/timezone").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def resolve_timezone(configured: str | None) -> tuple[tzinfo, str]:
    """Return (tzinfo, label) from GTASKS_TIMEZONE, then task_behavior.TIMEZONE, then the system."""
    for name in (configured, behavior.TIMEZONE, _system_zone_name()):
        if name:
            try:
                return ZoneInfo(name), name
            except (ValueError, OSError):
                continue
    local = datetime.now().astimezone().tzinfo or UTC
    return local, datetime.now(local).tzname() or "local"


def today_in(tz: tzinfo) -> date:
    return datetime.now(tz).date()


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _number(word: str) -> int | None:
    if word.isdigit():
        return int(word)
    return NUMBER_WORDS.get(word)


def resolve_due(text: str | None, today: date, tz: tzinfo | None = None) -> date | None:
    """Turn a due-date phrase into a date. Returns None for empty or 'none'."""
    if text is None:
        return None
    raw = text.strip()
    phrase = re.sub(r"\s+", " ", raw.lower().strip(" .,!"))
    phrase = re.sub(r"^(due|by|on|before)\s+", "", phrase)
    if phrase in CLEAR_WORDS:
        return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", phrase):
        try:
            return date.fromisoformat(phrase)
        except ValueError:
            raise InvalidArgument(f"{raw!r} is not a real calendar date.") from None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[t ].+", phrase):
        try:
            moment = datetime.fromisoformat(raw.strip().replace("z", "Z"))
        except ValueError:
            raise InvalidArgument(f"Could not read the date-time {raw!r}. {PHRASE_HELP}") from None
        if moment.tzinfo is not None and tz is not None:
            moment = moment.astimezone(tz)
        return moment.date()

    simple = {
        "today": 0, "tonight": 0, "this evening": 0, "this afternoon": 0, "this morning": 0,
        "tomorrow": 1, "tmrw": 1, "tmr": 1, "tomorrow morning": 1, "tomorrow night": 1,
        "day after tomorrow": 2, "the day after tomorrow": 2, "yesterday": -1,
    }  # fmt: skip
    if phrase in simple:
        return today + timedelta(days=simple[phrase])

    if match := re.fullmatch(r"in (\w+) (day|days|week|weeks|month|months)", phrase):
        count = _number(match.group(1))
        if count is not None:
            unit = match.group(2)
            try:
                if unit.startswith("day"):
                    return today + timedelta(days=count)
                if unit.startswith("week"):
                    return today + timedelta(weeks=count)
                return _add_months(today, count)
            except (ValueError, OverflowError):
                raise InvalidArgument(f"{raw!r} is too far in the future.") from None

    if phrase == "next week":
        monday = today + timedelta(days=7 - today.weekday())
        return monday + timedelta(days=behavior.NEXT_WEEK_WEEKDAY)
    if phrase in ("end of week", "end of the week", "this week"):
        return today + timedelta(days=max(0, behavior.END_OF_WEEK_WEEKDAY - today.weekday()))
    if phrase in ("this weekend", "weekend", "the weekend"):
        return today + timedelta(days=max(0, 5 - today.weekday()))
    if phrase in ("end of month", "end of the month", "this month"):
        return today.replace(day=calendar.monthrange(today.year, today.month)[1])
    if phrase in ("next month", "end of next month"):
        nxt = _add_months(today.replace(day=1), 1)
        if phrase == "next month":
            return nxt
        return nxt.replace(day=calendar.monthrange(nxt.year, nxt.month)[1])

    if match := re.fullmatch(r"(?:(this|next|coming|the coming) )?(\w+)", phrase):
        prefix, word = match.groups()
        if word in WEEKDAYS:
            delta = (WEEKDAYS[word] - today.weekday()) % 7
            if delta == 0 and not (behavior.SAME_WEEKDAY_MEANS_TODAY and prefix != "next"):
                delta = 7
            if prefix == "next" and behavior.NEXT_WEEKDAY_SKIPS_A_WEEK:
                delta += 7
            return today + timedelta(days=delta)

    month_day = re.fullmatch(r"(\w+) (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?", phrase)
    day_month = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? (?:of )?(\w+)(?: (\d{4}))?", phrase)
    parts = None
    if month_day and month_day.group(1) in MONTHS:
        parts = (MONTHS[month_day.group(1)], int(month_day.group(2)), month_day.group(3))
    elif day_month and day_month.group(2) in MONTHS:
        parts = (MONTHS[day_month.group(2)], int(day_month.group(1)), day_month.group(3))
    if parts:
        month, day, year = parts
        try:
            candidate = date(int(year) if year else today.year, month, day)
            if not year and candidate < today:
                candidate = date(today.year + 1, month, day)
        except ValueError:
            raise InvalidArgument(f"{raw!r} is not a real calendar date.") from None
        return candidate

    raise InvalidArgument(f"Could not understand the due date {raw!r}. {PHRASE_HELP}")


def to_google_due(day: date) -> str:
    return f"{day.isoformat()}T00:00:00.000Z"


def from_google_due(value: str | None) -> str | None:
    return value[:10] if value else None
