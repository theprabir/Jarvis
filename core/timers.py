"""Timers and persisted reminders.

Timers live in memory for the session. Reminders are stored in the shared
SQLite database and are reloaded on startup, so a reminder survives a restart.
A daemon thread checks due entries and hands a spoken notification to the
callback the application registered.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  due_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  fired INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT '',
  source_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (fired, due_at);
"""


class TimerError(Exception):
  """Raised when a timer or reminder cannot be created or read."""


@dataclass
class Scheduled:
  """One pending timer or reminder."""

  id: int
  kind: str
  label: str
  due_at: datetime
  persistent: bool = False
  source: str = ""
  source_id: int | None = None

  @property
  def remaining_s(self):
    return max(0, int((self.due_at - datetime.now()).total_seconds()))


class TimerService:
  """In-memory timers plus persisted reminders with a polling thread."""

  def __init__(self, db_path=None, notify=None, autostart=True, poll_s=0.5):
    self._lock = threading.Lock()
    self._timers = {}
    self._next_id = 1
    self._notify = notify
    self._poll_s = max(0.2, float(poll_s))
    self._stop = threading.Event()
    self._thread = None
    self._missed = []
    self._db = None
    if db_path:
      self._open(Path(db_path))
    if autostart:
      self._thread = threading.Thread(
          target=self._loop, name="jarvis-timers", daemon=True
      )
      self._thread.start()

  def _open(self, path):
    try:
      path.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(str(path), check_same_thread=False)
      self._db.executescript(SCHEMA)
      self._migrate()
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise TimerError(f"Could not open {path}: {exc}") from exc
    self._load_reminders()

  def _migrate(self):
    """Add source metadata to reminder tables created before v0.2.6."""
    columns = {
        row[1]
        for row in self._db.execute("PRAGMA table_info(reminders)").fetchall()
    }
    if "source" not in columns:
      self._db.execute(
          "ALTER TABLE reminders ADD COLUMN source TEXT NOT NULL DEFAULT ''"
      )
    if "source_id" not in columns:
      self._db.execute(
          "ALTER TABLE reminders ADD COLUMN source_id INTEGER"
      )

  def _load_reminders(self):
    try:
      rows = self._db.execute(
          "SELECT id, text, due_at, source, source_id FROM reminders"
          " WHERE fired = 0 ORDER BY due_at"
      ).fetchall()
    except sqlite3.Error as exc:
      raise TimerError(f"Could not read reminders: {exc}") from exc
    for row in rows:
      try:
        due = datetime.fromisoformat(row[2])
      except ValueError:
        due = datetime.now()
      if due <= datetime.now():
        self._db.execute(
            "UPDATE reminders SET fired = 1 WHERE id = ?", (row[0],)
        )
        self._missed.append(self._notification_text(row[3], row[1]))
        continue
      self._timers[("reminder", row[0])] = Scheduled(
          id=row[0], kind="reminder", label=row[1], due_at=due,
          persistent=True, source=row[3] or "", source_id=row[4],
      )
      self._next_id = max(self._next_id, row[0] + 1)
    if rows:
      self._db.commit()

  def set_notifier(self, callback):
    self._notify = callback

  def add_timer(self, seconds, label=""):
    seconds = int(seconds)
    if not 1 <= seconds <= 24 * 3600:
      raise TimerError("a timer must be between 1 second and 24 hours")
    with self._lock:
      timer_id = self._next_id
      self._next_id += 1
      entry = Scheduled(
          id=timer_id,
          kind="timer",
          label=(label or "").strip(),
          due_at=datetime.now() + timedelta(seconds=seconds),
      )
      self._timers[("timer", timer_id)] = entry
    return entry

  def add_reminder(self, due_at, text, source="", source_id=None):
    body = " ".join(str(text or "").split())
    if not body:
      raise TimerError("a reminder needs some text")
    if self._db is None:
      raise TimerError("reminders need a database")
    now = datetime.now()
    if due_at <= now:
      raise TimerError("that time has already passed")
    try:
      cursor = self._db.execute(
          "INSERT INTO reminders"
          " (text, due_at, created_at, fired, source, source_id)"
          " VALUES (?, ?, ?, 0, ?, ?)",
          (body, due_at.isoformat(timespec="seconds"),
           now.isoformat(timespec="seconds"), str(source or ""),
           int(source_id) if source_id is not None else None),
      )
      self._db.commit()
    except sqlite3.Error as exc:
      raise TimerError(f"Could not save the reminder: {exc}") from exc
    entry = Scheduled(
        id=int(cursor.lastrowid),
        kind="reminder",
        label=body,
        due_at=due_at,
        persistent=True,
        source=str(source or ""),
        source_id=int(source_id) if source_id is not None else None,
    )
    with self._lock:
      self._timers[("reminder", entry.id)] = entry
      self._next_id = max(self._next_id, entry.id + 1)
    return entry

  def list(self):
    with self._lock:
      entries = list(self._timers.values())
    return sorted(entries, key=lambda item: item.due_at)

  def get(self, timer_id):
    with self._lock:
      for entry in self._timers.values():
        if entry.id == timer_id:
          return entry
    return None

  def cancel(self, timer_id=None):
    """Cancel one entry by id, or the next one when no id is given."""
    with self._lock:
      entries = sorted(
          self._timers.values(), key=lambda item: item.due_at
      )
      target = None
      for entry in entries:
        if timer_id is None or entry.id == timer_id:
          target = entry
          break
      if target is None:
        return None
      self._timers.pop((target.kind, target.id), None)
    if target.persistent and self._db is not None:
      try:
        self._db.execute(
            "UPDATE reminders SET fired = 1 WHERE id = ?", (target.id,)
        )
        self._db.commit()
      except sqlite3.Error as exc:
        log.warning("Could not cancel reminder %s: %s", target.id, exc)
    return target

  def cancel_all(self):
    with self._lock:
      entries = list(self._timers.values())
      self._timers.clear()
    for entry in entries:
      if entry.persistent and self._db is not None:
        try:
          self._db.execute(
              "UPDATE reminders SET fired = 1 WHERE id = ?", (entry.id,)
          )
        except sqlite3.Error:
          log.debug("Could not cancel reminder %s", entry.id, exc_info=True)
    if entries and self._db is not None:
      try:
        self._db.commit()
      except sqlite3.Error:
        log.debug("Could not commit cancellations", exc_info=True)
    return len(entries)

  def cancel_source(self, source, source_id):
    """Cancel persisted reminders belonging to one productivity item."""
    removed = []
    with self._lock:
      for key, entry in list(self._timers.items()):
        if entry.source == source and entry.source_id == source_id:
          removed.append(entry)
          self._timers.pop(key, None)
    if removed and self._db is not None:
      try:
        for entry in removed:
          self._db.execute(
              "UPDATE reminders SET fired = 1 WHERE id = ?", (entry.id,)
          )
        self._db.commit()
      except sqlite3.Error as exc:
        log.warning("Could not cancel reminders for %s %s: %s",
                    source, source_id, exc)
    return len(removed)

  def poll(self, now=None):
    """Return due notification texts and remove those entries."""
    now = now or datetime.now()
    messages = []
    if self._missed:
      messages.extend(self._missed)
      self._missed = []
    due = []
    with self._lock:
      for key, entry in list(self._timers.items()):
        if entry.due_at <= now:
          due.append(entry)
          self._timers.pop(key, None)
    for entry in due:
      if entry.kind == "reminder":
        messages.append(
            self._notification_text(entry.source, entry.label)
        )
        if self._db is not None:
          try:
            self._db.execute(
                "UPDATE reminders SET fired = 1 WHERE id = ?", (entry.id,)
            )
            self._db.commit()
          except sqlite3.Error as exc:
            log.warning("Could not mark reminder %s fired: %s", entry.id, exc)
      elif entry.label:
        messages.append(f"Timer finished: {entry.label}.")
      else:
        messages.append("Your timer is finished, sir.")
    return messages

  @staticmethod
  def _notification_text(source, label):
    if source == "todo":
      return f"To-do: {label}."
    return f"Reminder: {label}."

  def _loop(self):
    while not self._stop.is_set():
      for message in self.poll():
        self._deliver(message)
      self._stop.wait(self._poll_s)

  def _deliver(self, message):
    if self._notify is None:
      return
    try:
      self._notify(message)
    except Exception:  # a notifier must never kill the scheduler
      log.warning("Timer notification failed", exc_info=True)

  def close(self):
    self._stop.set()
    thread = self._thread
    if thread is not None and thread.is_alive():
      thread.join(timeout=2.0)
    self._thread = None
    if self._db is not None:
      try:
        self._db.close()
      except sqlite3.Error:
        log.debug("Reminder close failed", exc_info=True)
      self._db = None
