"""Validated note tools backed by the shared local note service."""

from __future__ import annotations

from core.notes import NoteError, format_note
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Notes are disabled.")
  return service


def _fail(exc):
  raise ToolError(str(exc)) from exc


def _note_data(note):
  return {
      "id": note.id,
      "title": note.title,
      "content": note.content,
      "created_at": note.created_at,
      "updated_at": note.updated_at,
  }


def _hit_data(hit):
  return {
      "note": _note_data(hit.note),
      "snippet": hit.snippet,
      "score": hit.score,
      "mode": hit.mode,
  }


def add_note(provider, content, title=""):
  service = _service(provider)
  try:
    note = service.add(content, title=title)
  except NoteError as exc:
    _fail(exc)
  return ToolResult(True, f"Note #{note.id} saved: {note.title}.", _note_data(note))


def list_notes(provider, limit=20):
  service = _service(provider)
  limit = max(1, min(int(limit), 50))
  try:
    notes = service.list(limit=limit)
  except NoteError as exc:
    _fail(exc)
  if not notes:
    return ToolResult(True, "You have no notes.", {"count": 0, "notes": []})
  text = "Notes:\n" + "\n".join(format_note(note) for note in notes)
  return ToolResult(
      True, text, {"count": len(notes), "notes": [_note_data(note) for note in notes]}
  )


def show_note(provider, id):
  service = _service(provider)
  try:
    note = service.get(id)
  except NoteError as exc:
    _fail(exc)
  if note is None:
    raise ToolError("there is no note with that id.")
  return ToolResult(True, format_note(note), _note_data(note))


def search_notes(provider, query, limit=10):
  service = _service(provider)
  limit = max(1, min(int(limit), 50))
  try:
    hits = service.search(query, limit=limit)
  except NoteError as exc:
    _fail(exc)
  if not hits:
    return ToolResult(
        True,
        f"No notes matched {query!r}.",
        {"query": query, "count": 0, "results": []},
    )
  text = f"Notes matching {query!r}:\n" + "\n".join(
      format_note(hit.note, hit.snippet) for hit in hits
  )
  return ToolResult(
      True,
      text,
      {
          "query": query,
          "count": len(hits),
          "results": [_hit_data(hit) for hit in hits],
      },
  )


def update_note(provider, id, content="", title=""):
  service = _service(provider)
  try:
    note = service.update(
        id,
        content=content if content else None,
        title=title if title else None,
    )
  except NoteError as exc:
    _fail(exc)
  return ToolResult(True, f"Note #{note.id} updated: {note.title}.", _note_data(note))


def delete_note(provider, id):
  service = _service(provider)
  try:
    note = service.delete(id)
  except NoteError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Note #{note.id} removed: {note.title}.",
      _note_data(note),
  )
