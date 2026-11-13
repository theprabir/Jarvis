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
from datetime import datetime, timedelta, timezone
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
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  topics TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes (created_at);
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
    " created_at, updated_at, expires_at, source, confidence, subject,"
    " superseded_by"
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


def _normalise_text(text):
  """Lowercase words for a punctuation-insensitive transcript comparison."""
  return " ".join(WORD.findall(str(text or "").lower()))


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
      "source",
      "confidence",
      "subject",
      "superseded_by",
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
      self._migrate()
      try:
        self._db.executescript(FTS_SCHEMA)
        self._fts = True
      except sqlite3.Error:
        log.info("SQLite FTS5 unavailable; memory search uses scan scoring")
      self._purge(now=_now())
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise MemoryStoreError(f"Could not open {self.path}: {exc}") from exc

  def _migrate(self):
    """Add columns introduced after the first memory release."""
    columns = {
        row[1] for row in self._db.execute("PRAGMA table_info(memories)")
    }
    additions = {
        "source": "TEXT NOT NULL DEFAULT 'explicit'",
        "confidence": "REAL NOT NULL DEFAULT 1.0",
        "subject": "TEXT",
        "superseded_by": "INTEGER",
    }
    for name, ddl in additions.items():
      if name not in columns:
        self._db.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl}")

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
      source="explicit",
      confidence=1.0,
      subject=None,
  ):
    """Save a memory and return ``(id, created)``.

    An identical persistent memory is kept once: saving it again refreshes its
    timestamp instead of duplicating it. When ``subject`` names a stable user
    attribute (for example ``name``), an older memory about the same attribute
    is expired and linked to the new one instead of conflicting with it.
    """
    text = " ".join(str(content or "").split())[:MAX_CONTENT_CHARS]
    if not text:
      raise MemoryStoreError("a memory needs some content")
    name = _category(category)
    persistent = 0 if temporary else 1
    origin = str(source or "explicit").strip().lower()
    if origin not in {"explicit", "learned", "episodic"}:
      origin = "explicit"
    score = max(0.0, min(1.0, float(confidence)))
    key = " ".join(str(subject or "").split()).lower() or None
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
              "UPDATE memories SET updated_at = ?,"
              " importance = MAX(importance, ?),"
              " confidence = MAX(confidence, ?) WHERE id = ?",
              (now, max(1, int(importance)), score, existing[0]),
          )
          self._db.commit()
          return int(existing[0]), False
      cursor = self._db.execute(
          "INSERT INTO memories"
          " (category, content, importance, persistent, created_at,"
          " updated_at, expires_at, source, confidence, subject)"
          " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          (name, text, max(1, int(importance)), persistent, now, now,
           expires_at, origin, score, key),
      )
      new_id = int(cursor.lastrowid)
      if persistent and key:
        self._supersede(key, text, new_id, now)
      self._db.commit()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not save the memory: {exc}") from exc
    return new_id, True

  def _supersede(self, subject, content, new_id, now):
    """Expire the active memory about the same subject, keeping its history."""
    row = self._db.execute(
        "SELECT id FROM memories WHERE subject = ? AND persistent = 1"
        " AND (expires_at IS NULL OR expires_at > ?)"
        " AND lower(content) <> lower(?) ORDER BY id DESC LIMIT 1",
        (subject, now, content),
    ).fetchone()
    if row:
      self._db.execute(
          "UPDATE memories SET expires_at = ?, superseded_by = ?"
          " WHERE id = ?",
          (now, new_id, row[0]),
      )
      return int(row[0])
    return None

  def find_by_subject(self, subject):
    """Return the active memory for a subject such as ``name``."""
    key = " ".join(str(subject or "").split()).lower()
    if not key:
      return None
    try:
      row = self._db.execute(
          f"SELECT {SELECT_COLUMNS} FROM memories WHERE subject = ?"
          " AND persistent = 1 AND (expires_at IS NULL OR expires_at > ?)"
          " ORDER BY id DESC LIMIT 1",
          (key, _now()),
      ).fetchone()
    except sqlite3.Error:
      return None
    return _row_dict(row) if row else None

  def history(self, subject):
    """Return every memory ever saved for a subject, oldest first."""
    key = " ".join(str(subject or "").split()).lower()
    if not key:
      return []
    try:
      rows = self._db.execute(
          f"SELECT {SELECT_COLUMNS} FROM memories WHERE subject = ?"
          " ORDER BY id ASC",
          (key,),
      ).fetchall()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not read memory history: {exc}") from exc
    return [_row_dict(row) for row in rows]

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

  def list(self, category=None, limit=20, source=None):
    """Return the newest memories, optionally of one category or source."""
    query = f"SELECT {SELECT_COLUMNS} FROM memories WHERE 1 = 1"
    params = []
    if category:
      query += " AND category = ?"
      params.append(_category(category))
    if source:
      query += " AND source = ?"
      params.append(str(source).strip().lower())
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
    """Return memories matching the query, ranked by relevance."""
    terms = _terms(query)
    if not terms:
      return self.list(limit=limit)
    if self._fts:
      try:
        rows = self._search_fts(terms, max(1, int(limit)) * 4)
        return self._rerank(rows, terms, limit)
      except sqlite3.Error:
        log.debug("FTS query failed; using scan scoring", exc_info=True)
    return self._search_scan(terms, limit)

  def _search_fts(self, terms, limit):
    match = " OR ".join(f'"{term}"' for term in terms)
    rows = self._db.execute(
        "SELECT m.id, m.category, m.content, m.importance, m.persistent,"
        " m.created_at, m.updated_at, m.expires_at, m.source,"
        " m.confidence, m.subject, m.superseded_by"
        " FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid"
        f" WHERE memories_fts MATCH ?"
        f" AND (m.expires_at IS NULL OR m.expires_at > ?)"
        f" ORDER BY bm25(memories_fts) LIMIT ?",
        (match, _now(), max(1, int(limit))),
    ).fetchall()
    return rows

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
      if any(term in haystack for term in terms):
        scored.append((self._relevance(item, terms), item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _score, item in scored[: max(1, int(limit))]]

  def _rerank(self, rows, terms, limit):
    items = [_row_dict(row) for row in rows]
    items.sort(key=lambda item: self._relevance(item, terms), reverse=True)
    return items[: max(1, int(limit))]

  def _relevance(self, item, terms):
    """Combine term overlap, importance, recency and category hints."""
    haystack = set(_terms(f"{item['category']} {item['content']}"))
    overlap = sum(1 for term in set(terms) if term in haystack)
    score = overlap * 2.0
    score += float(item.get("importance") or 1) * 0.5
    score += self._recency(item.get("updated_at"))
    if item.get("category") == "preference":
      score += 0.4
    lowered = {term.lower() for term in terms}
    if lowered & {"prefer", "preference", "like", "favorite", "favourite"}:
      if item.get("category") == "preference":
        score += 0.5
    return score

  @staticmethod
  def _recency(value):
    try:
      stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
      return 0.0
    if stamp.tzinfo is None:
      stamp = stamp.replace(tzinfo=timezone.utc)
    days = max(
        0.0,
        (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0,
    )
    return max(0.0, 1.0 - days / 90.0)

  def ranked(self, query=None, limit=6):
    """Return active persistent memories ranked for a context block."""
    try:
      rows = self._db.execute(
          f"SELECT {SELECT_COLUMNS} FROM memories"
          " WHERE persistent = 1"
          " AND (expires_at IS NULL OR expires_at > ?)"
          " ORDER BY id DESC LIMIT 200",
          (_now(),),
      ).fetchall()
    except sqlite3.Error as exc:
      log.warning("Could not rank memories: %s", exc)
      return []
    terms = _terms(query)
    items = [_row_dict(row) for row in rows]
    items.sort(key=lambda item: self._relevance(item, terms), reverse=True)
    return items[: max(1, int(limit))]

  def context_block(
      self,
      query=None,
      limit=6,
      episode_limit=2,
      max_chars=700,
      exclude_text="",
  ):
    """Reconstruct prompt context from semantic and episodic memory.

    ``exclude_text`` lets callers omit facts already present in the current
    transcript. Repeating them costs prompt tokens on every turn without
    adding information.
    """
    memories = self.ranked(query, limit)
    episodes = (
        self.recent_episodes(episode_limit, query)
        if int(episode_limit) > 0
        else []
    )
    if exclude_text:
      transcript = _normalise_text(exclude_text)
      memories = [
          item for item in memories
          if _normalise_text(item["content"]) not in transcript
      ]
    if not memories and not episodes:
      return ""
    lines = []
    used = 0
    if memories:
      lines.append("Known about the user:")
      used = len(lines[0])
      for item in memories:
        line = f"- {item['content']}"
        if used + len(line) + 1 > max_chars:
          break
        lines.append(line)
        used += len(line) + 1
    if episodes:
      header = "Recent events:"
      if used + len(header) + 1 <= max_chars:
        lines.append(header)
        used += len(header) + 1
        for item in episodes:
          line = f"- {item['created_at'][:10]}: {item['summary']}"
          if used + len(line) + 1 > max_chars:
            break
          lines.append(line)
          used += len(line) + 1
    return "\n".join(lines)

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
          " ORDER BY CASE category WHEN 'preference' THEN 0 ELSE 1 END,"
          " importance DESC, id DESC"
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

  # -- episodic memory --------------------------------------------------

  def add_episode(
      self,
      session_id,
      summary,
      topics="",
      ttl_days=30,
      max_episodes=500,
  ):
    """Store one expiring episodic summary and prune older entries."""
    text = " ".join(str(summary or "").split())[:300]
    if not text:
      raise MemoryStoreError("an episode needs a summary")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=max(1, int(ttl_days)))
    created = now.isoformat(timespec="seconds")
    try:
      cursor = self._db.execute(
          "INSERT INTO episodes"
          " (session_id, summary, topics, created_at, expires_at)"
          " VALUES (?, ?, ?, ?, ?)",
          (
              str(session_id or ""),
              text,
              " ".join(str(topics or "").split())[:160],
              created,
              expires.isoformat(timespec="seconds"),
          ),
      )
      self._db.execute(
          "DELETE FROM episodes WHERE expires_at <= ?", (created,)
      )
      self._db.execute(
          "DELETE FROM episodes WHERE id NOT IN"
          " (SELECT id FROM episodes ORDER BY id DESC LIMIT ?)",
          (max(1, int(max_episodes)),),
      )
      self._db.commit()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not save the episode: {exc}") from exc
    return int(cursor.lastrowid)

  def recent_episodes(self, limit=3, query=None):
    """Return active episodes, newest or best matching first."""
    try:
      rows = self._db.execute(
          "SELECT id, session_id, summary, topics, created_at, expires_at"
          " FROM episodes WHERE expires_at > ?"
          " ORDER BY id DESC LIMIT 50",
          (_now(),),
      ).fetchall()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not read episodes: {exc}") from exc
    keys = ("id", "session_id", "summary", "topics", "created_at",
            "expires_at")
    items = [dict(zip(keys, row)) for row in rows]
    terms = _terms(query)
    if terms:
      def score(item):
        haystack = set(_terms(f"{item['summary']} {item['topics']}"))
        return sum(1 for term in set(terms) if term in haystack)

      items.sort(key=lambda item: (score(item), item["id"]), reverse=True)
    return items[: max(1, int(limit))]

  def episode_count(self):
    try:
      return int(
          self._db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
      )
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not count episodes: {exc}") from exc

  def clear_episodes(self):
    try:
      cursor = self._db.execute("DELETE FROM episodes")
      self._db.commit()
    except sqlite3.Error as exc:
      raise MemoryStoreError(f"Could not clear episodes: {exc}") from exc
    return cursor.rowcount

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
    self._db.execute("DELETE FROM episodes WHERE expires_at <= ?", (now,))
