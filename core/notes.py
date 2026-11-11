"""Local notes with full-text search.

Notes are deliberately separate from productivity to-dos and memories. They
share the assistant SQLite file but use their own tables, so searching notes
never mixes in conversation or memory rows. FTS5 is used when the bundled
SQLite provides it and a scored scan is used otherwise.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes (updated_at);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title,
  content,
  content='notes',
  content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, title, content)
  VALUES (new.id, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, content)
  VALUES ('delete', old.id, old.title, old.content);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, content)
  VALUES ('delete', old.id, old.title, old.content);
  INSERT INTO notes_fts(rowid, title, content)
  VALUES (new.id, new.title, new.content);
END;
"""

COLUMNS = "id, title, content, created_at, updated_at"
FTS_COLUMNS = "n.id, n.title, n.content, n.created_at, n.updated_at"
_WORD = re.compile(r"[a-z0-9]+")


class NoteError(Exception):
  """Raised when a note cannot be created, changed or read."""


@dataclass(frozen=True)
class Note:
  """One persisted note."""

  id: int
  title: str
  content: str
  created_at: str
  updated_at: str


@dataclass(frozen=True)
class NoteHit:
  """A search result with a short surrounding excerpt."""

  note: Note
  snippet: str
  score: float
  mode: str


def _now():
  return datetime.now().isoformat(timespec="seconds")


def _truncate(text, limit):
  clean = " ".join(str(text or "").split())
  if len(clean) <= limit:
    return clean
  return clean[: max(0, limit - 3)].rstrip() + "..."


def _title_from(content):
  first_line = str(content or "").strip().splitlines()
  return _truncate(first_line[0] if first_line else "", 80)


def _clean_title(title):
  return " ".join(str(title or "").split()).strip()


def _terms(query):
  return [
      word
      for word in _WORD.findall(str(query or "").lower())
      if len(word) > 1
  ][:12]


def _snippet(content, terms, width=140):
  text = " ".join(str(content or "").split())
  if len(text) <= width:
    return text
  lowered = text.lower()
  positions = [
      position
      for position in (lowered.find(term) for term in terms)
      if position >= 0
  ]
  if not positions:
    return _truncate(text, width)
  center = min(positions)
  start = max(0, center - width // 3)
  end = min(len(text), start + width)
  excerpt = text[start:end].strip()
  if start:
    excerpt = "... " + excerpt
  if end < len(text):
    excerpt += " ..."
  return excerpt


def format_note(note, snippet=None):
  """Render one note compactly for the console and voice reply."""
  title = note.title or _title_from(note.content)
  stamp = note.updated_at.replace("T", " ")[:16]
  lines = [f"[{note.id}] {title} ({stamp})"]
  body = _truncate(snippet if snippet is not None else note.content, 240)
  if body and body != title:
    lines.append(f"  {body}")
  return "\n".join(lines)


class NoteStore:
  """SQLite note storage plus an FTS5 title/content index."""

  def __init__(self, path):
    self.path = Path(path)
    self._lock = threading.RLock()
    self._fts = False
    self._db = None
    try:
      self.path.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(
          str(self.path), check_same_thread=False, timeout=10.0
      )
      self._db.row_factory = sqlite3.Row
      self._db.executescript(SCHEMA)
      try:
        self._db.executescript(FTS_SCHEMA)
        self._fts = True
      except sqlite3.OperationalError as exc:
        log.warning("Note FTS5 unavailable, using scan search: %s", exc)
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise NoteError(f"could not open {self.path}: {exc}") from exc

  def is_search_indexed(self):
    return self._fts

  def close(self):
    if self._db is None:
      return
    try:
      with self._lock:
        self._db.close()
    except sqlite3.Error:
      log.debug("Note store close failed", exc_info=True)
    finally:
      self._db = None

  def add(self, title, content):
    try:
      with self._lock:
        cursor = self._db.execute(
            "INSERT INTO notes (title, content, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (title, content, _now(), _now()),
        )
        self._db.commit()
        note_id = int(cursor.lastrowid)
    except sqlite3.Error as exc:
      raise NoteError(f"could not save the note: {exc}") from exc
    note = self.get(note_id)
    if note is None:
      raise NoteError("the note disappeared after saving")
    return note

  def get(self, note_id):
    try:
      with self._lock:
        row = self._db.execute(
            f"SELECT {COLUMNS} FROM notes WHERE id = ?", (int(note_id),)
        ).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
      return None
    return Note(**dict(row)) if row else None

  def list(self, limit=20):
    try:
      with self._lock:
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM notes"
            " ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    except sqlite3.Error as exc:
      raise NoteError(f"could not list notes: {exc}") from exc
    return [Note(**dict(row)) for row in rows]

  def update(self, note_id, **fields):
    allowed = {"title", "content", "updated_at"}
    unknown = sorted(set(fields) - allowed)
    if unknown:
      raise NoteError("cannot update " + ", ".join(unknown))
    values = dict(fields)
    values["updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    try:
      with self._lock:
        self._db.execute(
            f"UPDATE notes SET {assignments} WHERE id = ?",
            list(values.values()) + [int(note_id)],
        )
        self._db.commit()
    except sqlite3.Error as exc:
      raise NoteError(f"could not update the note: {exc}") from exc
    return self.get(note_id)

  def delete(self, note_id):
    try:
      with self._lock:
        self._db.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
        self._db.commit()
    except sqlite3.Error as exc:
      raise NoteError(f"could not delete the note: {exc}") from exc

  def search(self, query, limit=10, scan_limit=5000):
    terms = _terms(query)
    if not terms:
      return []
    limit = max(1, int(limit))
    if self._fts:
      fts_query = " OR ".join(f'"{term}"' for term in terms)
      try:
        with self._lock:
          rows = self._db.execute(
              f"SELECT {FTS_COLUMNS},"
              " bm25(notes_fts) AS rank"
              " FROM notes_fts"
              " JOIN notes n ON n.id = notes_fts.rowid"
              " WHERE notes_fts MATCH ?"
              " ORDER BY rank, n.updated_at DESC LIMIT ?",
              (fts_query, limit),
          ).fetchall()
      except sqlite3.OperationalError as exc:
        log.warning("Note FTS query failed, using scan search: %s", exc)
        rows = []
      if rows:
        return [
            NoteHit(
                Note(
                    row["id"],
                    row["title"],
                    row["content"],
                    row["created_at"],
                    row["updated_at"],
                ),
                _snippet(row["content"], terms),
                round(-float(row["rank"]), 4),
                "fts",
            )
            for row in rows
        ]
    return self._scan_search(terms, limit, scan_limit)

  def _scan_search(self, terms, limit, scan_limit):
    try:
      with self._lock:
        rows = self._db.execute(
            f"SELECT {COLUMNS} FROM notes"
            " ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, int(scan_limit)),),
        ).fetchall()
    except sqlite3.Error as exc:
      raise NoteError(f"could not search notes: {exc}") from exc
    scored = []
    for row in rows:
      note = Note(**dict(row))
      title = note.title.lower()
      content = note.content.lower()
      score = sum(
          2 * title.count(term) + content.count(term) for term in terms
      )
      if score:
        scored.append((score, note))
    scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
    return [
        NoteHit(note, _snippet(note.content, terms), float(score), "scan")
        for score, note in scored[:limit]
    ]


class NoteService:
  """Validation, search defaults and formatting for local notes."""

  def __init__(self, config):
    self._config = config
    self._store = NoteStore(config.resolved_db)

  def add(self, content, title=""):
    content = str(content or "").strip()
    if not content:
      raise NoteError("a note needs some text")
    if len(content) > self._config.max_content_chars:
      raise NoteError("that note is too long")
    title = _clean_title(title) or _title_from(content)
    if len(title) > 200:
      raise NoteError("that note title is too long")
    return self._store.add(title, content)

  def get(self, note_id):
    return self._store.get(note_id)

  def list(self, limit=None):
    return self._store.list(limit or 20)

  def search(self, query, limit=None):
    if not _terms(query):
      raise NoteError("a note search needs a word")
    return self._store.search(
        query,
        limit or self._config.search_limit,
        self._config.max_items,
    )

  def update(self, note_id, content=None, title=None):
    note = self._store.get(note_id)
    if note is None:
      raise NoteError("there is no note with that id")
    fields = {}
    if content is not None:
      content = str(content or "").strip()
      if not content:
        raise NoteError("a note needs some text")
      if len(content) > self._config.max_content_chars:
        raise NoteError("that note is too long")
      fields["content"] = content
      if title is None:
        fields["title"] = _title_from(content)
    if title is not None:
      title = _clean_title(title)
      if len(title) > 200:
        raise NoteError("that note title is too long")
      fields["title"] = title or _title_from(
          fields.get("content", note.content)
      )
    if not fields:
      raise NoteError("nothing to update")
    return self._store.update(note_id, **fields)

  def delete(self, note_id):
    note = self._store.get(note_id)
    if note is None:
      raise NoteError("there is no note with that id")
    self._store.delete(note_id)
    return note

  def close(self):
    self._store.close()
