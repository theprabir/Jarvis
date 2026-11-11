"""Local time tracking with one active entry and daily totals.

Entries share the assistant SQLite file but use their own ``time_entries``
table. No timer thread is needed: elapsed time is computed from persisted
timestamps whenever status or a report is requested.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from core.productivity import parse_when

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS time_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  started_at TEXT NOT NULL,
  stopped_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_time_started ON time_entries (started_at);
CREATE INDEX IF NOT EXISTS idx_time_stopped ON time_entries (stopped_at);
"""

COLUMNS = "id, label, started_at, stopped_at, created_at"


class TimeError(Exception):
    """Raised when a time entry cannot be started, stopped or read."""


@dataclass(frozen=True)
class TimeEntry:
    """One tracked interval."""

    id: int
    label: str
    started_at: datetime
    stopped_at: datetime | None
    created_at: str

    @property
    def active(self):
        return self.stopped_at is None

    def seconds(self, now=None):
        end = self.stopped_at or (now or datetime.now())
        return max(0, int((end - self.started_at).total_seconds()))


@dataclass(frozen=True)
class TimeTotal:
    """Tracked seconds for one label on one day."""

    label: str
    seconds: int


@dataclass(frozen=True)
class TimeReport:
    """Totals for one local day."""

    day: date
    totals: tuple[TimeTotal, ...] = ()
    total_seconds: int = 0

    @property
    def empty(self):
        return self.total_seconds == 0


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


def _duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _report_date(value, now=None):
    now = now or _now()
    text = " ".join(str(value or "").split()).strip().lower()
    if not text:
        return now.date()
    if text == "today":
        return now.date()
    if text == "yesterday":
        return now.date() - timedelta(days=1)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
    except ValueError:
        pass
    parsed = parse_when(text, now=now, prefer_future=False)
    if parsed is None:
        raise TimeError("I couldn't understand that date")
    return parsed.date()


def format_entry(entry, now=None):
    """Render one tracked interval."""
    now = now or _now()
    elapsed = _duration(entry.seconds(now))
    if entry.active:
        return (
            f"[{entry.id}] {entry.label} - active {elapsed}"
            f" (started {entry.started_at.strftime('%H:%M')})"
        )
    return (
        f"[{entry.id}] {entry.label} - {elapsed}"
        f" ({entry.started_at.strftime('%d %b %H:%M')} to"
        f" {entry.stopped_at.strftime('%d %b %H:%M')})"
    )


def format_report(report):
    """Render one day's totals."""
    if report.empty:
        return f"No tracked time for {report.day.strftime('%A %d %B %Y')}."
    lines = [f"Time totals for {report.day.strftime('%A %d %B %Y')}:"]
    for total in report.totals:
        lines.append(f"  {total.label} - {_duration(total.seconds)}")
    lines.append(f"Total: {_duration(report.total_seconds)}")
    return "\n".join(lines)


def format_week(reports, start):
    """Render a seven-day report."""
    total = sum(report.total_seconds for report in reports)
    if not total:
        return (
            f"No tracked time for the week starting"
            f" {start.strftime('%A %d %B %Y')}."
        )
    lines = [f"Time this week from {start.strftime('%A %d %B %Y')}:"]
    for report in reports:
        lines.append(
            f"  {report.day.strftime('%a %d %b')} -"
            f" {_duration(report.total_seconds)}"
        )
    lines.append(f"Total: {_duration(total)}")
    return "\n".join(lines)


def _day_bounds(day):
    start = datetime.combine(day, datetime.min.time())
    return start, start + timedelta(days=1)


class TimeStore:
    """SQLite persistence for tracked intervals."""

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
            raise TimeError(f"Could not open {self.path}: {exc}") from exc

    def start(self, label, now=None):
        label = " ".join(str(label or "").split()).strip()
        if not label:
            raise TimeError("a time entry needs a label")
        if len(label) > 200:
            raise TimeError("that time entry label is too long")
        if self.active() is not None:
            raise TimeError("a time entry is already running; stop it first")
        now = (now or _now()).replace(microsecond=0)
        try:
            with self._lock:
                cursor = self._db.execute(
                    "INSERT INTO time_entries"
                    " (label, started_at, stopped_at, created_at)"
                    " VALUES (?, ?, '', ?)",
                    (label, _iso(now), _iso(now)),
                )
                self._db.commit()
                entry_id = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise TimeError(f"Could not start tracking: {exc}") from exc
        entry = self.get(entry_id)
        if entry is None:
            raise TimeError("the time entry disappeared after starting")
        return entry

    def stop(self, now=None):
        entry = self.active()
        if entry is None:
            raise TimeError("no time entry is running")
        now = (now or _now()).replace(microsecond=0)
        if now <= entry.started_at:
            now = entry.started_at + timedelta(seconds=1)
        try:
            with self._lock:
                self._db.execute(
                    "UPDATE time_entries SET stopped_at = ? WHERE id = ?",
                    (_iso(now), entry.id),
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise TimeError(f"Could not stop tracking: {exc}") from exc
        return self.get(entry.id)

    def active(self):
        try:
            with self._lock:
                row = self._db.execute(
                    f"SELECT {COLUMNS} FROM time_entries"
                    " WHERE stopped_at = '' ORDER BY id DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise TimeError(f"Could not read the active entry: {exc}") from exc
        return self._entry(row) if row else None

    def get(self, entry_id):
        try:
            with self._lock:
                row = self._db.execute(
                    f"SELECT {COLUMNS} FROM time_entries WHERE id = ?",
                    (int(entry_id),),
                ).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return None
        return self._entry(row) if row else None

    def list(self, limit=50):
        try:
            with self._lock:
                rows = self._db.execute(
                    f"SELECT {COLUMNS} FROM time_entries"
                    " ORDER BY started_at DESC, id DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        except sqlite3.Error as exc:
            raise TimeError(f"Could not list time entries: {exc}") from exc
        return [self._entry(row) for row in rows]

    def count(self):
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM time_entries"
                ).fetchone()
        except sqlite3.Error as exc:
            raise TimeError(f"Could not count time entries: {exc}") from exc
        return int(row[0]) if row else 0

    def report(self, day, now=None):
        now = now or _now()
        start, end = _day_bounds(day)
        try:
            with self._lock:
                rows = self._db.execute(
                    f"SELECT {COLUMNS} FROM time_entries"
                    " WHERE started_at < ?"
                    " AND (stopped_at = '' OR stopped_at > ?)",
                    (_iso(end), _iso(start)),
                ).fetchall()
        except sqlite3.Error as exc:
            raise TimeError(f"Could not build the time report: {exc}") from exc
        totals = {}
        for row in rows:
            entry = self._entry(row)
            began = max(entry.started_at, start)
            ended = min(entry.stopped_at or now, end)
            seconds = max(0, int((ended - began).total_seconds()))
            if seconds:
                totals[entry.label] = totals.get(entry.label, 0) + seconds
        ordered = tuple(
            TimeTotal(label, seconds)
            for label, seconds in sorted(
                totals.items(), key=lambda item: (-item[1], item[0].lower())
            )
        )
        return TimeReport(day, ordered, sum(totals.values()))

    def week(self, start, now=None):
        start = start or (now or _now()).date()
        return tuple(
            self.report(start + timedelta(days=offset), now)
            for offset in range(7)
        )

    def delete(self, entry_id):
        entry = self.get(entry_id)
        if entry is None:
            raise TimeError("there is no time entry with that id")
        try:
            with self._lock:
                self._db.execute(
                    "DELETE FROM time_entries WHERE id = ?", (int(entry_id),)
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise TimeError(f"Could not delete the time entry: {exc}") from exc
        return entry

    def close(self):
        try:
            with self._lock:
                self._db.close()
        except sqlite3.Error:
            log.debug("Time tracking store close failed", exc_info=True)

    @staticmethod
    def _entry(row):
        keys = tuple(column.strip() for column in COLUMNS.split(","))
        data = dict(zip(keys, row))
        data["started_at"] = _parse(data["started_at"]) or datetime.now()
        data["stopped_at"] = _parse(data["stopped_at"])
        return TimeEntry(**data)


class TimeService:
    """Time tracking rules and reports."""

    def __init__(self, config):
        self._config = config
        self._store = TimeStore(config.resolved_db)

    def start(self, label, now=None):
        if self._store.count() >= self._config.max_entries:
            raise TimeError(
                f"the time entry limit of {self._config.max_entries} is reached"
            )
        return self._store.start(label, now=now)

    def stop(self, now=None):
        return self._store.stop(now=now)

    def active(self):
        return self._store.active()

    def get(self, entry_id):
        return self._store.get(entry_id)

    def list(self, limit=None):
        return self._store.list(limit or self._config.max_entries)

    def report(self, day=None, now=None):
        now = now or _now()
        return self._store.report(_report_date(day, now), now)

    def week(self, start=None, now=None):
        now = now or _now()
        if start:
            start_date = _report_date(start, now)
        else:
            start_date = now.date() - timedelta(days=now.weekday())
        return self._store.week(start_date, now)

    def delete(self, entry_id):
        return self._store.delete(entry_id)

    def close(self):
        self._store.close()
