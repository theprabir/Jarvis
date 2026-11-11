"""Local productivity to-dos, deadlines and recurring tasks.

Productivity items are separate from the durable multi-step execution plans in
:mod:`core.tasks`. They share the SQLite file but use their own ``todo_items``
table, so ``/tasks`` keeps its existing meaning and history.

Reminder delivery is delegated to :class:`core.timers.TimerService`. A to-do
reminder is stored in the existing ``reminders`` table with a ``todo`` source
marker, which keeps one lightweight scheduler thread for both timers and
to-dos.
"""

from __future__ import annotations

import calendar
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from core.dates import WEEKDAYS, find_date_reference
from core.utilities import parse_duration

log = logging.getLogger(__name__)

OPEN = "open"
DONE = "done"
CANCELLED = "cancelled"

RECURRENCES = {"none", "daily", "weekly", "monthly"}
STATUSES = {OPEN, DONE, CANCELLED}
PRIORITY_WORDS = {
    "low": "low",
    "normal": "normal",
    "medium": "normal",
    "high": "high",
    "critical": "critical",
    "urgent": "critical",
}
_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}

SCHEMA = """
CREATE TABLE IF NOT EXISTS todo_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '',
  priority TEXT NOT NULL DEFAULT 'normal',
  status TEXT NOT NULL DEFAULT 'open',
  due_at TEXT NOT NULL DEFAULT '',
  remind_at TEXT NOT NULL DEFAULT '',
  recurrence TEXT NOT NULL DEFAULT 'none',
  tags TEXT NOT NULL DEFAULT '',
  parent_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT NOT NULL DEFAULT '',
  last_notified_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_todo_status_due
  ON todo_items (status, due_at);
CREATE INDEX IF NOT EXISTS idx_todo_parent ON todo_items (parent_id);
"""

COLUMNS = (
    "id, title, details, priority, status, due_at, remind_at, recurrence,"
    " tags, parent_id, created_at, updated_at, completed_at,"
    " last_notified_at"
)

_TIME = re.compile(
    r"\b(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?"
    r"\s*(?P<meridiem>am|pm)\b",
    re.I,
)
_TIME_24 = re.compile(r"\bat\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\b")
_TIME_BARE = re.compile(
    r"\bat\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\b", re.I
)
_TIME_OF_DAY = re.compile(
    r"\b(?:(?:this|in\s+the)\s+)?"
    r"(?P<value>morning|afternoon|evening|night|tonight|noon|midnight)\b",
    re.I,
)
_END_OF = re.compile(
    r"\bend\s+of\s+(?P<value>day|week|month)\b", re.I
)
_WEEKDAY = re.compile(
    r"\b(?P<weekday>" + "|".join(WEEKDAYS) + r")\b", re.I
)
_AMOUNT_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_RELATIVE_DATE = re.compile(
    r"\b(?:in\s+(?P<amount1>\d{1,3}|a|an|one|two|three|four|five|six|"
    r"seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?P<unit1>days?|weeks?|months?)"
    r"|(?P<amount2>\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|"
    r"nine|ten|eleven|twelve)\s+(?P<unit2>days?|weeks?|months?)\s+"
    r"from\s+(?:now|today))\b",
    re.I,
)
_RELATIVE_DURATION = re.compile(
    r"\bin\s+(?P<amount>\d{1,3}|a|an|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve)\s+"
    r"(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?)\b",
    re.I,
)
_MONTH_DAY = re.compile(
    r"\b(?:(?P<month>" + _MONTH_NAMES + r")\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?|"
    r"(?P<day2>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
    r"(?P<month2>" + _MONTH_NAMES + r"))\b",
    re.I,
)
_DAY_OF_MONTH = re.compile(
    r"\b(?:on\s+)?(?:the\s+)?(?P<day>\d{1,2})(?:st|nd|rd|th)\b",
    re.I,
)
_DUE = re.compile(r"\bdue\s+(?P<when>.+)$", re.I)
_REMIND = re.compile(r"\bremind(?:\s+me)?\s+(?P<when>.+)$", re.I)
_PRIORITY = re.compile(
    r"\bpriority\s+(?P<value>low|normal|medium|high|critical|urgent)\b",
    re.I,
)
_REPEAT = re.compile(
    r"\b(?:repeat(?:ing)?|every)\s+"
    r"(?P<value>day|daily|week|weekly|month|monthly)\b",
    re.I,
)


class ProductivityError(Exception):
    """Raised when a to-do cannot be created, changed or read."""


@dataclass(frozen=True)
class TodoItem:
    """One persisted productivity item."""

    id: int
    title: str
    details: str
    priority: str
    status: str
    due_at: datetime | None
    remind_at: datetime | None
    recurrence: str
    tags: str
    parent_id: int | None
    created_at: str
    updated_at: str
    completed_at: str
    last_notified_at: str

    def is_overdue(self, now=None):
        now = now or datetime.now()
        return (
            self.status == OPEN
            and self.due_at is not None
            and self.due_at < now
        )


@dataclass(frozen=True)
class PlanDay:
    """The open to-dos scheduled for one day of a plan."""

    day: date
    items: tuple[TodoItem, ...] = ()


@dataclass(frozen=True)
class ProductivityPlan:
    """A read-only snapshot of open to-dos arranged by day."""

    start: date
    days: tuple[PlanDay, ...]
    overdue: tuple[TodoItem, ...] = ()
    unscheduled: tuple[TodoItem, ...] = ()
    later: tuple[TodoItem, ...] = ()
    total_open: int = 0

    @property
    def end(self):
        return self.start + timedelta(days=len(self.days) - 1)


@dataclass(frozen=True)
class _DateMatch:
    """A date phrase and the span it occupies in the source text."""

    target: date
    phrase: str
    start: int
    end: int


def _now():
    return datetime.now().replace(microsecond=0)


def _plan_date(value, now=None):
    """Resolve a plan date from a date, datetime or deterministic phrase."""
    if value in (None, ""):
        return (now or _now()).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = " ".join(str(value).split()).strip()
    if not text:
        return (now or _now()).date()
    if text.lower() in {"this week", "this week's"}:
        return (now or _now()).date()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
    except ValueError:
        pass
    parsed = parse_when(text, now=now, prefer_future=False)
    if parsed is None:
        raise ProductivityError("I couldn't understand that plan date")
    return parsed.date()


def _iso(value):
    return value.isoformat(timespec="seconds") if value else ""


def _parse(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _priority(value):
    text = str(value or "").strip().lower()
    if text in PRIORITY_WORDS:
        return PRIORITY_WORDS[text]
    raise ProductivityError(
        "priority must be low, normal, high or critical"
    )


def _recurrence(value):
    text = str(value or "").strip().lower()
    if text in {"", "none", "never"}:
        return "none"
    if text in {"day", "daily"}:
        return "daily"
    if text in {"week", "weekly"}:
        return "weekly"
    if text in {"month", "monthly"}:
        return "monthly"
    raise ProductivityError("recurrence must be none, daily, weekly or monthly")


def _clock(text):
    """Return ``(hour, minute)`` from a clock phrase, or None.

    Besides explicit clock times, the deterministic vocabulary accepts
    morning/afternoon/evening/night, noon, midnight and end of day/week/month.
    """
    match = _TIME.search(text or "")
    if match:
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        meridiem = match.group("meridiem").lower()
        if not 1 <= hour <= 12 or minute > 59:
            raise ProductivityError("that clock time is invalid")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
        return hour, minute
    match = _TIME_24.search(text or "")
    if match:
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        if not 0 <= hour <= 23 or minute > 59:
            raise ProductivityError("that clock time is invalid")
        return hour, minute
    match = _TIME_BARE.search(text or "")
    if match:
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        if not 0 <= hour <= 23 or minute > 59:
            raise ProductivityError("that clock time is invalid")
        lowered = str(text or "").lower()
        # A bare "at 5" is far more likely to mean after work than 5 a.m.
        # An afternoon/evening word makes a bare 7 mean 19:00.
        late = re.search(r"\b(?:afternoon|evening|night|tonight)\b", lowered)
        morning = re.search(r"\bmorning\b", lowered)
        if 1 <= hour <= 6 and not morning:
            hour += 12
        elif 7 <= hour <= 11 and late:
            hour += 12
        return hour, minute
    match = _END_OF.search(text or "")
    if match:
        return 17, 0
    if re.search(r"\beod\b", text or "", re.I):
        return 17, 0
    match = _TIME_OF_DAY.search(text or "")
    if match:
        value = match.group("value").lower()
        return {
            "morning": (9, 0),
            "afternoon": (15, 0),
            "evening": (18, 0),
            "night": (20, 0),
            "tonight": (20, 0),
            "noon": (12, 0),
            "midnight": (0, 0),
        }[value]
    return None


def _amount(value):
    """Read a small number written as digits or an English word."""
    text = str(value or "").strip().lower()
    if text.isdigit():
        return int(text)
    return _AMOUNT_WORDS.get(text, 0)


def _relative_seconds(text):
    """Return a relative duration in seconds, including word amounts."""
    seconds = parse_duration(text)
    if seconds:
        return seconds
    lowered = str(text or "").lower()
    if re.search(r"\bhalf\s+(?:an\s+)?hour\b", lowered):
        return 30 * 60
    if re.search(r"\bquarter\s+(?:of\s+)?(?:an\s+)?hour\b", lowered):
        return 15 * 60
    match = _RELATIVE_DURATION.search(lowered)
    if not match:
        return None
    amount = _amount(match.group("amount"))
    unit = match.group("unit").lower()
    if unit.startswith("min"):
        factor = 60
    elif unit.startswith("h"):
        factor = 60 * 60
    elif unit.startswith("d"):
        factor = 24 * 60 * 60
    else:
        factor = 7 * 24 * 60 * 60
    return amount * factor if amount else None


def _relative_duration_match(text):
    """Return an in/from duration and the span it occupies, or None."""
    lowered = " ".join(str(text or "").lower().split())
    match = _RELATIVE_DURATION.search(lowered)
    if match:
        amount = _amount(match.group("amount"))
        unit = match.group("unit").lower()
        if unit.startswith("min"):
            factor = 60
        elif unit.startswith("h"):
            factor = 60 * 60
        elif unit.startswith("d"):
            factor = 24 * 60 * 60
        else:
            factor = 7 * 24 * 60 * 60
        if amount:
            return amount * factor, match
    for pattern, seconds in (
        (r"\bin\s+half\s+(?:an\s+)?hour\b", 30 * 60),
        (r"\bin\s+a\s+quarter\s+(?:of\s+)?(?:an\s+)?hour\b", 15 * 60),
    ):
        match = re.search(pattern, lowered)
        if match:
            return seconds, match
    return None


def _relative_date_match(lowered, now):
    """Match in/from date phrases that find_date_reference does not cover."""
    match = _RELATIVE_DATE.search(lowered)
    if not match:
        return None
    amount = _amount(match.group("amount1") or match.group("amount2"))
    unit = (match.group("unit1") or match.group("unit2")).lower()
    if not amount:
        return None
    if unit.startswith("month"):
        target = _add_months(now, amount).date()
    else:
        days = amount * (7 if unit.startswith("week") else 1)
        target = now.date() + timedelta(days=days)
    return _DateMatch(target, match.group(0), match.start(), match.end())


def _next_calendar_date(month, day, base):
    """Return the next real occurrence of a month/day, or None."""
    for year in range(base.year, base.year + 9):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= base:
            return candidate
    return None


def _next_day_of_month(day, base):
    """Return the next month containing the requested day number."""
    for offset in range(0, 13):
        month = base.month - 1 + offset
        year = base.year + month // 12
        month = month % 12 + 1
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= base:
            return candidate
    return None


def _date_match(text, now):
    """Find the first understood date phrase and its source span."""
    lowered = " ".join(str(text or "").lower().split())
    relative = _relative_date_match(lowered, now)
    if relative is not None:
        return relative

    reference = find_date_reference(lowered, now.date())
    if reference:
        try:
            target = date.fromisoformat(reference["date"])
        except (KeyError, ValueError):
            target = None
        if target is not None:
            phrase = reference["phrase"]
            start = lowered.find(phrase)
            return _DateMatch(target, phrase, start, start + len(phrase))

    match = _MONTH_DAY.search(lowered)
    if match:
        month_name = (match.group("month") or match.group("month2")).lower()
        day = int(match.group("day") or match.group("day2"))
        target = _next_calendar_date(_MONTHS[month_name], day, now.date())
        if target is not None:
            return _DateMatch(target, match.group(0), match.start(), match.end())

    for pattern, target in (
        (r"\bnext\s+week\b", now.date() + timedelta(days=7)),
        (r"\bnext\s+month\b", _add_months(now, 1).date()),
    ):
        match = re.search(pattern, lowered)
        if match:
            return _DateMatch(target, match.group(0), match.start(), match.end())

    match = re.search(r"\bthis\s+weekend\b", lowered)
    if match:
        offset = (5 - now.weekday()) % 7
        target = now.date() + timedelta(days=offset)
        return _DateMatch(target, match.group(0), match.start(), match.end())

    match = re.search(r"\bend\s+of\s+week\b", lowered)
    if match:
        offset = (4 - now.weekday()) % 7
        target = now.date() + timedelta(days=offset)
        return _DateMatch(target, match.group(0), match.start(), match.end())

    match = re.search(r"\bend\s+of\s+month\b", lowered)
    if match:
        last_day = calendar.monthrange(now.year, now.month)[1]
        target = date(now.year, now.month, last_day)
        return _DateMatch(target, match.group(0), match.start(), match.end())

    match = re.search(r"\bend\s+of\s+day\b", lowered) or re.search(
        r"\beod\b", lowered
    )
    if match:
        return _DateMatch(
            now.date(), match.group(0), match.start(), match.end()
        )

    match = re.search(r"\btonight\b", lowered)
    if match:
        return _DateMatch(
            now.date(), match.group(0), match.start(), match.end()
        )

    match = _DAY_OF_MONTH.search(lowered)
    if match:
        target = _next_day_of_month(int(match.group("day")), now.date())
        if target is not None:
            return _DateMatch(target, match.group(0), match.start(), match.end())

    match = _WEEKDAY.search(lowered)
    if match:
        target = WEEKDAYS[match.group("weekday").lower()]
        offset = (target - now.weekday()) % 7
        return _DateMatch(
            now.date() + timedelta(days=offset),
            match.group(0),
            match.start(),
            match.end(),
        )
    return None


def _roll_past_time(result, lowered, now):
    """Move a same-day past time to the next sensible occurrence."""
    if "end of month" in lowered:
        month = result.month
        year = result.year + month // 12
        month = month % 12 + 1
        last_day = calendar.monthrange(year, month)[1]
        return result.replace(year=year, month=month, day=last_day)
    month_day = _MONTH_DAY.search(lowered)
    if month_day:
        month_name = (month_day.group("month") or month_day.group("month2"))
        day = int(month_day.group("day") or month_day.group("day2"))
        target = _next_calendar_date(
            _MONTHS[month_name.lower()], day, result.date() + timedelta(days=1)
        )
        if target is not None:
            return datetime.combine(target, result.time())
    day_match = _DAY_OF_MONTH.search(lowered)
    if day_match:
        target = _next_day_of_month(
            int(day_match.group("day")), result.date() + timedelta(days=1)
        )
        if target is not None:
            return datetime.combine(target, result.time())
    if (
        _WEEKDAY.search(lowered)
        or "weekend" in lowered
        or "end of week" in lowered
    ):
        return result + timedelta(days=7)
    return result + timedelta(days=1)


def parse_when(text, now=None, prefer_future=True):
    """Turn a due/reminder phrase into a local datetime.

    The parser deliberately handles a small, deterministic vocabulary: today,
    tomorrow, tonight, mornings and evenings, weekdays, weekend, next week or
    month, month/day names, relative days/weeks/months and ISO dates, with an
    optional clock time. Unrecognized text returns ``None`` instead of being
    guessed. A bare hour such as ``at 5`` means the afternoon; use ``5am`` when
    the morning is intended.
    """
    now = now or _now()
    phrase = " ".join(str(text or "").strip().split())
    if not phrase:
        return None
    lowered = phrase.lower()
    if re.search(r"\bin\b", lowered):
        seconds = _relative_seconds(lowered)
        if seconds:
            clock = _clock(lowered)
            if clock is None:
                return now + timedelta(seconds=seconds)
            base = now + timedelta(seconds=seconds)
            target_date = base.date()
        else:
            target_date = None
    else:
        target_date = None
    if target_date is None:
        match = _date_match(lowered, now)
        target_date = match.target if match is not None else None
    clock = _clock(lowered)
    if target_date is None and clock is None:
        return None
    if target_date is None:
        target_date = now.date()
    if clock is None:
        hour, minute = (20, 0) if "tonight" in lowered else (9, 0)
    else:
        hour, minute = clock
    result = datetime.combine(target_date, datetime.min.time()).replace(
        hour=hour, minute=minute
    )
    if prefer_future and result < now and target_date == now.date():
        result = _roll_past_time(result, lowered, now)
    return result


def parse_todo_input(text, now=None):
    """Parse a natural to-do body into service arguments.

    Returns a dict with ``title`` and optional ``priority``, ``due_at``,
    ``remind_at`` and ``recurrence``. It never calls the model.
    """
    now = now or _now()
    body = " ".join(str(text or "").split()).strip()
    if not body:
        return {"title": ""}

    recurrence = "none"
    match = _REPEAT.search(body)
    if match:
        recurrence = _recurrence(match.group("value"))
        body = (body[: match.start()] + " " + body[match.end():]).strip()

    chosen_priority = None
    match = _PRIORITY.search(body)
    if match:
        chosen_priority = _priority(match.group("value"))
        body = (body[: match.start()] + " " + body[match.end():]).strip()

    remind_at = None
    match = _REMIND.search(body)
    if match:
        remind_at = parse_when(match.group("when"), now, prefer_future=True)
        if remind_at is not None:
            body = (body[: match.start()] + " " + body[match.end():]).strip()

    due_at = None
    match = _DUE.search(body)
    if match:
        due_at = parse_when(match.group("when"), now, prefer_future=False)
        if due_at is not None:
            body = (body[: match.start()] + " " + body[match.end():]).strip()

    if due_at is None:
        date_match = _date_match(body, now)
        clock = _clock(body)
        if date_match is not None:
            phrase_text = body
            hour, minute = clock or (9, 0)
            due_at = datetime.combine(
                date_match.target, datetime.min.time()
            ).replace(hour=hour, minute=minute)
            body = (
                body[: date_match.start] + " " + body[date_match.end:]
            ).strip()
            if clock is not None:
                body = _remove_clock(body)
            if due_at < now and date_match.target == now.date():
                due_at = _roll_past_time(due_at, phrase_text, now)
        elif clock is not None:
            hour, minute = clock
            due_at = datetime.combine(
                now.date(), datetime.min.time()
            ).replace(hour=hour, minute=minute)
            if due_at < now:
                due_at += timedelta(days=1)
            body = _remove_clock(body)

    if due_at is None:
        relative = _relative_duration_match(body)
        if relative is not None:
            seconds, match = relative
            due_at = now + timedelta(seconds=seconds)
            body = (body[: match.start()] + " " + body[match.end():]).strip()

    if due_at is None and recurrence != "none":
        due_at = (now + timedelta(days=1)).replace(hour=9, minute=0,
                                                   second=0, microsecond=0)

    title = _clean_title(body)
    if due_at is None:
        due_at = remind_at
    return {
        "title": title,
        "priority": chosen_priority,
        "due_at": due_at,
        "remind_at": remind_at,
        "recurrence": recurrence,
    }


def _remove_clock(text):
    cleaned = _TIME.sub(" ", text)
    cleaned = _TIME_24.sub(" ", cleaned)
    cleaned = _TIME_BARE.sub(" ", cleaned)
    cleaned = _END_OF.sub(" ", cleaned)
    cleaned = re.sub(r"\beod\b", " ", cleaned, flags=re.I)
    cleaned = _TIME_OF_DAY.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _clean_title(text):
    """Remove dangling prepositions left after a date phrase is extracted."""
    title = re.sub(r"\s+", " ", text).strip(" ,.;:-")
    title = re.sub(r"^to\s+", "", title, flags=re.I).strip()
    title = re.sub(r"^(?:on|by|at|due)\s+", "", title, flags=re.I).strip()
    title = re.sub(
        r"\s+(?:on|by|at|due|before|after)$", "", title, flags=re.I
    ).strip()
    return title


def _add_months(value, months=1):
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def next_due(value, recurrence):
    """Advance one date by a recurrence interval."""
    if recurrence == "daily":
        return value + timedelta(days=1)
    if recurrence == "weekly":
        return value + timedelta(days=7)
    if recurrence == "monthly":
        return _add_months(value)
    return value


def _next_future(value, recurrence, now):
    result = next_due(value, recurrence)
    for _ in range(1000):
        if result > now:
            return result
        result = next_due(result, recurrence)
    raise ProductivityError("the recurrence could not be advanced")


def format_todo(item, now=None):
    """Render one compact to-do line for the console and voice reply."""
    now = now or _now()
    parts = [f"[{item.id}] {item.title}"]
    if item.due_at is not None:
        label = item.due_at.strftime("%a %d %b %H:%M")
        if item.is_overdue(now):
            label += " (overdue)"
        parts.append(f"due {label}")
    if item.priority != "normal":
        parts.append(item.priority)
    if item.recurrence != "none":
        parts.append(f"repeats {item.recurrence}")
    if item.status != OPEN:
        parts.append(item.status)
    return " - ".join(parts)


def _plan_order(item):
    """Order by deadline, then priority, then creation id."""
    return (
        item.due_at or datetime.max,
        _PRIORITY_RANK.get(item.priority, 2),
        item.id,
    )


def _plan_range(start, end):
    if start.year != end.year:
        return f"{start.strftime('%d %B %Y')} - {end.strftime('%d %B %Y')}"
    if start.month != end.month:
        return f"{start.strftime('%d %B')} - {end.strftime('%d %B %Y')}"
    return f"{start.strftime('%d')}-{end.strftime('%d %B %Y')}"


def _plan_section(lines, title, items, now, limit=10):
    if not items:
        return
    lines.append(f"{title}:")
    for item in items[:limit]:
        lines.append(f"  {format_todo(item, now)}")
    hidden = len(items) - limit
    if hidden > 0:
        lines.append(f"  ... and {hidden} more")


def format_plan(plan, now=None):
    """Render a daily or weekly plan for the console and voice reply."""
    now = now or _now()
    daily = len(plan.days) == 1
    if daily:
        heading = f"Daily plan for {plan.start.strftime('%A %d %B %Y')}:"
    else:
        heading = f"Weekly plan for {_plan_range(plan.start, plan.end)}:"
    lines = [heading]
    if plan.total_open == 0:
        lines.append("No open to-dos.")
        return "\n".join(lines)

    _plan_section(lines, "Overdue", plan.overdue, now)
    for entry in plan.days:
        if entry.day == now.date():
            label = f"Today - {entry.day.strftime('%A %d %B')}"
        else:
            label = entry.day.strftime("%A %d %B")
        if entry.items:
            _plan_section(lines, label, entry.items, now, limit=8)
        else:
            lines.append(f"{label}: no items")
    _plan_section(lines, "Unscheduled", plan.unscheduled, now)
    _plan_section(lines, "Upcoming" if daily else "Later", plan.later, now)

    word = "item" if plan.total_open == 1 else "items"
    summary = f"{plan.total_open} open {word}"
    if plan.overdue:
        summary += f", {len(plan.overdue)} overdue"
    lines.append("")
    lines.append(summary + ".")
    return "\n".join(lines)


class ProductivityStore:
    """SQLite persistence for productivity items."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=10.0
            )
            self._db.executescript(SCHEMA)
            self._db.commit()
        except (OSError, sqlite3.Error) as exc:
            raise ProductivityError(
                f"Could not open {self.path}: {exc}"
            ) from exc

    def add(
        self,
        title,
        details="",
        priority="normal",
        due_at=None,
        remind_at=None,
        recurrence="none",
        tags="",
        parent_id=None,
    ):
        title = " ".join(str(title or "").split()).strip()
        if not title:
            raise ProductivityError("a to-do needs a title")
        if len(title) > 300:
            raise ProductivityError("that to-do title is too long")
        priority = _priority(priority)
        recurrence = _recurrence(recurrence)
        if recurrence != "none" and due_at is None:
            raise ProductivityError("a recurring to-do needs a due date")
        now = _now()
        try:
            with self._lock:
                cursor = self._db.execute(
                    "INSERT INTO todo_items"
                    " (title, details, priority, status, due_at, remind_at,"
                    " recurrence, tags, parent_id, created_at, updated_at,"
                    " completed_at, last_notified_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')",
                    (
                        title,
                        str(details or "").strip(),
                        priority,
                        OPEN,
                        _iso(due_at),
                        _iso(remind_at),
                        recurrence,
                        str(tags or "").strip(),
                        int(parent_id) if parent_id else None,
                        _iso(now),
                        _iso(now),
                    ),
                )
                self._db.commit()
                item_id = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise ProductivityError(f"Could not save the to-do: {exc}") from exc
        item = self.get(item_id)
        if item is None:
            raise ProductivityError("the to-do disappeared after saving")
        return item

    def get(self, item_id):
        try:
            with self._lock:
                row = self._db.execute(
                    f"SELECT {COLUMNS} FROM todo_items WHERE id = ?",
                    (int(item_id),),
                ).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return None
        return self._item(row) if row else None

    def list(self, status=None, limit=50):
        query = f"SELECT {COLUMNS} FROM todo_items"
        params = []
        if status in STATUSES:
            query += " WHERE status = ?"
            params.append(status)
        query += (
            " ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'done' THEN 1"
            " ELSE 2 END, CASE WHEN due_at = '' THEN 1 ELSE 0 END,"
            " due_at, id"
        )
        query += " LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            with self._lock:
                rows = self._db.execute(query, params).fetchall()
        except sqlite3.Error as exc:
            raise ProductivityError(f"Could not list to-dos: {exc}") from exc
        return [self._item(row) for row in rows]

    def update(self, item_id, **fields):
        allowed = {
            "title",
            "details",
            "priority",
            "status",
            "due_at",
            "remind_at",
            "recurrence",
            "tags",
            "completed_at",
            "last_notified_at",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ProductivityError(
                "cannot update " + ", ".join(unknown)
            )
        item = self.get(item_id)
        if item is None:
            raise ProductivityError("there is no to-do with that id")
        values = {}
        for key, value in fields.items():
            if key == "title":
                value = " ".join(str(value or "").split()).strip()
                if not value:
                    raise ProductivityError("a to-do needs a title")
            elif key == "priority":
                value = _priority(value)
            elif key == "recurrence":
                value = _recurrence(value)
            elif key == "status":
                if value not in STATUSES:
                    raise ProductivityError("unknown to-do status")
            elif key in {"due_at", "remind_at"}:
                value = _iso(value)
            elif key in {
                "details",
                "tags",
                "completed_at",
                "last_notified_at",
            }:
                value = str(value or "").strip()
            values[key] = value
        if "recurrence" in values or "due_at" in values:
            recurrence = values.get("recurrence", item.recurrence)
            due = values["due_at"] if "due_at" in values else item.due_at
            due = _parse(due) if isinstance(due, str) else due
            if recurrence != "none" and due is None:
                raise ProductivityError(
                    "a recurring to-do needs a due date"
                )
        values["updated_at"] = _iso(_now())
        assignments = ", ".join(f"{key} = ?" for key in values)
        try:
            with self._lock:
                self._db.execute(
                    f"UPDATE todo_items SET {assignments} WHERE id = ?",
                    list(values.values()) + [int(item_id)],
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise ProductivityError(
                f"Could not update the to-do: {exc}"
            ) from exc
        return self.get(item_id)

    def complete(self, item_id):
        item = self.get(item_id)
        if item is None:
            raise ProductivityError("there is no to-do with that id")
        if item.status == DONE:
            return item
        return self.update(
            item_id, status=DONE, completed_at=_iso(_now())
        )

    def delete(self, item_id):
        item = self.get(item_id)
        if item is None:
            raise ProductivityError("there is no to-do with that id")
        try:
            with self._lock:
                self._db.execute(
                    "DELETE FROM todo_items WHERE id = ?", (int(item_id),)
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise ProductivityError(
                f"Could not delete the to-do: {exc}"
            ) from exc
        return item

    def close(self):
        try:
            with self._lock:
                self._db.close()
        except sqlite3.Error:
            log.debug("Productivity store close failed", exc_info=True)

    @staticmethod
    def _item(row):
        keys = tuple(column.strip() for column in COLUMNS.split(","))
        data = dict(zip(keys, row))
        data["due_at"] = _parse(data["due_at"])
        data["remind_at"] = _parse(data["remind_at"])
        data["parent_id"] = (
            int(data["parent_id"]) if data["parent_id"] is not None else None
        )
        return TodoItem(**data)


class ProductivityService:
    """To-do rules plus reminder scheduling through the timer service."""

    def __init__(self, config, timers=None):
        self._config = config
        self._timers = timers
        self._store = ProductivityStore(config.resolved_db)

    def add(
        self,
        title,
        details="",
        priority=None,
        due_at=None,
        remind_at=None,
        recurrence="none",
        tags="",
        parent_id=None,
    ):
        priority = priority or self._config.default_priority
        recurrence = _recurrence(recurrence)
        if due_at is not None and remind_at is None and due_at > _now():
            # A deadline with no explicit reminder still gets one at the
            # deadline, so adding a due date is not silently inert.
            remind_at = due_at
        if remind_at is not None and remind_at <= _now():
            raise ProductivityError("that reminder time has already passed")
        item = self._store.add(
            title,
            details=details,
            priority=priority,
            due_at=due_at,
            remind_at=remind_at,
            recurrence=recurrence,
            tags=tags,
            parent_id=parent_id,
        )
        try:
            self._schedule(item)
        except Exception:
            self._store.delete(item.id)
            raise
        return item

    def get(self, item_id):
        return self._store.get(item_id)

    def list(self, status=OPEN, limit=None):
        return self._store.list(
            status=status,
            limit=limit or self._config.max_items,
        )

    def daily_plan(self, day=None):
        """Build a read-only plan for one day, defaulting to today."""
        return self._plan(_plan_date(day), 1)

    def weekly_plan(self, start=None):
        """Build a read-only seven-day plan, defaulting to today."""
        return self._plan(_plan_date(start), 7)

    def _plan(self, start, days):
        rows = self.list(status=OPEN)
        schedule = {start + timedelta(days=offset): [] for offset in range(days)}
        end = start + timedelta(days=days)
        overdue = []
        unscheduled = []
        later = []
        for item in rows:
            if item.due_at is None:
                unscheduled.append(item)
                continue
            due_day = item.due_at.date()
            if due_day < start:
                overdue.append(item)
            elif due_day >= end:
                later.append(item)
            else:
                schedule[due_day].append(item)
        for items in schedule.values():
            items.sort(key=_plan_order)
        return ProductivityPlan(
            start=start,
            days=tuple(
                PlanDay(day, tuple(items)) for day, items in schedule.items()
            ),
            overdue=tuple(sorted(overdue, key=_plan_order)),
            unscheduled=tuple(sorted(unscheduled, key=_plan_order)),
            later=tuple(sorted(later, key=_plan_order)),
            total_open=len(rows),
        )

    def update(self, item_id, **fields):
        old = self._store.get(item_id)
        if old is None:
            raise ProductivityError("there is no to-do with that id")
        self._cancel_reminder(old)
        try:
            item = self._store.update(item_id, **fields)
            self._schedule(item)
        except Exception:
            # Put the old reminder back if the update failed after it was
            # cancelled; the database row is unchanged when validation fails.
            current = self._store.get(item_id)
            if current is not None:
                try:
                    self._schedule(current)
                except Exception:
                    log.debug("Could not restore reminder", exc_info=True)
            raise
        return item

    def complete(self, item_id):
        item = self._store.get(item_id)
        if item is None:
            raise ProductivityError("there is no to-do with that id")
        if item.status == DONE:
            return item, None
        self._cancel_reminder(item)
        done = self._store.complete(item_id)
        next_item = None
        if done.recurrence != "none" and done.due_at is not None:
            now = _now()
            next_date = _next_future(done.due_at, done.recurrence, now)
            next_remind = None
            if done.remind_at is not None:
                offset = done.remind_at - done.due_at
                next_remind = next_date + offset
                if next_remind <= now:
                    next_remind = next_date
            next_item = self.add(
                done.title,
                details=done.details,
                priority=done.priority,
                due_at=next_date,
                remind_at=next_remind,
                recurrence=done.recurrence,
                tags=done.tags,
                parent_id=done.parent_id or done.id,
            )
        return done, next_item

    def delete(self, item_id):
        item = self._store.get(item_id)
        if item is None:
            raise ProductivityError("there is no to-do with that id")
        self._cancel_reminder(item)
        return self._store.delete(item_id)

    def snooze(self, item_id, minutes):
        minutes = int(minutes)
        if not 1 <= minutes <= 7 * 24 * 60:
            raise ProductivityError("snooze must be between 1 minute and 7 days")
        item = self._store.get(item_id)
        if item is None:
            raise ProductivityError("there is no to-do with that id")
        if item.status != OPEN:
            raise ProductivityError("only open to-dos can be snoozed")
        remind = _now() + timedelta(minutes=minutes)
        self._cancel_reminder(item)
        item = self._store.update(item_id, remind_at=remind)
        self._schedule(item)
        return item

    def _schedule(self, item):
        if self._timers is None:
            return None
        if item.status != OPEN or item.remind_at is None:
            return None
        if item.remind_at <= _now():
            return None
        try:
            return self._timers.add_reminder(
                item.remind_at,
                item.title,
                source="todo",
                source_id=item.id,
            )
        except Exception as exc:
            raise ProductivityError(f"could not schedule reminder: {exc}") from exc

    def _cancel_reminder(self, item):
        if self._timers is None:
            return 0
        return self._timers.cancel_source("todo", item.id)

    def close(self):
        self._store.close()
