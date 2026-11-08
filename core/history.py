"""SQLite-backed conversation history.

Persists every completed exchange in the ``conversations`` table from the
specification (user message, assistant message, timestamp). This is durable
history, not long-term memory: no exchange is treated as a user preference
until feature set 4.6 adds the ``memories`` table.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  user_message TEXT NOT NULL,
  assistant_message TEXT NOT NULL,
  mode TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_session
  ON conversations (session_id, id);
"""


class HistoryError(Exception):
  """Raised when the history database cannot be read or written."""


class HistoryStore:
  """Minimal SQLite writer/reader for completed exchanges."""

  def __init__(self, path):
    self.path = Path(path)
    try:
      self.path.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(str(self.path))
      self._db.executescript(SCHEMA)
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise HistoryError(f"Could not open {self.path}: {exc}") from exc

  def add(self, session_id, user_message, assistant_message, mode):
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
      self._db.execute(
          "INSERT INTO conversations"
          " (session_id, user_message, assistant_message, mode, created_at)"
          " VALUES (?, ?, ?, ?, ?)",
          (session_id, user_message, assistant_message, mode, created_at),
      )
      self._db.commit()
    except sqlite3.Error as exc:
      raise HistoryError(f"Could not save exchange: {exc}") from exc

  def recent(self, session_id=None, limit=5):
    """Return the latest exchanges, oldest first."""
    query = (
        "SELECT user_message, assistant_message, created_at FROM conversations"
    )
    params = []
    if session_id:
      query += " WHERE session_id = ?"
      params.append(session_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
      rows = self._db.execute(query, params).fetchall()
    except sqlite3.Error as exc:
      raise HistoryError(f"Could not read history: {exc}") from exc
    return [
        {"user": user, "assistant": assistant, "created_at": created}
        for user, assistant, created in reversed(rows)
    ]

  def last_session_id(self):
    """Return the session id of the most recent exchange, if any."""
    try:
      row = self._db.execute(
          "SELECT session_id FROM conversations ORDER BY id DESC LIMIT 1"
      ).fetchone()
    except sqlite3.Error as exc:
      raise HistoryError(f"Could not read the last session: {exc}") from exc
    return row[0] if row else None

  def session_turns(self, session_id, limit=8):
    """Return the latest exchanges of one session, oldest first."""
    try:
      rows = self._db.execute(
          "SELECT user_message, assistant_message FROM conversations"
          " WHERE session_id = ? ORDER BY id DESC LIMIT ?",
          (session_id, max(1, int(limit))),
      ).fetchall()
    except sqlite3.Error as exc:
      raise HistoryError(f"Could not read the session: {exc}") from exc
    return [
        {"user": user, "assistant": assistant}
        for user, assistant in reversed(rows)
    ]

  def clear(self):
    """Delete every saved exchange."""
    try:
      self._db.execute("DELETE FROM conversations")
      self._db.commit()
    except sqlite3.Error as exc:
      raise HistoryError(f"Could not delete history: {exc}") from exc

  def count(self):
    try:
      return int(self._db.execute("SELECT COUNT(*) FROM conversations")
                 .fetchone()[0])
    except sqlite3.Error as exc:
      raise HistoryError(f"Could not count history: {exc}") from exc

  def close(self):
    try:
      self._db.close()
    except sqlite3.Error:
      log.debug("History close failed", exc_info=True)
