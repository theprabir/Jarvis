"""Document tools: read, index, search, extract, convert and write files."""

from __future__ import annotations

import logging

from core.documents import DocumentError, normalize_text
from tools.base import ToolError, ToolResult

log = logging.getLogger(__name__)


def _fail(exc):
  raise ToolError(str(exc)) from exc


def read_document(service, path):
  try:
    document = service.read(path)
  except DocumentError as exc:
    _fail(exc)
  preview = normalize_text(document.text)[:300]
  text = (
      f"{document.title} is a {document.format} document with"
      f" {document.chars} characters and {len(document.tables)} table(s)."
  )
  if preview:
    text += f" It starts: {preview}"
  metadata = document.metadata
  if metadata.get("ocr_confidence") is not None:
    text += (
        f" OCR confidence: {round(float(metadata['ocr_confidence']) * 100)}%"
        f" ({metadata.get('ocr_level', 'unknown')}; local estimate)."
    )
  if metadata.get("ocr_partial_pages") or metadata.get("ocr_partial"):
    text += " OCR warning: recognition may be incomplete; verify the scan."
  if metadata.get("ocr_truncated"):
    text += " OCR was limited by the ocr_max_pages setting."
  return ToolResult(
      True,
      text,
      {
          "path": str(document.path),
          "title": document.title,
          "format": document.format,
          "text": document.text,
          "tables": len(document.tables),
          "metadata": document.metadata,
      },
  )


def index_document(service, path):
  try:
    result = service.index(path)
  except DocumentError as exc:
    _fail(exc)
  text = f"Indexed {result['title']} into {result['chunks']} searchable passages."
  if result.get("ocr_partial_pages") or result.get("ocr_partial"):
    text += " OCR warning: recognition may be incomplete."
  elif result.get("ocr_confidence") is not None:
    text += (
        f" OCR confidence: {round(float(result['ocr_confidence']) * 100)}%."
    )
  return ToolResult(True, text, result)


def index_folder(service, path=".", recursive=True):
  try:
    result = service.index_folder(path, recursive)
  except DocumentError as exc:
    _fail(exc)
  text = (
      f"Indexed {result['indexed']} document(s), skipped"
      f" {result['skipped']} unchanged and failed {result['failed']}."
  )
  if result["errors"]:
    text += " First problem: " + result["errors"][0]
  if result.get("ocr_warnings"):
    text += " OCR warning: " + result["ocr_warnings"][0]
  return ToolResult(True, text, result)


def search_documents(service, query):
  try:
    hits = service.search(query)
  except DocumentError as exc:
    _fail(exc)
  if not hits:
    return ToolResult(
        True,
        f"I found no indexed passages about {query}.",
        {"query": query, "hits": []},
    )
  names = []
  for hit in hits[:3]:
    if hit["title"] not in names:
      names.append(hit["title"])
  return ToolResult(
      True,
      f"I found {len(hits)} passage(s) in " + ", ".join(names) + ".",
      {"query": query, "hits": hits},
  )


def extract_document(service, path, kind="dates"):
  kind = str(kind or "dates").lower()
  if kind not in {"dates", "actions", "tables"}:
    raise ToolError("kind must be dates, actions or tables")
  try:
    if kind == "actions":
      items = service.actions(path)
    elif kind == "tables":
      tables = service.tables(path)
      items = [
          {"name": table.name, "rows": table.rows} for table in tables
      ]
    else:
      items = service.dates(path)
  except DocumentError as exc:
    _fail(exc)
  label = {"actions": "action items", "tables": "tables"}.get(
      kind, "dates"
  )
  return ToolResult(
      True,
      f"I found {len(items)} {label} in that document.",
      {"path": str(path), "kind": kind, "items": items},
  )


def convert_document(service, source, format="", target=""):
  try:
    result = service.convert(source, target or None, format or None)
  except DocumentError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Converted {result['source']} to {result['path']}.",
      result,
  )


def write_document(service, path, content, title=""):
  try:
    result = service.write(path, content, title)
  except DocumentError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Created {result['path']} with {result['chars']} characters"
      " of indexed text.",
      result,
  )


def report_documents(service, paths, output, title="Document report"):
  names = [item.strip() for item in str(paths or "").split("|") if item.strip()]
  if not names:
    raise ToolError("at least one document path is required")
  try:
    result = service.report(names, output, title or "Document report")
  except DocumentError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Wrote a report on {result['documents']} document(s) to"
      f" {result['path']}.",
      result,
  )
