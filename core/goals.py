"""Local goals with numeric progress and target dates.

Goals are separate from to-dos, notes and habits. They share the assistant
SQLite file but use their own ``goals`` table and need no timer or worker.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from core.productivity import parse_todo_input, parse_when

log = logging.getLogger(__name__)

ACTIVE = "active"
COMPLETED = "completed"
STATUSES = {ACTIVE, COMPLETED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '',
  target_value REAL,
  current_value REAL NOT NULL DEFAULT 0,
  unit TEXT NOT NULL DEFAULT '',
  target_date TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_goals_status_date
  ON goals (status, target_date);
"""

COLUMNS = (
    "id, title, details, target_value, current_value, unit, target_date,"
    " status, created_at, updated_at, completed_at"
)

_TARGET = re.compile(
    r"\btarget\s+(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-z%]{1,20})?\b",
    re.I,
)
_AMOUNT = re.compile(
    r"\b(?P<value>\d+(?:\.\d+)?)\s+(?P<unit>[a-z%]{1,20})\b",
    re.I,
)


class GoalError(Exception):
    """Raised when a goal cannot be created, changed or read."""


@dataclass(frozen=True)
class Goal:
    """One persisted goal."""

    id: int
    title: str
    details: str
    target_value: float | None
    current_value: float
    unit: str
    target_date: date | None
    status: str
    created_at: str
    updated_at: str
    completed_at: str

    @property
    def progress_percent(self):
        if not self.target_value or self.target_value <= 0:
            return None
        return round(self.current_value / self.target_value * 100)

    def days_left(self, today=None):
        if self.target_date is None:
            return None
        return (self.target_date - (today or date.today())).days

    def is_overdue(self, today=None):
        return (
            self.status == ACTIVE
            and self.target_date is not None
            and self.target_date < (today or date.today())
        )


def _now():
    return datetime.now().replace(microsecond=0)


def _iso(value):
    return value.isoformat(timespec="seconds") if value else ""


def _number(value):
    """Format a stored number without a trailing .0."""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _positive(value, label="target"):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise GoalError(f"{label} must be a number") from None
    if number <= 0:
        raise GoalError(f"{label} must be greater than zero")
    return number


def _date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = " ".join(str(value).split()).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
    except ValueError:
        pass
    parsed = parse_when(text, prefer_future=False)
    if parsed is None:
        raise GoalError("I couldn't understand that target date")
    return parsed.date()


def _clean_title(value):
    title = " ".join(str(value or "").split()).strip(" ,.;:-")
    title = re.sub(r"^(?:to|that|for)\s+", "", title, flags=re.I)
    return title.strip(" ,.;:-")


def parse_goal_input(text):
    """Parse a goal title, optional numeric target and target date."""
    parsed_todo = parse_todo_input(text)
    title = parsed_todo.get("title", "")
    due_at = parsed_todo.get("due_at")
    target_value = None
    unit = ""

    match = _TARGET.search(title)
    if match is None:
        match = _AMOUNT.search(title)
    if match is not None:
        try:
            target_value = _positive(match.group("value"))
        except GoalError:
            target_value = None
        else:
            unit = (match.group("unit") or "").strip().lower()
            title = (title[: match.start()] + " " + title[match.end():]).strip()

    title = _clean_title(title)
    return {
        "title": title,
        "target_value": target_value,
        "unit": unit,
        "target_date": due_at.date().isoformat() if due_at else "",
    }


def format_goal(goal, today=None):
    """Render one compact goal line."""
    today = today or date.today()
    parts = [f"[{goal.id}] {goal.title}"]
    if goal.target_value is not None:
        parts.append(
            f"{_number(goal.current_value)}/{_number(goal.target_value)}"
            f" {goal.unit}".rstrip()
            + f" ({goal.progress_percent}%)"
        )
    elif goal.current_value:
        parts.append(f"progress {_number(goal.current_value)}")
    if goal.target_date is not None:
        label = goal.target_date.strftime("%a %d %b")
        days = goal.days_left(today)
        if goal.status == COMPLETED:
            parts.append(f"target {label}")
        elif days < 0:
            parts.append(f"due {label} (overdue by {-days} days)")
        elif days == 0:
            parts.append(f"due {label} (today)")
        else:
            parts.append(f"due {label} ({days} days left)")
    if goal.status != ACTIVE:
        parts.append(goal.status)
    return " - ".join(parts)


def format_goal_detail(goal, today=None):
    """Render full goal detail."""
    today = today or date.today()
    lines = [f"Goal #{goal.id}: {goal.title}", f"Status: {goal.status}"]
    if goal.target_value is not None:
        lines.append(
            f"Progress: {_number(goal.current_value)}/"
            f"{_number(goal.target_value)} {goal.unit}".rstrip()
            + f" ({goal.progress_percent}%)"
        )
    elif goal.current_value:
        lines.append(f"Progress: {_number(goal.current_value)}")
    else:
        lines.append("Progress: no numeric target")
    if goal.target_date is not None:
        days = goal.days_left(today)
        label = goal.target_date.strftime("%A %d %B %Y")
        if goal.status == ACTIVE and days < 0:
            label += f" (overdue by {-days} days)"
        elif goal.status == ACTIVE and days == 0:
            label += " (today)"
        elif goal.status == ACTIVE:
            label += f" ({days} days left)"
        lines.append(f"Target date: {label}")
    if goal.details:
        lines.append(f"Details: {goal.details}")
    if goal.completed_at:
        lines.append(f"Completed: {goal.completed_at[:10]}")
    return "\n".join(lines)


class GoalStore:
    """SQLite persistence for goals."""

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
            raise GoalError(f"Could not open {self.path}: {exc}") from exc

    def add(
        self,
        title,
        details="",
        target_value=None,
        current_value=0,
        unit="",
        target_date=None,
    ):
        title = " ".join(str(title or "").split()).strip()
        if not title:
            raise GoalError("a goal needs a title")
        if len(title) > 200:
            raise GoalError("that goal title is too long")
        target_value = _positive(target_value)
        try:
            current_value = float(current_value or 0)
        except (TypeError, ValueError):
            raise GoalError("current progress must be a number") from None
        if current_value < 0:
            raise GoalError("current progress cannot be negative")
        unit = " ".join(str(unit or "").split()).strip()[:20]
        target_date = _date(target_date)
        now = _now()
        try:
            with self._lock:
                cursor = self._db.execute(
                    "INSERT INTO goals"
                    " (title, details, target_value, current_value, unit,"
                    " target_date, status, created_at, updated_at,"
                    " completed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')",
                    (
                        title,
                        str(details or "").strip(),
                        target_value,
                        current_value,
                        unit,
                        target_date.isoformat() if target_date else "",
                        ACTIVE,
                        _iso(now),
                        _iso(now),
                    ),
                )
                self._db.commit()
                goal_id = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise GoalError(f"Could not save the goal: {exc}") from exc
        goal = self.get(goal_id)
        if goal is None:
            raise GoalError("the goal disappeared after saving")
        return goal

    def get(self, goal_id):
        try:
            with self._lock:
                row = self._db.execute(
                    f"SELECT {COLUMNS} FROM goals WHERE id = ?",
                    (int(goal_id),),
                ).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return None
        return self._goal(row) if row else None

    def list(self, status=None, limit=200):
        query = f"SELECT {COLUMNS} FROM goals"
        params = []
        if status in STATUSES:
            query += " WHERE status = ?"
            params.append(status)
        query += (
            " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,"
            " CASE WHEN target_date = '' THEN 1 ELSE 0 END, target_date, id"
        )
        query += " LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            with self._lock:
                rows = self._db.execute(query, params).fetchall()
        except sqlite3.Error as exc:
            raise GoalError(f"Could not list goals: {exc}") from exc
        return [self._goal(row) for row in rows]

    def count(self):
        try:
            with self._lock:
                row = self._db.execute("SELECT COUNT(*) FROM goals").fetchone()
        except sqlite3.Error as exc:
            raise GoalError(f"Could not count goals: {exc}") from exc
        return int(row[0]) if row else 0

    def update(self, goal_id, **fields):
        allowed = {
            "title",
            "details",
            "target_value",
            "current_value",
            "unit",
            "target_date",
            "status",
            "completed_at",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise GoalError("cannot update " + ", ".join(unknown))
        goal = self.get(goal_id)
        if goal is None:
            raise GoalError("there is no goal with that id")
        values = {}
        for key, value in fields.items():
            if key == "title":
                value = " ".join(str(value or "").split()).strip()
                if not value:
                    raise GoalError("a goal needs a title")
                if len(value) > 200:
                    raise GoalError("that goal title is too long")
            elif key == "target_value":
                value = _positive(value)
            elif key == "current_value":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise GoalError("current progress must be a number") from None
                if value < 0:
                    raise GoalError("current progress cannot be negative")
            elif key == "target_date":
                value = _date(value)
                value = value.isoformat() if value else ""
            elif key == "status":
                if value not in STATUSES:
                    raise GoalError("unknown goal status")
            else:
                value = str(value or "").strip()
            values[key] = value
        values["updated_at"] = _iso(_now())
        assignments = ", ".join(f"{key} = ?" for key in values)
        try:
            with self._lock:
                self._db.execute(
                    f"UPDATE goals SET {assignments} WHERE id = ?",
                    list(values.values()) + [int(goal_id)],
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise GoalError(f"Could not update the goal: {exc}") from exc
        return self.get(goal_id)

    def delete(self, goal_id):
        goal = self.get(goal_id)
        if goal is None:
            raise GoalError("there is no goal with that id")
        try:
            with self._lock:
                self._db.execute(
                    "DELETE FROM goals WHERE id = ?", (int(goal_id),)
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise GoalError(f"Could not delete the goal: {exc}") from exc
        return goal

    def close(self):
        try:
            with self._lock:
                self._db.close()
        except sqlite3.Error:
            log.debug("Goal store close failed", exc_info=True)

    @staticmethod
    def _goal(row):
        keys = tuple(column.strip() for column in COLUMNS.split(","))
        data = dict(zip(keys, row))
        data["target_date"] = (
            date.fromisoformat(data["target_date"])
            if data["target_date"]
            else None
        )
        return Goal(**data)


class GoalService:
    """Goal rules and progress updates."""

    def __init__(self, config):
        self._config = config
        self._store = GoalStore(config.resolved_db)

    def add(
        self,
        title,
        details="",
        target_value=None,
        unit="",
        target_date=None,
    ):
        if self._store.count() >= self._config.max_goals:
            raise GoalError(
                f"the goal limit of {self._config.max_goals} is reached"
            )
        return self._store.add(
            title,
            details=details,
            target_value=target_value,
            unit=unit,
            target_date=target_date,
        )

    def get(self, goal_id):
        return self._store.get(goal_id)

    def list(self, status=None, limit=None):
        return self._store.list(
            status=status, limit=limit or self._config.max_goals
        )

    def update(self, goal_id, **fields):
        return self._store.update(goal_id, **fields)

    def progress(self, goal_id, value, add=False):
        goal = self._store.get(goal_id)
        if goal is None:
            raise GoalError("there is no goal with that id")
        try:
            amount = float(value)
        except (TypeError, ValueError):
            raise GoalError("progress must be a number") from None
        if amount < 0:
            raise GoalError("progress cannot be negative")
        current = goal.current_value + amount if add else amount
        return self._store.update(goal_id, current_value=current)

    def complete(self, goal_id):
        goal = self._store.get(goal_id)
        if goal is None:
            raise GoalError("there is no goal with that id")
        if goal.status == COMPLETED:
            return goal
        return self._store.update(
            goal_id, status=COMPLETED, completed_at=_iso(_now())
        )

    def reopen(self, goal_id):
        goal = self._store.get(goal_id)
        if goal is None:
            raise GoalError("there is no goal with that id")
        return self._store.update(
            goal_id, status=ACTIVE, completed_at=""
        )

    def delete(self, goal_id):
        return self._store.delete(goal_id)

    def close(self):
        self._store.close()
