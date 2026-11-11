"""Validated clipboard-history tools."""

from __future__ import annotations

from core.clipboard_history import ClipboardError, format_entry
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Clipboard history is disabled.")
  return service


def _data(entry):
  return {
      "id": entry.id,
      "content": entry.content,
      "source": entry.source,
      "created_at": entry.created_at,
  }


def list_entries(provider, limit=20):
  service = _service(provider)
  try:
    entries = service.list(limit)
  except ClipboardError as exc:
    raise ToolError(str(exc)) from exc
  if not entries:
    return ToolResult(True, "Clipboard history is empty.", {"count": 0, "entries": []})
  return ToolResult(
      True,
      "Clipboard history:\n" + "\n".join(format_entry(entry) for entry in entries),
      {"count": len(entries), "entries": [_data(entry) for entry in entries]},
  )


def search_entries(provider, query, limit=10):
  service = _service(provider)
  try:
    entries = service.search(query, limit)
  except ClipboardError as exc:
    raise ToolError(str(exc)) from exc
  if not entries:
    return ToolResult(True, "No clipboard entries matched.", {"count": 0, "entries": []})
  return ToolResult(
      True,
      "Clipboard search results:\n"
      + "\n".join(format_entry(entry) for entry in entries),
      {"count": len(entries), "entries": [_data(entry) for entry in entries]},
  )


def show_entry(provider, id):
  service = _service(provider)
  try:
    entry = service.get(id)
  except ClipboardError as exc:
    raise ToolError(str(exc)) from exc
  if entry is None:
    raise ToolError("there is no clipboard entry with that id.")
  return ToolResult(True, format_entry(entry, detail=True), _data(entry))


def delete_entry(provider, id):
  service = _service(provider)
  try:
    entry = service.delete(id)
  except ClipboardError as exc:
    raise ToolError(str(exc)) from exc
  return ToolResult(True, f"Clipboard entry #{entry.id} deleted.", _data(entry))


def clear_history(provider):
  service = _service(provider)
  try:
    count = service.clear()
  except ClipboardError as exc:
    raise ToolError(str(exc)) from exc
  return ToolResult(True, f"Deleted {count} clipboard entries.", {"count": count})
