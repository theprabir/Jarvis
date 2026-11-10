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
_WEEKDAY = re.compile(
    r"\b(?P<weekday>" + "|".join(WEEKDAYS) + r")\b", re.I
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


def _now():
    return datetime.now().replace(microsecond=0)


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
    """Return ``(hour, minute)`` from a simple clock phrase, or None."""
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
    return None


def _date_from_text(text, now):
    lowered = " ".join(str(text or "").lower().split())
    if "tonight" in lowered:
        return now.date()
    reference = find_date_reference(lowered, now.date())
    if reference:
        try:
            return date.fromisoformat(reference["date"])
        except (KeyError, ValueError):
            pass
    match = _WEEKDAY.search(lowered)
    if match:
        target = WEEKDAYS[match.group("weekday").lower()]
        offset = (target - now.weekday()) % 7
        return now.date() + timedelta(days=offset)
    return None


def parse_when(text, now=None, prefer_future=True):
    """Turn a due/reminder phrase into a local datetime.

    The parser deliberately handles a small, deterministic vocabulary: today,
    tomorrow, tonight, weekdays, relative days/weeks and ISO dates, with an
    optional clock time. Unrecognized text returns ``None`` instead of being
    guessed.
    """
    now = now or _now()
    phrase = " ".join(str(text or "").strip().split())
    if not phrase:
        return None
    lowered = phrase.lower()
    if re.search(r"\bin\b", lowered):
        seconds = parse_duration(lowered)
        if seconds:
            return now + timedelta(seconds=seconds)
    target_date = _date_from_text(lowered, now)
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
    if prefer_future and result <= now and target_date == now.date():
        reference = find_date_reference(lowered, now.date())
        weekday_only = _WEEKDAY.search(lowered) and reference is None
        if weekday_only:
            result += timedelta(days=7)
        else:
            result += timedelta(days=1)
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
        reference = find_date_reference(body, now.date())
        clock = _clock(body)
        if reference:
            try:
                target = date.fromisoformat(reference["date"])
            except (KeyError, ValueError):
                target = None
            if target is not None:
                hour, minute = clock or (9, 0)
                due_at = datetime.combine(
                    target, datetime.min.time()
                ).replace(hour=hour, minute=minute)
                body = body.replace(reference["phrase"], " ", 1)
                if clock:
                    body = _remove_clock(body)
        elif clock is not None:
            hour, minute = clock
            due_at = datetime.combine(
                now.date(), datetime.min.time()
            ).replace(hour=hour, minute=minute)
            if due_at <= now:
                due_at += timedelta(days=1)
            body = _remove_clock(body)

    if due_at is None and recurrence != "none":
        due_at = (now + timedelta(days=1)).replace(hour=9, minute=0,
                                                   second=0, microsecond=0)

    title = re.sub(r"\s+", " ", body).strip(" ,.;:-")
    title = re.sub(r"^to\s+", "", title, flags=re.I).strip()
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
    return " ".join(cleaned.split())


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
