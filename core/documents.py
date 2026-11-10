"""Local document intelligence and retrieval-augmented search.

Documents stay on disk. Extraction normalizes PDF, DOCX, TXT, Markdown, CSV
and XLSX files into text and tables, then a bounded chunk index in SQLite FTS5
supplies passages for search and question answering. No vector database or
embedding model is used in this feature set.
"""

from __future__ import annotations

import csv
import difflib
import io
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "models",
    "cache",
}

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".xlsx",
    ".xlsm",
}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown"}
MAX_TABLE_ROWS = 200
MAX_TABLE_COLUMNS = 30
DATE_LIMIT = 50
ACTION_LIMIT = 30
SUMMARY_SENTENCES = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  format TEXT NOT NULL,
  size INTEGER NOT NULL,
  modified REAL NOT NULL,
  chars INTEGER NOT NULL,
  chunks INTEGER NOT NULL,
  indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_document
  ON document_chunks (document_id, ordinal);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
  text,
  content='document_chunks',
  content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS document_chunks_ai
AFTER INSERT ON document_chunks BEGIN
  INSERT INTO document_chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS document_chunks_ad
AFTER DELETE ON document_chunks BEGIN
  INSERT INTO document_chunks_fts(document_chunks_fts, rowid, text)
  VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS document_chunks_au
AFTER UPDATE ON document_chunks BEGIN
  INSERT INTO document_chunks_fts(document_chunks_fts, rowid, text)
  VALUES ('delete', old.id, old.text);
  INSERT INTO document_chunks_fts(rowid, text)
  VALUES (new.id, new.text);
END;
"""

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October"
    "|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov"
    "|Dec"
)
_DATE_PATTERN = re.compile(
    rf"\b(?:20\d{{2}}-\d{{1,2}}-\d{{1,2}}"
    rf"|\d{{1,2}}[/-]\d{{1,2}}[/-](?:20\d{{2}}|\d{{2}})"
    rf"|(?:{_MONTHS})\s+\d{{1,2}}(?:,\s*20\d{{2}})?"
    rf"|(?:{_MONTHS})\s+20\d{{2}}"
    rf"|\d{{1,2}}\s+(?:{_MONTHS})(?:,?\s+20\d{{2}})?)\b",
    re.I,
)
_ACTION_HEADING = re.compile(
    r"^\s*(?:#+\s*)?(?:next\s+steps?|action\s+items?|follow[-\s]?ups?"
    r"|tasks?|to[-\s]?do)\s*:?\s*$",
    re.I,
)
_HEADING = re.compile(r"^\s*(?:#+\s+|[A-Z][A-Za-z ]{2,40}:\s*$)")
_CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+)$")
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$")
_TODO = re.compile(
    r"^\s*(?:TODO|ACTION(?:\s+ITEM)?|NEXT\s+STEP|FOLLOW[-\s]?UP)"
    r"\s*[:\-]\s*(.+)$",
    re.I,
)
_WORD = re.compile(r"[a-z0-9][a-z0-9'-]+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "can",
    "could",
    "does",
    "each",
    "from",
    "further",
    "have",
    "having",
    "here",
    "into",
    "its",
    "itself",
    "more",
    "most",
    "other",
    "over",
    "same",
    "should",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "under",
    "until",
    "very",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "would",
    "your",
}


class DocumentError(Exception):
  """Raised when a document cannot be read, indexed or written."""


@dataclass
class Table:
  """One extracted table, kept as string cells for rendering and export."""

  name: str
  rows: list = field(default_factory=list)

  def markdown(self):
    rows = [[str(cell or "") for cell in row] for row in self.rows if row]
    if not rows:
      return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)

  def text(self):
    rows = [[str(cell or "") for cell in row] for row in self.rows if row]
    return "\n".join("\t".join(row) for row in rows)


@dataclass
class Document:
  """Extracted content plus enough metadata to index and convert it."""

  path: Path
  title: str
  format: str
  text: str
  paragraphs: str = ""
  tables: list = field(default_factory=list)
  metadata: dict = field(default_factory=dict)

  @property
  def chars(self):
    return len(self.text)


def _now():
  return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode(raw):
  for encoding in ("utf-8-sig", "utf-16", "cp1252"):
    try:
      return raw.decode(encoding)
    except UnicodeDecodeError:
      continue
  return raw.decode("utf-8", errors="replace")


def _read_text(path):
  try:
    text = _decode(path.read_bytes())
  except OSError as exc:
    raise DocumentError(f"I could not read {path.name}: {exc}") from exc
  return text.replace("\r\n", "\n").replace("\r", "\n")


def _cell(value):
  if value is None:
    return ""
  if isinstance(value, datetime):
    return value.date().isoformat()
  return str(value).strip()


def _table_from_rows(name, rows):
  clean = []
  for row in rows:
    values = [_cell(cell) for cell in list(row)[:MAX_TABLE_COLUMNS]]
    while values and not values[-1]:
      values.pop()
    if any(values):
      clean.append(values)
    if len(clean) >= MAX_TABLE_ROWS:
      break
  return Table(name=name, rows=clean)


def _read_pdf(path, config):
  try:
    from pypdf import PdfReader
  except ImportError as exc:
    raise DocumentError(
        "PDF support needs pypdf; run pip install -r requirements.txt"
    ) from exc
  try:
    reader = PdfReader(str(path))
    pages = []
    for index, page in enumerate(reader.pages, 1):
      try:
        text = page.extract_text() or ""
      except Exception as exc:  # one bad page should not lose the file
        log.warning("PDF page %s of %s failed: %s", index, path.name, exc)
        text = ""
      if text.strip():
        pages.append(f"[Page {index}]\n{text.strip()}")
  except Exception as exc:
    raise DocumentError(f"I could not read the PDF {path.name}: {exc}") from exc
  title = path.stem
  try:
    title = (reader.metadata or {}).get("/Title") or title
  except Exception:
    pass
  return Document(
      path=path,
      title=str(title).strip(),
      format="PDF",
      text="\n\n".join(pages),
      paragraphs="\n\n".join(pages),
      metadata={"pages": len(reader.pages)},
  )


def _read_docx(path, config):
  try:
    from docx import Document as DocxDocument
  except ImportError as exc:
    raise DocumentError(
        "DOCX support needs python-docx; run pip install -r requirements.txt"
    ) from exc
  try:
    source = DocxDocument(str(path))
  except Exception as exc:
    raise DocumentError(f"I could not read {path.name}: {exc}") from exc
  paragraphs = [
      paragraph.text.strip()
      for paragraph in source.paragraphs
      if paragraph.text.strip()
  ]
  tables = []
  for index, table in enumerate(source.tables, 1):
    rows = [[cell.text for cell in row.cells] for row in table.rows]
    tables.append(_table_from_rows(f"Table {index}", rows))
  body = "\n".join(paragraphs)
  table_text = "\n\n".join(
      f"Table: {table.name}\n{table.text()}" for table in tables if table.rows
  )
  title = path.stem
  try:
    title = source.core_properties.title or title
  except Exception:
    pass
  if title == path.stem:
    for paragraph in source.paragraphs:
      text = paragraph.text.strip()
      style = paragraph.style.name.lower() if paragraph.style else ""
      if text and (style.startswith("heading") or style == "title"):
        title = text
        break
  text = body + (("\n\n" + table_text) if table_text else "")
  return Document(
      path=path,
      title=str(title).strip(),
      format="DOCX",
      text=text,
      paragraphs=body,
      tables=tables,
      metadata={"paragraphs": len(paragraphs), "tables": len(tables)},
  )


def _read_xlsx(path, config):
  try:
    import openpyxl
  except ImportError as exc:
    raise DocumentError(
        "XLSX support needs openpyxl; run pip install -r requirements.txt"
    ) from exc
  try:
    workbook = openpyxl.load_workbook(
        filename=str(path), read_only=True, data_only=True
    )
  except Exception as exc:
    raise DocumentError(f"I could not read {path.name}: {exc}") from exc
  tables = []
  try:
    for sheet in workbook.worksheets:
      rows = []
      for row in sheet.iter_rows(
          min_row=1,
          max_row=MAX_TABLE_ROWS,
          max_col=MAX_TABLE_COLUMNS,
          values_only=True,
      ):
        values = [_cell(value) for value in row]
        if any(values):
          rows.append(values)
      table = _table_from_rows(sheet.title, rows)
      if table.rows:
        tables.append(table)
    sheet_count = len(workbook.sheetnames)
  finally:
    workbook.close()
  text = "\n\n".join(
      f"Sheet: {table.name}\n{table.text()}" for table in tables
  )
  return Document(
      path=path,
      title=path.stem,
      format="XLSX",
      text=text,
      tables=tables,
      metadata={"sheets": sheet_count},
  )


def _read_csv(path, config):
  text = _read_text(path)
  try:
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    delimiter = dialect.delimiter
  except csv.Error:
    delimiter = ","
  rows = []
  try:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for row in reader:
      values = [_cell(cell) for cell in row[:MAX_TABLE_COLUMNS]]
      if any(values):
        rows.append(values)
      if len(rows) >= MAX_TABLE_ROWS:
        break
  except csv.Error as exc:
    raise DocumentError(f"I could not parse {path.name}: {exc}") from exc
  table = _table_from_rows("CSV", rows)
  return Document(
      path=path,
      title=path.stem,
      format="CSV",
      text=table.text(),
      tables=[table] if table.rows else [],
      metadata={"rows": len(rows), "delimiter": delimiter},
  )


def _read_plain(path, config):
  text = _read_text(path)
  title = path.stem
  if path.suffix.lower() in {".md", ".markdown"}:
    match = re.search(r"^\s*#\s+(.+)$", text, re.M)
    if match:
      title = match.group(1).strip()
  return Document(
      path=path,
      title=title,
      format="Markdown" if path.suffix.lower() in {".md", ".markdown"} else "TXT",
      text=text,
      paragraphs=text,
      metadata={"lines": len(text.splitlines())},
  )


def read_document(config, path):
  """Extract text and tables from a supported document."""
  target = Path(path)
  if not target.is_file():
    raise DocumentError(f"{target.name} is not a file I can read")
  try:
    size = target.stat().st_size
  except OSError as exc:
    raise DocumentError(f"I could not inspect {target.name}: {exc}") from exc
  if size > config.max_file_bytes:
    raise DocumentError(
        f"{target.name} is larger than the {config.max_file_bytes} byte limit"
    )
  extension = target.suffix.lower()
  if extension not in SUPPORTED_EXTENSIONS:
    formats = ", ".join(sorted(SUPPORTED_EXTENSIONS))
    raise DocumentError(f"{extension or 'that file'} is not supported ({formats})")
  if extension == ".pdf":
    document = _read_pdf(target, config)
  elif extension == ".docx":
    document = _read_docx(target, config)
  elif extension in {".xlsx", ".xlsm"}:
    document = _read_xlsx(target, config)
  elif extension == ".csv":
    document = _read_csv(target, config)
  else:
    document = _read_plain(target, config)
  document.text = document.text[: config.max_chars].strip()
  document.paragraphs = document.paragraphs[: config.max_chars].strip()
  if not document.text:
    document.text = "(no extractable text)"
  return document


def normalize_text(text):
  """Collapse whitespace while keeping paragraph boundaries for chunks."""
  lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
  return "\n".join(line for line in lines if line).strip()


def chunk_text(text, size=900, overlap=120):
  """Split text into overlapping passages at natural boundaries."""
  content = normalize_text(text)
  if not content:
    return []
  if len(content) <= size:
    return [content]
  chunks = []
  start = 0
  while start < len(content):
    end = min(start + size, len(content))
    if end < len(content):
      floor = start + size // 2
      cut = max(
          content.rfind("\n", floor, end),
          content.rfind(". ", floor, end),
          content.rfind(" ", floor, end),
      )
      if cut > start:
        end = cut + 1
    piece = content[start:end].strip()
    if piece:
      chunks.append(piece)
    if end >= len(content):
      break
    start = max(start + 1, end - overlap)
  return chunks


def format_bytes(size):
  value = float(size)
  for unit in ("bytes", "KB", "MB", "GB"):
    if value < 1024 or unit == "GB":
      return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
    value /= 1024
  return f"{value:.1f} GB"


def extract_dates(text, limit=DATE_LIMIT):
  """Find explicit dates with the line that gives them context."""
  results = []
  seen = set()
  for line in str(text or "").splitlines():
    context = " ".join(line.split())
    for match in _DATE_PATTERN.finditer(line):
      raw = match.group(0).strip()
      key = (raw.lower(), context[:100].lower())
      if key in seen:
        continue
      seen.add(key)
      results.append(
          {
              "text": raw,
              "date": _iso_date(raw),
              "context": context[:200],
          }
      )
      if len(results) >= limit:
        return results
  return results


def _iso_date(raw):
  value = " ".join(str(raw).replace(",", " ").split())
  for pattern in (
      "%Y-%m-%d",
      "%m/%d/%Y",
      "%m-%d-%Y",
      "%m/%d/%y",
      "%m-%d-%y",
      "%B %d %Y",
      "%b %d %Y",
      "%d %B %Y",
      "%d %b %Y",
  ):
    try:
      parsed = datetime.strptime(value, pattern)
      return parsed.date().isoformat()
    except ValueError:
      continue
  for pattern in ("%B %d", "%b %d", "%d %B", "%d %b"):
    try:
      parsed = datetime.strptime(value, pattern)
      return parsed.replace(year=datetime.now().year).date().isoformat()
    except ValueError:
      continue
  return ""


def extract_actions(text, limit=ACTION_LIMIT):
  """Find checkbox items, TODO markers and bullets under action headings."""
  results = []
  seen = set()
  active = False
  for raw in str(text or "").splitlines():
    line = raw.strip()
    if not line:
      continue
    if _ACTION_HEADING.match(line):
      active = True
      continue
    if active and _HEADING.match(line) and not _BULLET.match(line):
      active = False
    match = _CHECKBOX.match(raw) or _TODO.match(raw)
    item = match.group(1).strip() if match else ""
    if not item and active:
      bullet = _BULLET.match(raw)
      if bullet:
        item = bullet.group(1).strip()
    if not item:
      continue
    item = item.strip(" -")
    key = item.lower()
    if not item or key in seen:
      continue
    seen.add(key)
    results.append(item)
    if len(results) >= limit:
      break
  return results


def summarize_document(document, limit=SUMMARY_SENTENCES):
  """Build a deterministic extractive summary without calling the model."""
  sentences = [
      " ".join(part.split())
      for part in _SENTENCE_SPLIT.split(document.text)
      if len(" ".join(part.split())) >= 35
  ]
  frequencies = {}
  for word in _WORD.findall(document.text.lower()):
    if word not in _STOPWORDS:
      frequencies[word] = frequencies.get(word, 0) + 1
  title_words = set(_WORD.findall(document.title.lower()))
  scored = []
  for index, sentence in enumerate(sentences):
    words = [
        word
        for word in _WORD.findall(sentence.lower())
        if word not in _STOPWORDS
    ]
    score = sum(frequencies.get(word, 0) for word in words)
    score += 4 * sum(1 for word in words if word in title_words)
    score += 2 if index < 3 else 0
    scored.append((score, index, sentence))
  chosen = sorted(
      sorted(scored, reverse=True)[: max(1, limit)],
      key=lambda item: item[1],
  )
  points = [sentence for _score, _index, sentence in chosen]
  passages = len(chunk_text(document.text))
  plural = "" if passages == 1 else "s"
  details = [
      f"{document.title} ({document.format}, {document.chars} characters)",
      f"{len(document.tables)} table(s), {passages} passage{plural}.",
  ]
  if points:
    details.append("Key points:")
    details.extend(f"- {sentence}" for sentence in points)
  return "\n".join(details)


def compare_documents(first, second):
  """Compare two extracted documents without sending their text anywhere."""
  first_lines = [line.strip() for line in first.text.splitlines() if line.strip()]
  second_lines = [
      line.strip() for line in second.text.splitlines() if line.strip()
  ]
  matcher = difflib.SequenceMatcher(None, first_lines, second_lines)
  added = []
  removed = []
  for tag, i1, i2, j1, j2 in matcher.get_opcodes():
    if tag in {"replace", "delete"}:
      removed.extend(first_lines[i1:i2])
    if tag in {"replace", "insert"}:
      added.extend(second_lines[j1:j2])
  first_words = {word for word in _WORD.findall(first.text.lower())}
  second_words = {word for word in _WORD.findall(second.text.lower())}
  return {
      "first": first,
      "second": second,
      "similarity": round(matcher.ratio() * 100, 1),
      "added": added[:12],
      "removed": removed[:12],
      "added_count": len(added),
      "removed_count": len(removed),
      "only_first": sorted(first_words - second_words - _STOPWORDS)[:8],
      "only_second": sorted(second_words - first_words - _STOPWORDS)[:8],
      "tables": (len(first.tables), len(second.tables)),
  }


class DocumentStore:
  """SQLite document metadata plus an FTS5 passage index."""

  def __init__(self, path):
    self._path = Path(path)
    self._fts = False
    self._db = None
    try:
      self._path.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(str(self._path))
      self._db.row_factory = sqlite3.Row
      self._db.execute("PRAGMA foreign_keys = ON")
      self._db.executescript(SCHEMA)
      try:
        self._db.executescript(FTS_SCHEMA)
        self._fts = True
      except sqlite3.OperationalError as exc:
        log.warning("Document FTS5 unavailable, using scan search: %s", exc)
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise DocumentError(f"I could not open the document index: {exc}") from exc

  def close(self):
    if self._db is not None:
      self._db.close()
      self._db = None

  def _require(self):
    if self._db is None:
      raise DocumentError("the document index is closed")
    return self._db

  def is_current(self, path, size, modified):
    db = self._require()
    row = db.execute(
        "SELECT size, modified FROM documents WHERE path = ?", (str(path),)
    ).fetchone()
    return bool(
        row
        and int(row["size"]) == int(size)
        and abs(float(row["modified"]) - float(modified)) < 1.0
    )

  def save(self, document, chunks):
    db = self._require()
    path = str(document.path)
    try:
      with db:
        db.execute("DELETE FROM documents WHERE path = ?", (path,))
        cursor = db.execute(
            "INSERT INTO documents"
            " (path, title, format, size, modified, chars, chunks, indexed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                path,
                document.title,
                document.format,
                document.path.stat().st_size,
                document.path.stat().st_mtime,
                document.chars,
                len(chunks),
                _now(),
            ),
        )
        document_id = cursor.lastrowid
        db.executemany(
            "INSERT INTO document_chunks (document_id, ordinal, text)"
            " VALUES (?, ?, ?)",
            [
                (document_id, index, text)
                for index, text in enumerate(chunks, 1)
            ],
        )
    except (OSError, sqlite3.Error) as exc:
      raise DocumentError(f"I could not index {document.path.name}: {exc}") from exc
    return document_id

  def remove(self, path):
    db = self._require()
    try:
      with db:
        cursor = db.execute(
            "DELETE FROM documents WHERE path = ?", (str(path),)
        )
      return cursor.rowcount > 0
    except sqlite3.Error as exc:
      raise DocumentError(f"I could not remove that document: {exc}") from exc

  def list(self, limit=50):
    db = self._require()
    try:
      rows = db.execute(
          "SELECT id, path, title, format, chars, chunks, indexed_at"
          " FROM documents ORDER BY indexed_at DESC LIMIT ?",
          (max(1, int(limit)),),
      ).fetchall()
    except sqlite3.Error as exc:
      raise DocumentError(f"I could not read the document index: {exc}") from exc
    return [dict(row) for row in rows]

  def search(self, query, limit=5):
    db = self._require()
    terms = [
        term
        for term in re.findall(r"[a-z0-9_]+", str(query or "").lower())
        if len(term) > 1
    ][:12]
    if not terms:
      return []
    if self._fts:
      fts_query = " OR ".join(f'"{term}"' for term in terms)
      try:
        rows = db.execute(
            "SELECT d.path, d.title, c.ordinal, c.text,"
            " bm25(document_chunks_fts) AS rank"
            " FROM document_chunks_fts"
            " JOIN document_chunks c ON c.id = document_chunks_fts.rowid"
            " JOIN documents d ON d.id = c.document_id"
            " WHERE document_chunks_fts MATCH ?"
            " ORDER BY rank LIMIT ?",
            (fts_query, max(1, int(limit))),
        ).fetchall()
      except sqlite3.OperationalError as exc:
        log.warning("Document FTS query failed, using scan search: %s", exc)
        rows = []
      if rows:
        return [
            {
                "path": row["path"],
                "title": row["title"],
                "ordinal": row["ordinal"],
                "text": row["text"],
                "score": round(-float(row["rank"]), 4),
                "mode": "fts",
            }
            for row in rows
        ]
    return self._scan_search(db, terms, limit)

  def _scan_search(self, db, terms, limit):
    try:
      rows = db.execute(
          "SELECT d.path, d.title, c.ordinal, c.text"
          " FROM document_chunks c"
          " JOIN documents d ON d.id = c.document_id"
      ).fetchall()
    except sqlite3.Error as exc:
      raise DocumentError(f"I could not search the document index: {exc}") from exc
    scored = []
    for row in rows:
      lowered = row["text"].lower()
      score = sum(1 for term in terms if term in lowered)
      if score:
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "path": row["path"],
            "title": row["title"],
            "ordinal": row["ordinal"],
            "text": row["text"],
            "score": float(score),
            "mode": "scan",
        }
        for score, row in scored[: max(1, int(limit))]
    ]

  def context(self, query, limit, chars):
    hits = self.search(query, limit=limit)
    lines = []
    used = 0
    for index, hit in enumerate(hits, 1):
      passage = f"[{index}] {hit['title']} (passage {hit['ordinal']}):"
      passage += f" {hit['text']}"
      if used + len(passage) > chars:
        break
      lines.append(passage)
      used += len(passage)
    return "\n\n".join(lines), hits


class DocumentService:
  """Path-safe document operations used by the assistant and tools."""

  def __init__(self, config, tools_config):
    self._config = config
    self._tools = tools_config
    self._store = DocumentStore(config.resolved_db)

  def close(self):
    self._store.close()

  def resolve(self, raw):
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
      raise DocumentError("a path is required")
    root = self._tools.resolved_root
    path = Path(text).expanduser()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
      raise DocumentError(
          f"{text} is outside the allowed folder ({root}). Change"
          " [tools] root in config/settings.toml to work elsewhere"
      )
    return resolved

  def relative(self, path):
    try:
      return str(Path(path).resolve().relative_to(self._tools.resolved_root))
    except ValueError:
      return Path(path).name

  def read(self, path):
    return read_document(self._config, self.resolve(path))

  def index(self, path):
    document = self.read(path)
    chunks = chunk_text(
        document.text, self._config.chunk_chars, self._config.chunk_overlap
    )[: self._config.max_chunks]
    if not chunks:
      raise DocumentError(f"{document.title} has no text to index")
    self._store.save(document, chunks)
    log.info(
        "Indexed %s (%s chunks, %s chars)",
        document.path.name,
        len(chunks),
        document.chars,
    )
    return {
        "path": str(document.path),
        "title": document.title,
        "format": document.format,
        "chars": document.chars,
        "chunks": len(chunks),
        "tables": len(document.tables),
    }

  def index_folder(self, path, recursive=True):
    target = self.resolve(path)
    if not target.is_dir():
      raise DocumentError(f"{target.name} is not a folder")
    files = list(
        self._walk(target, recursive)[: self._config.max_files]
    )
    result = {
        "path": str(target),
        "found": len(files),
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }
    for file in files:
      try:
        stat = file.stat()
        if self._store.is_current(file, stat.st_size, stat.st_mtime):
          result["skipped"] += 1
          continue
        self.index(str(file))
        result["indexed"] += 1
      except (OSError, DocumentError) as exc:
        result["failed"] += 1
        if len(result["errors"]) < 5:
          result["errors"].append(f"{file.name}: {exc}")
    return result

  def _walk(self, root, recursive):
    stack = [(root, 0)]
    found = []
    while stack:
      current, depth = stack.pop()
      try:
        entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
      except OSError:
        continue
      for entry in entries:
        try:
          if entry.is_dir():
            if (
                recursive
                and entry.name.lower() not in _SKIP_DIRECTORIES
                and not entry.name.startswith(".")
            ):
              stack.append((entry, depth + 1))
            continue
          if (
              entry.suffix.lower() in SUPPORTED_EXTENSIONS
              and not entry.name.startswith(".")
              and entry.stat().st_size <= self._config.max_file_bytes
          ):
            found.append(entry)
        except OSError:
          continue
    return found

  def search(self, query, limit=None):
    return self._store.search(
        query, limit or self._config.search_limit
    )

  def list(self, limit=50):
    return self._store.list(limit)

  def remove(self, path):
    target = self.resolve(path)
    removed = self._store.remove(target)
    if not removed:
      raise DocumentError(f"{target.name} is not in the index")
    return {"path": str(target)}

  def context(self, query):
    return self._store.context(
        query,
        self._config.search_limit,
        self._config.context_chars,
    )

  def summary(self, path):
    return summarize_document(self.read(path))

  def dates(self, path):
    return extract_dates(self.read(path).text)

  def actions(self, path):
    return extract_actions(self.read(path).text)

  def tables(self, path):
    return self.read(path).tables

  def compare(self, first, second):
    return compare_documents(self.read(first), self.read(second))

  def write(self, path, content, title="", tables=None):
    target = self.resolve(path)
    extension = target.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
      raise DocumentError(
          "write to one of: " + ", ".join(sorted(SUPPORTED_EXTENSIONS))
      )
    try:
      target.parent.mkdir(parents=True, exist_ok=True)
      if extension in TEXT_EXTENSIONS:
        text = content
        if extension in {".md", ".markdown"} and title:
          if not text.lstrip().startswith("#"):
            text = f"# {title}\n\n{text}"
        target.write_text(text, encoding="utf-8")
      elif extension == ".csv":
        rows = _rows_from_tables(tables, content)
        with target.open("w", encoding="utf-8", newline="") as handle:
          csv.writer(handle).writerows(rows)
      elif extension in {".xlsx", ".xlsm"}:
        _write_xlsx(target, content, title, tables)
      elif extension == ".docx":
        _write_docx(target, content, title, tables)
      else:
        _write_pdf(target, content, title)
    except OSError as exc:
      raise DocumentError(f"I could not write {target.name}: {exc}") from exc
    result = self.index(str(target))
    result["path"] = str(target)
    return result

  def convert(self, source, target=None, format=None):
    document = self.read(source)
    if target:
      destination = str(target)
    else:
      extension = "." + str(format or "").lstrip(".").lower()
      if extension not in SUPPORTED_EXTENSIONS:
        raise DocumentError("a supported target format is required")
      destination = str(Path(source).with_suffix(extension))
    extension = Path(destination).suffix.lower()
    if extension in {".csv", ".xlsx", ".xlsm"} and not document.tables:
      raise DocumentError(
          f"{document.title} has no tables to convert into that format"
      )
    body = document.text
    if extension == ".docx":
      # Keep table content in the DOCX table objects only, not twice.
      body = document.paragraphs or ("" if document.tables else document.text)
    result = self.write(
        destination,
        body,
        title=document.title,
        tables=document.tables,
    )
    return {"source": str(document.path), **result}

  def report(self, paths, output, title="Document report"):
    documents = [self.read(path) for path in paths]
    lines = [
        f"# {title}",
        "",
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}.",
        "",
    ]
    for document in documents:
      lines.extend(
          [
              f"## {document.title}",
              f"{document.format}, {document.chars} characters,"
              f" {len(document.tables)} table(s).",
              "",
              summarize_document(document),
              "",
          ]
      )
      dates = extract_dates(document.text, limit=8)
      if dates:
        lines.append("Dates: " + "; ".join(item["text"] for item in dates))
        lines.append("")
      actions = extract_actions(document.text, limit=8)
      if actions:
        lines.append("Action items:")
        lines.extend(f"- {item}" for item in actions)
        lines.append("")
    result = self.write(
        output, "\n".join(lines).strip() + "\n", title=title
    )
    return {"documents": len(documents), **result}


def _rows_from_tables(tables, content):
  if tables:
    first = tables[0]
    return [list(row) for row in first.rows]
  rows = []
  for row in csv.reader(io.StringIO(content)):
    values = [str(cell).strip() for cell in row]
    if any(values):
      rows.append(values)
  return rows


def _safe_sheet_name(name, used):
  base = re.sub(r"[\[\]:*?/\\]", " ", str(name or "Sheet")).strip() or "Sheet"
  base = base[:31]
  candidate = base
  index = 2
  while candidate in used:
    suffix = f" {index}"
    candidate = base[: 31 - len(suffix)] + suffix
    index += 1
  used.add(candidate)
  return candidate


def _write_xlsx(path, content, title, tables):
  import openpyxl

  workbook = openpyxl.Workbook()
  workbook.remove(workbook.active)
  used = set()
  sources = tables or [Table(title or path.stem, _rows_from_tables([], content))]
  for table in sources:
    if not table.rows:
      continue
    sheet = workbook.create_sheet(_safe_sheet_name(table.name, used))
    for row in table.rows:
      sheet.append([str(cell) for cell in row])
  if not workbook.sheetnames:
    workbook.create_sheet("Sheet1")
  workbook.save(str(path))
  workbook.close()


def _write_docx(path, content, title, tables):
  from docx import Document as DocxDocument

  document = DocxDocument()
  if title:
    document.add_heading(title, level=1)
  for paragraph in re.split(r"\n\s*\n", content):
    text = paragraph.strip()
    if text:
      document.add_paragraph(text)
  for table in tables or []:
    if not table.rows:
      continue
    width = max(len(row) for row in table.rows)
    output = document.add_table(rows=0, cols=width)
    for row in table.rows:
      cells = output.add_row().cells
      for index in range(width):
        cells[index].text = str(row[index]) if index < len(row) else ""
  document.save(str(path))


def _pdf_escape(text):
  return (
      text.replace("\\", "\\\\")
      .replace("(", "\\(")
      .replace(")", "\\)")
  )


def _pdf_lines(text, width=88):
  lines = []
  for raw in str(text or "").splitlines():
    words = raw.split()
    if not words:
      lines.append("")
      continue
    current = words[0]
    for word in words[1:]:
      if len(current) + 1 + len(word) <= width:
        current += " " + word
      else:
        lines.append(current)
        current = word
    lines.append(current)
  return lines or [""]


def _write_pdf(path, content, title):
  """Write a small text-only PDF without adding a PDF generation library."""
  lines = _pdf_lines((title + "\n\n" if title else "") + content)
  pages = [lines[index:index + 50] for index in range(0, len(lines), 50)]
  pages = pages or [[""]]
  objects = {}
  page_ids = []
  for index, page_lines in enumerate(pages):
    page_id = 4 + index * 2
    stream_id = page_id + 1
    page_ids.append(page_id)
    objects[page_id] = (
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        f" /Resources << /Font << /F1 3 0 R >> >>"
        f" /Contents {stream_id} 0 R >>"
    ).encode("latin-1")
    stream = ["BT", "/F1 11 Tf", "50 780 Td", "14 TL"]
    for line in page_lines:
      safe = _pdf_escape(line).encode("latin-1", "replace").decode("latin-1")
      stream.append(f"({safe}) Tj")
      stream.append("T*")
    stream.append("ET")
    payload = "\n".join(stream).encode("latin-1")
    objects[stream_id] = (
        f"<< /Length {len(payload)} >>\nstream\n".encode("latin-1")
        + payload
        + b"\nendstream"
    )
  kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
  objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
  objects[2] = (
      f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
  ).encode("latin-1")
  objects[3] = (
      b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
      b" /Encoding /WinAnsiEncoding >>"
  )
  total = max(objects)
  data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
  offsets = {}
  for object_id in range(1, total + 1):
    body = objects.get(object_id, b"<< >>")
    offsets[object_id] = len(data)
    data.extend(f"{object_id} 0 obj\n".encode("latin-1"))
    data.extend(body)
    data.extend(b"\nendobj\n")
  xref = len(data)
  data.extend(f"xref\n0 {total + 1}\n".encode("latin-1"))
  data.extend(b"0000000000 65535 f \n")
  for object_id in range(1, total + 1):
    data.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("latin-1"))
  data.extend(
      (
          f"trailer << /Size {total + 1} /Root 1 0 R >>\n"
          f"startxref\n{xref}\n%%EOF\n"
      ).encode("latin-1")
  )
  Path(path).write_bytes(bytes(data))
