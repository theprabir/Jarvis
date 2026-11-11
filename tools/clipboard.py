"""Clipboard access through the standard-library tkinter binding."""

from __future__ import annotations

import logging

from tools.base import ToolError, ToolResult

log = logging.getLogger(__name__)

PREVIEW_CHARS = 300


def _tk_root():
  try:
    import tkinter
  except ImportError as exc:  # pragma: no cover - tkinter ships with Python
    raise ToolError("The clipboard needs tkinter, which is missing") from exc
  try:
    root = tkinter.Tk()
    root.withdraw()
    return root
  except tkinter.TclError as exc:
    raise ToolError(f"The clipboard is unavailable: {exc}") from exc


def copy_text(config, text, history=None):
  if not isinstance(text, str) or not text.strip():
    raise ToolError("nothing to copy")
  root = _tk_root()
  try:
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
  except Exception as exc:
    raise ToolError(f"I could not write to the clipboard: {exc}") from exc
  finally:
    root.destroy()
  if history is not None:
    try:
      history.add(text, source="clipboard")
    except Exception:
      log.warning("Could not save clipboard history", exc_info=True)
  return ToolResult(
      True, "Copied to the clipboard, sir.", {"characters": len(text)}
  )


def read_clipboard(config):
  root = _tk_root()
  try:
    try:
      text = root.clipboard_get()
    except Exception as exc:
      raise ToolError("The clipboard is empty or unavailable") from exc
  finally:
    root.destroy()

  preview = text.strip()
  if len(preview) > PREVIEW_CHARS:
    preview = preview[:PREVIEW_CHARS].rstrip() + "..."
  return ToolResult(
      True,
      f"The clipboard contains {len(text)} characters: {preview}",
      {"content": text, "characters": len(text)},
  )
