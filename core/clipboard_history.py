"""Bounded local clipboard history with lexical search."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


class ClipboardError(Exception):
    """Raised when clipboard history cannot be saved or read."""


@dataclass(frozen=True)
class ClipboardEntry:
    id: int
    content: str
    source: str
    created_at: str


def _now():
    return datetime.now().replace(microsecond=0)


def _iso(value):
    return value.isoformat(timespec="seconds")


def _preview(text, limit=180):
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 3].rstrip() + "..."


def format_entry(entry, detail=False):
    stamp = entry.created_at.replace("T", " ")[:16]
    text = f"[{entry.id}] {stamp} ({entry.source or 'clipboard'})"
    if detail:
        return text + f"\n{entry.content}"
    return text + f" — {_preview(entry.content)}"


class ClipboardHistoryService:
    """SQLite clipboard history with count, size and age retention."""

    def __init__(self, config):
        self._config = config
        self._lock = threading.RLock()
        self.path = Path(config.resolved_db)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=10.0
            )
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS clipboard_entries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  content TEXT NOT NULL,
                  source TEXT NOT NULL DEFAULT 'clipboard',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_clipboard_created
                  ON clipboard_entries (created_at);
                """
            )
            self._db.commit()
        except (OSError, sqlite3.Error) as exc:
            raise ClipboardError(f"Could not open {self.path}: {exc}") from exc

    def add(self, content, source="clipboard"):
        text = str(content or "")
        if not text.strip():
            raise ClipboardError("there is no text to save")
        if len(text) > self._config.max_chars:
            raise ClipboardError(
                f"clipboard text is too long; the limit is"
                f" {self._config.max_chars} characters"
            )
        now = _now()
        try:
            with self._lock:
                cursor = self._db.execute(
                    "INSERT INTO clipboard_entries"
                    " (content, source, created_at) VALUES (?, ?, ?)",
                    (text, str(source or "clipboard").strip(), _iso(now)),
                )
                self._db.commit()
                entry_id = int(cursor.lastrowid)
                self._prune(now)
        except sqlite3.Error as exc:
            raise ClipboardError(f"Could not save clipboard history: {exc}") from exc
        return self.get(entry_id)

    def get(self, entry_id):
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT id, content, source, created_at"
                    " FROM clipboard_entries WHERE id = ?",
                    (int(entry_id),),
                ).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return None
        return self._entry(row) if row else None

    def list(self, limit=None):
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, content, source, created_at"
                    " FROM clipboard_entries ORDER BY id DESC LIMIT ?",
                    (max(1, int(limit or self._config.search_limit)),),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ClipboardError(f"Could not list clipboard history: {exc}") from exc
        return [self._entry(row) for row in rows]

    def search(self, query, limit=None):
        text = " ".join(str(query or "").split())
        if not text:
            raise ClipboardError("a clipboard search needs a word")
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, content, source, created_at"
                    " FROM clipboard_entries WHERE content LIKE ?"
                    " ORDER BY id DESC LIMIT ?",
                    (
                        f"%{text}%",
                        max(1, int(limit or self._config.search_limit)),
                    ),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ClipboardError(f"Could not search clipboard history: {exc}") from exc
        return [self._entry(row) for row in rows]

    def delete(self, entry_id):
        entry = self.get(entry_id)
        if entry is None:
            raise ClipboardError("there is no clipboard entry with that id")
        try:
            with self._lock:
                self._db.execute(
                    "DELETE FROM clipboard_entries WHERE id = ?",
                    (int(entry_id),),
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise ClipboardError(f"Could not delete clipboard entry: {exc}") from exc
        return entry

    def clear(self):
        try:
            with self._lock:
                count = self._db.execute(
                    "SELECT COUNT(*) FROM clipboard_entries"
                ).fetchone()[0]
                self._db.execute("DELETE FROM clipboard_entries")
                self._db.commit()
        except sqlite3.Error as exc:
            raise ClipboardError(f"Could not clear clipboard history: {exc}") from exc
        return int(count or 0)

    def _prune(self, now):
        cutoff = _iso(now - timedelta(days=self._config.retention_days))
        self._db.execute(
            "DELETE FROM clipboard_entries WHERE created_at < ?", (cutoff,)
        )
        self._db.execute(
            "DELETE FROM clipboard_entries WHERE id NOT IN"
            " (SELECT id FROM clipboard_entries ORDER BY id DESC LIMIT ?)",
            (self._config.max_items,),
        )
        self._db.commit()

    def close(self):
        try:
            with self._lock:
                self._db.close()
        except sqlite3.Error:
            pass

    @staticmethod
    def _entry(row):
        return ClipboardEntry(
            id=row[0], content=row[1], source=row[2], created_at=row[3]
        )
