"""Sandboxed text-file and folder tools.

Every path is resolved against ``[tools] root`` and refused when it escapes
that folder, so a mistaken or malicious request cannot read or modify files
elsewhere on the machine.
"""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path

from tools.base import ToolError, ToolResult

SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "models",
    "cache",
}
PREVIEW_CHARS = 300


def _resolve(config, raw):
  text = str(raw or "").strip().strip('"').strip("'")
  if not text:
    raise ToolError("a path is required")
  path = Path(text).expanduser()
  root = config.resolved_root
  candidate = path if path.is_absolute() else root / path
  resolved = candidate.resolve()
  if resolved != root and root not in resolved.parents:
    raise ToolError(f"{text} is outside the allowed folder ({root})")
  return resolved


def read_file(config, path):
  target = _resolve(config, path)
  if not target.is_file():
    raise ToolError(f"{target.name} is not a file I can read")
  size = target.stat().st_size
  if size > config.max_read_bytes:
    raise ToolError(
        f"{target.name} is larger than the {config.max_read_bytes} byte limit"
    )
  try:
    content = target.read_text(encoding="utf-8", errors="replace")
  except OSError as exc:
    raise ToolError(f"I could not read {target.name}: {exc}") from exc

  lines = len(content.splitlines())
  preview = content.strip()
  if len(preview) > PREVIEW_CHARS:
    preview = preview[:PREVIEW_CHARS].rstrip() + "..."
  plural = "" if lines == 1 else "s"
  text = f"{target.name} has {lines} line{plural}."
  text += f" It starts: {preview}" if preview else " It is empty."
  return ToolResult(
      True,
      text,
      {"path": str(target), "content": content, "lines": lines},
  )


def write_file(config, path, content):
  target = _resolve(config, path)
  try:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
  except OSError as exc:
    raise ToolError(f"I could not write {target.name}: {exc}") from exc
  return ToolResult(
      True,
      f"Saved {len(content)} characters to {target.name}.",
      {"path": str(target), "characters": len(content)},
  )


def create_folder(config, path):
  target = _resolve(config, path)
  if target.exists():
    raise ToolError(f"{target.name} already exists")
  try:
    target.mkdir(parents=True)
  except OSError as exc:
    raise ToolError(f"I could not create {target.name}: {exc}") from exc
  return ToolResult(True, f"Created the folder {target.name}.", {"path": str(target)})


def rename_path(config, source, target):
  origin = _resolve(config, source)
  destination = _resolve(config, target)
  if not origin.exists():
    raise ToolError(f"{origin.name} does not exist")
  if destination.exists():
    raise ToolError(f"{destination.name} already exists")
  try:
    origin.rename(destination)
  except OSError as exc:
    raise ToolError(f"I could not rename {origin.name}: {exc}") from exc
  return ToolResult(
      True,
      f"Renamed {origin.name} to {destination.name}.",
      {"path": str(destination)},
  )


def copy_path(config, source, target):
  origin = _resolve(config, source)
  destination = _resolve(config, target)
  if not origin.exists():
    raise ToolError(f"{origin.name} does not exist")
  if destination.exists():
    raise ToolError(f"{destination.name} already exists")
  try:
    if origin.is_dir():
      shutil.copytree(origin, destination)
    else:
      shutil.copy2(origin, destination)
  except OSError as exc:
    raise ToolError(f"I could not copy {origin.name}: {exc}") from exc
  return ToolResult(
      True,
      f"Copied {origin.name} to {destination.name}.",
      {"path": str(destination)},
  )


def move_path(config, source, target):
  origin = _resolve(config, source)
  destination = _resolve(config, target)
  if not origin.exists():
    raise ToolError(f"{origin.name} does not exist")
  if destination.exists():
    raise ToolError(f"{destination.name} already exists")
  try:
    shutil.move(str(origin), str(destination))
  except OSError as exc:
    raise ToolError(f"I could not move {origin.name}: {exc}") from exc
  return ToolResult(
      True,
      f"Moved {origin.name} to {destination.name}.",
      {"path": str(destination)},
  )


def delete_path(config, path):
  target = _resolve(config, path)
  if target == config.resolved_root:
    raise ToolError("I will not delete the allowed folder itself")
  if not target.exists():
    raise ToolError(f"{target.name} does not exist")
  try:
    if target.is_dir():
      shutil.rmtree(target)
    else:
      target.unlink()
  except OSError as exc:
    raise ToolError(f"I could not delete {target.name}: {exc}") from exc
  return ToolResult(True, f"Deleted {target.name}.", {"path": str(target)})


def list_files(config, path="."):
  folder = _resolve(config, path)
  if not folder.is_dir():
    raise ToolError(f"{folder.name} is not a folder")
  try:
    entries = sorted(folder.iterdir(), key=lambda item: item.name.lower())
  except OSError as exc:
    raise ToolError(f"I could not list {folder.name}: {exc}") from exc
  if not entries:
    return ToolResult(True, f"{folder.name} is empty.", {"items": []})

  shown = entries[: config.search_limit]
  names = [
      entry.name + ("/" if entry.is_dir() else "") for entry in shown
  ]
  extra = len(entries) - len(shown)
  text = f"{folder.name} contains {len(entries)} item"
  text += "" if len(entries) == 1 else "s"
  text += ": " + ", ".join(names)
  if extra > 0:
    text += f", and {extra} more"
  text += "."
  return ToolResult(
      True,
      text,
      {"path": str(folder), "items": names, "total": len(entries)},
  )


def search_files(config, name, path="."):
  pattern = (name or "").strip().strip('"').strip("'")
  if not pattern:
    raise ToolError("a file name or pattern is required")
  if not any(character in pattern for character in "*?[]"):
    pattern = f"*{pattern}*"
  folder = _resolve(config, path)
  if not folder.is_dir():
    raise ToolError(f"{folder.name} is not a folder")

  matches = []
  for entry, _depth in _walk(folder, config.max_search_depth):
    if fnmatch.fnmatch(entry.name.lower(), pattern.lower()):
      matches.append(entry)
      if len(matches) >= config.search_limit:
        break

  if not matches:
    return ToolResult(
        True,
        f"I found no files matching {pattern} in {folder.name}.",
        {"matches": []},
    )
  root = config.resolved_root
  names = []
  for match in matches:
    try:
      names.append(str(match.relative_to(root)))
    except ValueError:
      names.append(match.name)
  text = f"I found {len(matches)} match"
  text += "" if len(matches) == 1 else "es"
  text += ": " + ", ".join(names) + "."
  return ToolResult(True, text, {"matches": names})


def _walk(root, max_depth):
  """Yield ``(path, depth)`` below ``root`` without heavy or noisy folders."""
  stack = [(root, 0)]
  while stack:
    current, depth = stack.pop()
    try:
      entries = list(current.iterdir())
    except OSError:
      continue
    for entry in entries:
      try:
        is_dir = entry.is_dir()
      except OSError:
        continue
      yield entry, depth + 1
      if (
          is_dir
          and entry.name.lower() not in SKIP_DIRECTORIES
          and depth + 1 < max_depth
      ):
        stack.append((entry, depth + 1))
