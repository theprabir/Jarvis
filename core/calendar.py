"""Local calendar events and daily agendas.

Events are local records in their own ``calendar_events`` table. They share the
assistant SQLite file but need no timer, worker or external calendar service.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from core.productivity import parse_todo_input, parse_when
from core.utilities import parse_duration

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '',
  start_at TEXT NOT NULL,
  duration_minutes INTEGER NOT NULL DEFAULT 60,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_start ON calendar_events (start_at);
"""

COLUMNS = (
    "id, title, details, start_at, duration_minutes, created_at, updated_at"
)

_DURATION_END = re.compile(
    r"\s+for\s+(?P<duration>\d+\s*(?:minutes?|mins?|hours?|hrs?))\s*$",
    re.I,
)


class CalendarError(Exception):
    """Raised when a calendar event cannot be created, changed or read."""


@dataclass(frozen=True)
class CalendarEvent:
    """One local calendar event."""

    id: int
    title: str
    details: str
    start_at: datetime
    duration_minutes: int
    created_at: str
    updated_at: str

    @property
    def end_at(self):
        return self.start_at + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True)
class Agenda:
    """Events scheduled for one local day."""

    day: date
    events: tuple[CalendarEvent, ...] = ()


def _now():
    return datetime.now().replace(microsecond=0)


def _iso(value):
    return value.isoformat(timespec="seconds") if value else ""


def _parse(value):
    try:
        return datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _duration(value):
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise CalendarError("duration must be a number of minutes") from None
    if not 1 <= minutes <= 1440:
        raise CalendarError("duration must be between 1 and 1440 minutes")
    return minutes


def _agenda_date(value, now=None):
    now = now or _now()
    text = " ".join(str(value or "").split()).strip().lower()
    if not text or text == "today":
        return now.date()
    if text == "tomorrow":
        return now.date() + timedelta(days=1)
    if text == "yesterday":
        return now.date() - timedelta(days=1)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
    except ValueError:
        pass
    parsed = parse_when(text, now=now, prefer_future=False)
    if parsed is None:
        raise CalendarError("I couldn't understand that date")
    return parsed.date()


def parse_event_input(text):
    """Parse a natural event title, start time, duration and details."""
    body = " ".join(str(text or "").split()).strip()
    details = ""
    if "|" in body:
        body, details = (part.strip() for part in body.split("|", 1))
    duration_minutes = 60
    match = _DURATION_END.search(body)
    if match:
        seconds = parse_duration(match.group("duration"))
        if seconds:
            duration_minutes = max(1, min(1440, seconds // 60))
            body = body[: match.start()].strip()
    parsed = parse_todo_input(body)
    start_at = parsed.get("due_at")
    return {
        "title": parsed.get("title", ""),
        "start_at": start_at,
        "duration_minutes": duration_minutes,
        "details": details,
    }


def format_event(event):
    """Render one event line."""
    start = event.start_at.strftime("%H:%M")
    end = event.end_at.strftime("%H:%M")
    text = f"[{event.id}] {start}-{end} {event.title}"
    if event.details:
        text += f" - {event.details}"
    return text


def format_agenda(agenda):
    """Render a daily agenda."""
    if not agenda.events:
        return f"Your calendar is clear for {agenda.day.strftime('%A %d %B %Y')}."
    lines = [f"Agenda for {agenda.day.strftime('%A %d %B %Y')}:"]
    lines.extend(f"  {format_event(event)}" for event in agenda.events)
    return "\n".join(lines)


class CalendarStore:
    """SQLite persistence for calendar events."""

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
            raise CalendarError(f"Could not open {self.path}: {exc}") from exc

    def add(self, title, start_at, duration_minutes=60, details=""):
        title = " ".join(str(title or "").split()).strip()
        if not title:
            raise CalendarError("an event needs a title")
        if len(title) > 200:
            raise CalendarError("that event title is too long")
        if not isinstance(start_at, datetime):
            raise CalendarError("an event needs a start date and time")
        duration_minutes = _duration(duration_minutes)
        now = _now()
        try:
            with self._lock:
                cursor = self._db.execute(
                    "INSERT INTO calendar_events"
                    " (title, details, start_at, duration_minutes,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        title,
                        str(details or "").strip(),
                        _iso(start_at),
                        duration_minutes,
                        _iso(now),
                        _iso(now),
                    ),
                )
                self._db.commit()
                event_id = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise CalendarError(f"Could not save the event: {exc}") from exc
        event = self.get(event_id)
        if event is None:
            raise CalendarError("the event disappeared after saving")
        return event

    def get(self, event_id):
        try:
            with self._lock:
                row = self._db.execute(
                    f"SELECT {COLUMNS} FROM calendar_events WHERE id = ?",
                    (int(event_id),),
                ).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return None
        return self._event(row) if row else None

    def agenda(self, day):
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        try:
            with self._lock:
                rows = self._db.execute(
                    f"SELECT {COLUMNS} FROM calendar_events"
                    " WHERE start_at >= ? AND start_at < ?"
                    " ORDER BY start_at, id",
                    (_iso(start), _iso(end)),
                ).fetchall()
        except sqlite3.Error as exc:
            raise CalendarError(f"Could not read the agenda: {exc}") from exc
        return Agenda(day, tuple(self._event(row) for row in rows))

    def upcoming(self, now=None, limit=20):
        now = now or _now()
        try:
            with self._lock:
                rows = self._db.execute(
                    f"SELECT {COLUMNS} FROM calendar_events"
                    " WHERE start_at >= ? ORDER BY start_at, id LIMIT ?",
                    (_iso(now), max(1, int(limit))),
                ).fetchall()
        except sqlite3.Error as exc:
            raise CalendarError(f"Could not list events: {exc}") from exc
        return [self._event(row) for row in rows]

    def update(self, event_id, **fields):
        allowed = {"title", "details", "start_at", "duration_minutes"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise CalendarError("cannot update " + ", ".join(unknown))
        event = self.get(event_id)
        if event is None:
            raise CalendarError("there is no event with that id")
        values = {}
        for key, value in fields.items():
            if key == "title":
                value = " ".join(str(value or "").split()).strip()
                if not value:
                    raise CalendarError("an event needs a title")
            elif key == "duration_minutes":
                value = _duration(value)
            elif key == "start_at":
                if not isinstance(value, datetime):
                    value = parse_when(str(value), prefer_future=False)
                if value is None:
                    raise CalendarError("I couldn't understand that start time")
                value = _iso(value)
            else:
                value = str(value or "").strip()
            values[key] = value
        values["updated_at"] = _iso(_now())
        assignments = ", ".join(f"{key} = ?" for key in values)
        try:
            with self._lock:
                self._db.execute(
                    f"UPDATE calendar_events SET {assignments} WHERE id = ?",
                    list(values.values()) + [int(event_id)],
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise CalendarError(f"Could not update the event: {exc}") from exc
        return self.get(event_id)

    def delete(self, event_id):
        event = self.get(event_id)
        if event is None:
            raise CalendarError("there is no event with that id")
        try:
            with self._lock:
                self._db.execute(
                    "DELETE FROM calendar_events WHERE id = ?",
                    (int(event_id),),
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise CalendarError(f"Could not delete the event: {exc}") from exc
        return event

    def count(self):
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM calendar_events"
                ).fetchone()
        except sqlite3.Error as exc:
            raise CalendarError(f"Could not count events: {exc}") from exc
        return int(row[0]) if row else 0

    def close(self):
        try:
            with self._lock:
                self._db.close()
        except sqlite3.Error:
            log.debug("Calendar store close failed", exc_info=True)

    @staticmethod
    def _event(row):
        keys = tuple(column.strip() for column in COLUMNS.split(","))
        data = dict(zip(keys, row))
        data["start_at"] = _parse(data["start_at"]) or datetime.now()
        return CalendarEvent(**data)


class CalendarService:
    """Calendar rules and agenda queries."""

    def __init__(self, config):
        self._config = config
        self._store = CalendarStore(config.resolved_db)

    def add(self, title, start_at, duration_minutes=None, details=""):
        if self._store.count() >= self._config.max_events:
            raise CalendarError(
                f"the event limit of {self._config.max_events} is reached"
            )
        return self._store.add(
            title,
            start_at,
            self._config.default_duration_minutes
            if duration_minutes is None
            else duration_minutes,
            details=details,
        )

    def get(self, event_id):
        return self._store.get(event_id)

    def agenda(self, day=None, now=None):
        now = now or _now()
        return self._store.agenda(_agenda_date(day, now))

    def upcoming(self, now=None, limit=None):
        return self._store.upcoming(
            now, limit or self._config.max_events
        )

    def update(self, event_id, **fields):
        return self._store.update(event_id, **fields)

    def delete(self, event_id):
        return self._store.delete(event_id)

    def close(self):
        self._store.close()
