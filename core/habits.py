"""Local daily habits, check-in streaks and recurring reminders.

Habits are separate from to-dos and notes. One check-in is stored per local
day, and reminders reuse :class:`core.timers.TimerService`; a habit reminder is
a single persisted daily entry, so no second scheduler is started.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from core.productivity import parse_when

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS habits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '',
  reminder_time TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habit_checkins (
  habit_id INTEGER NOT NULL,
  checkin_date TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (habit_id, checkin_date)
);
CREATE INDEX IF NOT EXISTS idx_habit_checkins_date
  ON habit_checkins (checkin_date);
"""

COLUMNS = "id, title, details, reminder_time, created_at, updated_at"

_SCHEDULE_WORDS = re.compile(
    r"\b(?:every\s+day|each\s+day|once\s+a\s+day|daily)\b", re.I
)
_DAY_PART = re.compile(
    r"\b(?:every|each|in\s+the)\s+"
    r"(?P<part>morning|afternoon|evening|night)\b",
    re.I,
)
_REMINDER = re.compile(
    r"\b(?:with\s+(?:a\s+)?)?remind(?:er)?(?:\s+me)?\s+"
    r"(?:at|around|by)\s+"
    r"(?P<when>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.I,
)
_AT_TIME = re.compile(
    r"\b(?:at|around)\s+"
    r"(?P<when>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.I,
)
_DAY_PARTS = {"morning": "09:00", "afternoon": "15:00",
              "evening": "20:00", "night": "20:00"}


class HabitError(Exception):
    """Raised when a habit cannot be created, changed or read."""


@dataclass(frozen=True)
class Habit:
    """One persisted daily habit."""

    id: int
    title: str
    details: str
    reminder_time: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class HabitSummary:
    """A habit with its current and best streaks."""

    habit: Habit
    streak: int
    best_streak: int
    done_today: bool
    total_checkins: int

    @property
    def id(self):
        return self.habit.id

    @property
    def title(self):
        return self.habit.title


def _now():
    return datetime.now().replace(microsecond=0)


def _today(value=None):
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _iso(value):
    return value.isoformat(timespec="seconds") if value else ""


def _clock(value):
    """Return a validated HH:MM reminder time, or an empty string."""
    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return ""
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hour, minute = (int(part) for part in text.split(":"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    parsed = parse_when(text, now=_now(), prefer_future=False)
    if parsed is not None:
        return parsed.strftime("%H:%M")
    raise HabitError("reminder must be a clock time such as 9pm or 21:00")


def _clean_title(value):
    title = " ".join(str(value or "").split()).strip(" ,.;:-")
    title = re.sub(r"^(?:called|named)\s+", "", title, flags=re.I)
    title = re.sub(r"^(?:to|that)\s+", "", title, flags=re.I)
    title = re.sub(r"\s+as\s+a\s+habit$", "", title, flags=re.I)
    return title.strip(" ,.;:-")


def parse_habit_input(text, now=None):
    """Parse a natural habit phrase into a title and optional reminder.

    It understands daily wording such as ``every day`` or ``daily``, and
    optional reminder phrases such as ``at 9pm``, ``every morning`` or
    ``with a reminder at 08:30``. Unrecognized text stays in the title.
    """
    now = now or _now()
    body = " ".join(str(text or "").split()).strip()
    if not body:
        return {"title": "", "reminder_time": ""}

    reminder = ""
    match = _REMINDER.search(body)
    if match is None:
        match = _AT_TIME.search(body)
    if match is not None:
        try:
            reminder = _clock(match.group("when"))
        except HabitError:
            reminder = ""
        else:
            body = (body[: match.start()] + " " + body[match.end():]).strip()

    if not reminder:
        match = _DAY_PART.search(body)
        if match is not None:
            reminder = _DAY_PARTS[match.group("part").lower()]
            body = (body[: match.start()] + " " + body[match.end():]).strip()

    body = _SCHEDULE_WORDS.sub(" ", body)
    body = re.sub(r"\b(?:remind(?:er)?\s+(?:me\s+)?(?:daily|every\s+day))\b",
                  " ", body, flags=re.I)
    title = _clean_title(body)
    return {"title": title, "reminder_time": reminder}


def _streaks(days, today):
    """Return current and best consecutive-day streaks for one habit."""
    ordered = sorted(set(days))
    if not ordered:
        return 0, 0
    best = run = 1
    for previous, current in zip(ordered, ordered[1:]):
        run = run + 1 if current == previous + timedelta(days=1) else 1
        best = max(best, run)
    checked = set(ordered)
    if today in checked:
        anchor = today
    elif today - timedelta(days=1) in checked:
        anchor = today - timedelta(days=1)
    else:
        return 0, best
    current = 0
    while anchor in checked:
        current += 1
        anchor -= timedelta(days=1)
    return current, best


def format_habit(summary):
    """Render one compact habit line with today's state and streak."""
    habit = summary.habit
    if summary.done_today:
        state = "done today"
    else:
        state = "not done today"
    streak = f"{summary.streak} day streak"
    parts = [f"[{habit.id}] {habit.title}", streak, state]
    if habit.reminder_time:
        parts.append(f"reminder {habit.reminder_time} daily")
    return " - ".join(parts)


def format_habit_detail(summary, today=None):
    """Render full habit detail, including best streak and recent days."""
    habit = summary.habit
    today = _today(today)
    lines = [
        f"Habit #{habit.id}: {habit.title}",
        f"Current streak: {summary.streak} day"
        + ("" if summary.streak == 1 else "s"),
        f"Best streak: {summary.best_streak} day"
        + ("" if summary.best_streak == 1 else "s"),
        f"Today: {'done' if summary.done_today else 'not done'}",
        f"Total check-ins: {summary.total_checkins}",
    ]
    if habit.reminder_time:
        lines.append(f"Reminder: {habit.reminder_time} every day")
    if habit.details:
        lines.append(f"Details: {habit.details}")
    return "\n".join(lines)


class HabitStore:
    """SQLite persistence for habits and their daily check-ins."""

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
            raise HabitError(f"Could not open {self.path}: {exc}") from exc

    def add(self, title, details="", reminder_time=""):
        title = " ".join(str(title or "").split()).strip()
        if not title:
            raise HabitError("a habit needs a name")
        if len(title) > 200:
            raise HabitError("that habit name is too long")
        reminder_time = _clock(reminder_time)
        now = _now()
        try:
            with self._lock:
                cursor = self._db.execute(
                    "INSERT INTO habits"
                    " (title, details, reminder_time, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        title,
                        str(details or "").strip(),
                        reminder_time,
                        _iso(now),
                        _iso(now),
                    ),
                )
                self._db.commit()
                habit_id = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise HabitError(f"Could not save the habit: {exc}") from exc
        habit = self.get(habit_id)
        if habit is None:
            raise HabitError("the habit disappeared after saving")
        return habit

    def get(self, habit_id):
        try:
            with self._lock:
                row = self._db.execute(
                    f"SELECT {COLUMNS} FROM habits WHERE id = ?",
                    (int(habit_id),),
                ).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return None
        return self._habit(row) if row else None

    def list(self, limit=200):
        try:
            with self._lock:
                rows = self._db.execute(
                    f"SELECT {COLUMNS} FROM habits ORDER BY id LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        except sqlite3.Error as exc:
            raise HabitError(f"Could not list habits: {exc}") from exc
        return [self._habit(row) for row in rows]

    def count(self):
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM habits"
                ).fetchone()
        except sqlite3.Error as exc:
            raise HabitError(f"Could not count habits: {exc}") from exc
        return int(row[0]) if row else 0

    def update(self, habit_id, **fields):
        allowed = {"title", "details", "reminder_time"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise HabitError("cannot update " + ", ".join(unknown))
        habit = self.get(habit_id)
        if habit is None:
            raise HabitError("there is no habit with that id")
        values = {}
        for key, value in fields.items():
            if key == "title":
                value = " ".join(str(value or "").split()).strip()
                if not value:
                    raise HabitError("a habit needs a name")
                if len(value) > 200:
                    raise HabitError("that habit name is too long")
            elif key == "reminder_time":
                value = _clock(value)
            else:
                value = str(value or "").strip()
            values[key] = value
        values["updated_at"] = _iso(_now())
        assignments = ", ".join(f"{key} = ?" for key in values)
        try:
            with self._lock:
                self._db.execute(
                    f"UPDATE habits SET {assignments} WHERE id = ?",
                    list(values.values()) + [int(habit_id)],
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise HabitError(f"Could not update the habit: {exc}") from exc
        return self.get(habit_id)

    def delete(self, habit_id):
        habit = self.get(habit_id)
        if habit is None:
            raise HabitError("there is no habit with that id")
        try:
            with self._lock:
                self._db.execute(
                    "DELETE FROM habit_checkins WHERE habit_id = ?",
                    (int(habit_id),),
                )
                self._db.execute(
                    "DELETE FROM habits WHERE id = ?", (int(habit_id),)
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise HabitError(f"Could not delete the habit: {exc}") from exc
        return habit

    def check_in(self, habit_id, day=None):
        habit = self.get(habit_id)
        if habit is None:
            raise HabitError("there is no habit with that id")
        checkin_date = _today(day)
        now = _now()
        try:
            with self._lock:
                self._db.execute(
                    "INSERT OR IGNORE INTO habit_checkins"
                    " (habit_id, checkin_date, created_at) VALUES (?, ?, ?)",
                    (int(habit_id), checkin_date.isoformat(), _iso(now)),
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise HabitError(f"Could not save the check-in: {exc}") from exc
        return self.summary(habit_id, checkin_date)

    def undo(self, habit_id, day=None):
        habit = self.get(habit_id)
        if habit is None:
            raise HabitError("there is no habit with that id")
        checkin_date = _today(day)
        try:
            with self._lock:
                self._db.execute(
                    "DELETE FROM habit_checkins"
                    " WHERE habit_id = ? AND checkin_date = ?",
                    (int(habit_id), checkin_date.isoformat()),
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise HabitError(f"Could not undo the check-in: {exc}") from exc
        return self.summary(habit_id, checkin_date)

    def summary(self, habit_id, today=None):
        habit = self.get(habit_id)
        if habit is None:
            raise HabitError("there is no habit with that id")
        today = _today(today)
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT checkin_date FROM habit_checkins"
                    " WHERE habit_id = ? ORDER BY checkin_date",
                    (int(habit_id),),
                ).fetchall()
        except sqlite3.Error as exc:
            raise HabitError(f"Could not read check-ins: {exc}") from exc
        days = []
        for row in rows:
            try:
                days.append(date.fromisoformat(row[0]))
            except ValueError:
                continue
        streak, best = _streaks(days, today)
        return HabitSummary(
            habit=habit,
            streak=streak,
            best_streak=best,
            done_today=today in set(days),
            total_checkins=len(days),
        )

    def close(self):
        try:
            with self._lock:
                self._db.close()
        except sqlite3.Error:
            log.debug("Habit store close failed", exc_info=True)

    @staticmethod
    def _habit(row):
        keys = tuple(column.strip() for column in COLUMNS.split(","))
        return Habit(**dict(zip(keys, row)))


class HabitService:
    """Habit rules plus daily reminder scheduling through the timer service."""

    def __init__(self, config, timers=None):
        self._config = config
        self._timers = timers
        self._store = HabitStore(config.resolved_db)
        self.refresh_reminders()

    def add(self, title, details="", reminder_time=""):
        if self._store.count() >= self._config.max_habits:
            raise HabitError(
                f"the habit limit of {self._config.max_habits} is reached"
            )
        habit = self._store.add(
            title, details=details, reminder_time=reminder_time
        )
        try:
            self._schedule(habit)
        except Exception:
            self._store.delete(habit.id)
            raise
        return self.summary(habit.id)

    def get(self, habit_id):
        return self._store.get(habit_id)

    def summary(self, habit_id, today=None):
        return self._store.summary(habit_id, today)

    def list(self, today=None):
        today = _today(today)
        return [
            self._store.summary(habit.id, today)
            for habit in self._store.list(self._config.max_habits)
        ]

    def update(self, habit_id, **fields):
        old = self._store.get(habit_id)
        if old is None:
            raise HabitError("there is no habit with that id")
        self._cancel_reminder(old)
        try:
            habit = self._store.update(habit_id, **fields)
            self._schedule(habit)
        except Exception:
            current = self._store.get(habit_id)
            if current is not None:
                try:
                    self._schedule(current)
                except Exception:
                    log.debug("Could not restore habit reminder", exc_info=True)
            raise
        return self.summary(habit_id)

    def delete(self, habit_id):
        habit = self._store.get(habit_id)
        if habit is None:
            raise HabitError("there is no habit with that id")
        self._cancel_reminder(habit)
        return self._store.delete(habit_id)

    def check_in(self, habit_id, day=None):
        return self._store.check_in(habit_id, day)

    def undo(self, habit_id, day=None):
        return self._store.undo(habit_id, day)

    def refresh_reminders(self):
        """Schedule a daily reminder for each habit that lacks one."""
        if self._timers is None:
            return
        try:
            habits = self._store.list(self._config.max_habits)
        except HabitError:
            return
        scheduled = {
            (entry.source, entry.source_id)
            for entry in self._timers.list()
            if entry.kind == "reminder"
        }
        for habit in habits:
            if not habit.reminder_time:
                continue
            if ("habit", habit.id) in scheduled:
                continue
            try:
                self._schedule(habit)
            except Exception as exc:
                log.warning(
                    "Could not schedule habit %s reminder: %s", habit.id, exc
                )

    def _schedule(self, habit):
        if self._timers is None or not habit.reminder_time:
            return None
        hour, minute = (int(part) for part in habit.reminder_time.split(":"))
        now = _now()
        due = datetime.combine(now.date(), time(hour, minute))
        if due <= now:
            due += timedelta(days=1)
        try:
            return self._timers.add_reminder(
                due,
                habit.title,
                source="habit",
                source_id=habit.id,
                repeat="daily",
            )
        except Exception as exc:
            raise HabitError(f"could not schedule reminder: {exc}") from exc

    def _cancel_reminder(self, habit):
        if self._timers is None:
            return 0
        return self._timers.cancel_source("habit", habit.id)

    def close(self):
        self._store.close()
