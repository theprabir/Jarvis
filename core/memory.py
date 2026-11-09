"""Explicit memory: saved preferences, facts, paths, folders and tasks.

Feature set 4.6 stores only what the user asks to remember. Nothing is
promoted from a conversation automatically (that is feature set 4.8). Memories
live in the same SQLite file as conversation history but in their own tables;
search uses FTS5 when the bundled SQLite provides it and falls back to a
scored scan otherwise.

Temporary memories are rows with ``persistent = 0``. They survive while the
process runs and are deleted the next time the store opens, so they never
accumulate on disk.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CATEGORIES = (
    "preference",
    "fact",
    "path",
    "folder",
    "website",
    "task",
    "general",
)
USAGE_KINDS = ("app", "folder", "website")
MAX_CONTENT_CHARS = 500
DEFAULT_CATEGORY = "fact"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL,
  content TEXT NOT NULL,
  importance INTEGER NOT NULL DEFAULT 1,
  persistent INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories (category);
CREATE TABLE IF NOT EXISTS memory_usage (
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  uses INTEGER NOT NULL DEFAULT 1,
  last_used_at TEXT NOT NULL,
  PRIMARY KEY (kind, value)
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content,
  category,
  content='memories',
  content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content, category)
  VALUES (new.id, new.content, new.category);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, category)
  VALUES ('delete', old.id, old.content, old.category);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, category)
  VALUES ('delete', old.id, old.content, old.category);
  INSERT INTO memories_fts(rowid, content, category)
  VALUES (new.id, new.content, new.category);
END;
"""

WORD = re.compile(r"[a-z0-9]+")
URL = re.compile(r"^(?:https?://|www\.)", re.I)
WINDOWS_PATH = re.compile(r"^(?:[a-z]:[\\/]|\\\\|/)", re.I)
FOLDER_WORDS = ("folder", "directory", "dir ")
PREFERENCE_STARTS = (
    "my ",
    "i prefer",
    "i like",
    "i use",
    "i always",
    "i usually",
)

SELECT_COLUMNS = (
    "id, category, content, importance, persistent,"
    " created_at, updated_at, expires_at"
)


class MemoryStoreError(Exception):
  """Raised when the memory database cannot be read or written."""


def _now():
  return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(content):
  """Infer the memory category from what the user asked to remember."""
  text = " ".join(str(content or "").split())
  lowered = text.lower()
  if URL.match(text):
    return "website"
  if WINDOWS_PATH.match(text) or re.search(r"\b[a-z]:[\\/]", lowered):
    return "folder" if any(
        word in lowered for word in FOLDER_WORDS
    ) else "path"
  if any(word in lowered for word in FOLDER_WORDS):
    return "folder"
  if lowered.startswith(PREFERENCE_STARTS) and any(
      marker in lowered for marker in (" is ", " are ", "prefer", "like", "use ")
  ):
    return "preference"
  if any(marker in lowered for marker in ("todo", "to-do", "task")):
    return "task"
  return DEFAULT_CATEGORY


def _category(value):
  name = str(value or "").strip().lower()
  return name if name in CATEGORIES else DEFAULT_CATEGORY


def _terms(text):
  return [word for word in WORD.findall(str(text or "").lower())
          if len(word) > 1]


def _row_dict(row):
  keys = (
      "id",
      "category",
      "content",
      "importance",
      "persistent",
      "created_at",
      "updated_at",
      "expires_at",
  )
  item = dict(zip(keys, row))
  item["temporary"] = not item.pop("persistent")
  return item


class MemoryStore:
  """Minimal SQLite store for explicit memories and usage counts."""

  def __init__(self, path):
    self.path = Path(path)
    self._fts = False
    try:
      self.path.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(str(self.path))
      self._db.executescript(SCHEMA)
      try:
        self._db.executescript(FTS_SCHEMA)
        self._fts = True
      except sqlite3.Error:
        log.info("SQLite FTS5 unavailable; memory search uses scan scoring")
      self._purge(now=_now())
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise MemoryStoreError(f"Could not open {self.path}: {exc}") from exc

  def close(self):
    try:
      self._db.close()
    except sqlite3.Error:
      log.debug("Memory close failed", exc_info=True)

  # -- writing ----------------------------------------------------------

  def add(
      self,
      category,
      content,
      temporary=False,
      importance=1,
      expires_at=None,
  ):
    """Save a memory and return ``(id, created)``.

    An identical persistent memory is kept once: saving it again refreshes its
    timestamp instead of duplicating it.
    """
    text = " ".join(str(content or "").split())[:MAX_CONTENT_CHARS]
    if not text:
      raise MemoryStoreError("a memory needs some content")
    name = _category(category)
    persistent = 0 if temporary else 1
    now = _now()
    try:
      if not temporary:
        existing = self._db.execute(
            "SELECT id FROM memories WHERE category = ?"
            " AND lower(content) = lower(?) AND persistent = 1 LIMIT 1",
            (name, text),
        ).fetchone()
        if existing:
          self._db.execute(
              "UPDATE memories SET updated_at = ? WHERE id = ?",
              (now, existing[0]),
          )
          self._db.commit()
          return int(existing[0]), False
      cursor = self._db.execute(
          "INSERT INTO memories"
          " (category, content, importance, persistent, created_at,"
          " updated_at, expires_at)"
          " VALUES (?, ?, ?, ?, ?, ?, ?)",
          (name, text, max(1, int(importance)), persistent, now, now,
           expires_at),
      )
      self._db.commit()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not save the memory: {exc}") from exc
    return int(cursor.lastrowid), True

  def update(self, memory_id, content=None, category=None):
    """Change a memory's text or category; return False when it is gone."""
    current = self.get(memory_id)
    if current is None:
      return False
    text = current["content"]
    if content is not None:
      text = " ".join(str(content).split())[:MAX_CONTENT_CHARS]
      if not text:
        raise MemoryStoreError("a memory needs some content")
    name = _category(category) if category is not None else current["category"]
    try:
      self._db.execute(
          "UPDATE memories SET content = ?, category = ?, updated_at = ?"
          " WHERE id = ?",
          (text, name, _now(), int(memory_id)),
      )
      self._db.commit()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not update the memory: {exc}") from exc
    return True

  def delete(self, memory_id):
    try:
      cursor = self._db.execute(
          "DELETE FROM memories WHERE id = ?", (int(memory_id),)
      )
      self._db.commit()
    except (sqlite3.Error, ValueError) as exc:
      raise MemoryStoreError(f"Could not delete the memory: {exc}") from exc
    return cursor.rowcount > 0

  def clear(self, temporary_only=False):
    """Delete remembered items (all of them, or only temporary ones)."""
    query = "DELETE FROM memories"
    if temporary_only:
      query += " WHERE persistent = 0"
    try:
      cursor = self._db.execute(query)
      self._db.commit()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not clear memory: {exc}") from exc
    return cursor.rowcount

  # -- reading ----------------------------------------------------------

  def get(self, memory_id):
    try:
      row = self._db.execute(
          f"SELECT {SELECT_COLUMNS} FROM memories WHERE id = ?",
          (int(memory_id),),
      ).fetchone()
    except (sqlite3.Error, ValueError):
      return None
    return _row_dict(row) if row else None

  def list(self, category=None, limit=20):
    """Return the newest memories, optionally of one category."""
    query = f"SELECT {SELECT_COLUMNS} FROM memories WHERE 1 = 1"
    params = []
    if category:
      query += " AND category = ?"
      params.append(_category(category))
    query += " AND (expires_at IS NULL OR expires_at > ?)"
    params.append(_now())
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
      rows = self._db.execute(query, params).fetchall()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not read memory: {exc}") from exc
    return [_row_dict(row) for row in rows]

  def search(self, query, limit=10):
    """Return memories matching every term, best match first."""
    terms = _terms(query)
    if not terms:
      return self.list(limit=limit)
    if self._fts:
      try:
        return self._search_fts(terms, limit)
      except sqlite3.Error:
        log.debug("FTS query failed; using scan scoring", exc_info=True)
    return self._search_scan(terms, limit)

  def _search_fts(self, terms, limit):
    match = " OR ".join(f'"{term}"' for term in terms)
    rows = self._db.execute(
        f"SELECT m.id, m.category, m.content, m.importance, m.persistent,"
        f" m.created_at, m.updated_at, m.expires_at"
        f" FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid"
        f" WHERE memories_fts MATCH ?"
        f" AND (m.expires_at IS NULL OR m.expires_at > ?)"
        f" ORDER BY bm25(memories_fts) LIMIT ?",
        (match, _now(), max(1, int(limit))),
    ).fetchall()
    return [_row_dict(row) for row in rows]

  def _search_scan(self, terms, limit):
    rows = self._db.execute(
        f"SELECT {SELECT_COLUMNS} FROM memories"
        " WHERE (expires_at IS NULL OR expires_at > ?)",
        (_now(),),
    ).fetchall()
    scored = []
    for row in rows:
      item = _row_dict(row)
      haystack = set(_terms(f"{item['category']} {item['content']}"))
      score = sum(1 for term in set(terms) if term in haystack)
      if score:
        scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1]["id"]), reverse=True)
    return [item for _score, item in scored[: max(1, int(limit))]]

  def count(self):
    try:
      return int(
          self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
      )
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not count memory: {exc}") from exc

  def prompt_block(self, limit=6, max_chars=400):
    """Format persistent memories for the system prompt, bounded in size."""
    try:
      rows = self._db.execute(
          f"SELECT {SELECT_COLUMNS} FROM memories"
          " WHERE persistent = 1"
          " AND (expires_at IS NULL OR expires_at > ?)"
          " ORDER BY CASE category WHEN 'preference' THEN 0 ELSE 1 END, id DESC"
          " LIMIT ?",
          (_now(), max(1, int(limit))),
      ).fetchall()
    except sqlite3.Error as exc:
      log.warning("Could not read memories for the prompt: %s", exc)
      return ""
    if not rows:
      return ""
    lines = []
    used = 0
    for row in rows:
      item = _row_dict(row)
      line = f"- {item['content']}"
      if used + len(line) > max_chars:
        break
      lines.append(line)
      used += len(line) + 1
    if not lines:
      return ""
    return "Known about the user:\n" + "\n".join(lines)

  # -- usage ------------------------------------------------------------

  def note_usage(self, kind, value):
    """Count one use of an application, folder or website."""
    name = str(kind or "").strip().lower()
    item = " ".join(str(value or "").split())
    if name not in USAGE_KINDS or not item:
      return
    now = _now()
    try:
      row = self._db.execute(
          "SELECT uses FROM memory_usage"
          " WHERE kind = ? AND lower(value) = lower(?)",
          (name, item),
      ).fetchone()
      if row:
        self._db.execute(
            "UPDATE memory_usage SET uses = uses + 1, last_used_at = ?,"
            " value = ? WHERE kind = ? AND lower(value) = lower(?)",
            (now, item, name, item),
        )
      else:
        self._db.execute(
            "INSERT INTO memory_usage (kind, value, uses, last_used_at)"
            " VALUES (?, ?, 1, ?)",
            (name, item, now),
        )
      self._db.commit()
    except sqlite3.Error as exc:
      log.warning("Could not record usage: %s", exc)

  def frequent(self, kind=None, limit=5):
    """Return the most used entries, most used first."""
    query = "SELECT kind, value, uses, last_used_at FROM memory_usage"
    params = []
    if kind:
      query += " WHERE kind = ?"
      params.append(str(kind).strip().lower())
    query += " ORDER BY uses DESC, last_used_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
      rows = self._db.execute(query, params).fetchall()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not read usage: {exc}") from exc
    return [
        {"kind": row[0], "value": row[1], "uses": row[2],
         "last_used_at": row[3]}
        for row in rows
    ]

  # -- maintenance ------------------------------------------------------

  def _purge(self, now):
    """Drop expired memories and any temporary rows from a previous run."""
    self._db.execute("DELETE FROM memories WHERE persistent = 0")
    self._db.execute(
        "DELETE FROM memories WHERE expires_at IS NOT NULL"
        " AND expires_at <= ?",
        (now,),
    )
