"""Local visual intelligence for images and screenshots.

Feature set 4.17 intentionally uses conventional image processing rather than a
second neural model. Pillow supplies metadata, colour and pixel comparison;
Windows OCR supplies text; numpy is used only for a small edge/connected-region
pass that suggests UI regions. Nothing is uploaded.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 20_000_000
MAX_OCR_PIXELS = 25_000_000
MAX_COMPARE_PIXELS = 4096
MAX_ELEMENTS = 16
MAX_TEXT_CHARS = 20_000
SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "models",
    "cache",
}
IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}
_ERROR_PATTERN = re.compile(
    r"\b(?:error|exception|failed|failure|denied|not found|traceback"
    r"|timeout|timed out|cannot|unable)\b",
    re.I,
)
_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")
_WORD = re.compile(r"[a-z0-9][a-z0-9'-]+")


class VisionError(Exception):
  """An image could not be inspected or indexed."""


@dataclass
class VisualAnalysis:
  """One local image analysis result."""

  path: Path
  title: str
  format: str
  mode: str
  width: int
  height: int
  size: int
  text: str = ""
  metadata: dict = field(default_factory=dict)
  colors: list = field(default_factory=list)
  brightness: float = 0.0
  contrast: float = 0.0
  elements: list = field(default_factory=list)
  observations: list = field(default_factory=list)
  category: str = "image"
  summary: str = ""

  @property
  def chars(self):
    return len(self.text)


SCHEMA = """
CREATE TABLE IF NOT EXISTS vision_images (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  format TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  size INTEGER NOT NULL,
  modified REAL NOT NULL,
  chars INTEGER NOT NULL,
  summary TEXT NOT NULL,
  metadata TEXT NOT NULL,
  indexed_at TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS vision_images_fts USING fts5(
  title,
  text,
  summary
);
"""


def _now():
  return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(text, limit=MAX_TEXT_CHARS):
  value = " ".join(str(text or "").split())
  return value[:limit]


def _numbers(text):
  values = []
  for match in _NUMBER.finditer(str(text or "")):
    raw = match.group(0).replace(",", "")
    try:
      values.append(float(raw.rstrip("%")))
    except ValueError:
      continue
  return values[:40]


def _trend(values):
  if len(values) < 2:
    return "insufficient numeric data"
  first = sum(values[: max(1, len(values) // 2)]) / max(
      1, len(values) // 2
  )
  second = sum(values[len(values) // 2:]) / max(
      1, len(values) - len(values) // 2
  )
  if not first:
    return "insufficient baseline"
  change = (second - first) / abs(first)
  if change > 0.08:
    return "rising"
  if change < -0.08:
    return "falling"
  return "flat"


def _short_summary(text, limit=5):
  body = " ".join(str(text or "").split())
  if not body:
    return ""
  sentences = [
      sentence.strip()
      for sentence in re.split(r"(?<=[.!?])\s+|\n+", body)
      if sentence.strip()
  ]
  if len(sentences) <= limit:
    return " ".join(sentences)
  return " ".join(sentences[:limit]) + " ..."


class VisionStore:
  """Small SQLite image index sharing the document database."""

  def __init__(self, path):
    self._path = Path(path)
    self._db = None
    self._fts = False
    self._open()

  def _open(self):
    try:
      self._path.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(str(self._path))
      self._db.row_factory = sqlite3.Row
      self._db.executescript(SCHEMA)
      try:
        self._db.executescript(FTS_SCHEMA)
        self._fts = True
      except sqlite3.OperationalError as exc:
        log.warning("Vision FTS5 unavailable, using scan search: %s", exc)
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise VisionError(f"I could not open the image index: {exc}") from exc

  def close(self):
    if self._db is not None:
      self._db.close()
      self._db = None

  def _require(self):
    if self._db is None:
      raise VisionError("the image index is closed")
    return self._db

  def save(self, analysis):
    db = self._require()
    path = str(analysis.path)
    try:
      with db:
        old = db.execute(
            "SELECT id FROM vision_images WHERE path = ?", (path,)
        ).fetchone()
        db.execute("DELETE FROM vision_images WHERE path = ?", (path,))
        cursor = db.execute(
            "INSERT INTO vision_images"
            " (path, title, format, width, height, size, modified, chars,"
            " summary, metadata, indexed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                path,
                analysis.title,
                analysis.format,
                analysis.width,
                analysis.height,
                analysis.size,
                analysis.path.stat().st_mtime,
                analysis.chars,
                analysis.summary,
                json.dumps(analysis.metadata, sort_keys=True),
                _now(),
            ),
        )
        row_id = cursor.lastrowid
        if self._fts and old is not None:
          db.execute(
              "DELETE FROM vision_images_fts WHERE rowid = ?", (old["id"],)
          )
        if self._fts:
          db.execute(
              "INSERT INTO vision_images_fts(rowid, title, text, summary)"
              " VALUES (?, ?, ?, ?)",
              (
                  row_id,
                  analysis.title,
                  analysis.text[:MAX_TEXT_CHARS],
                  analysis.summary,
              ),
          )
    except (OSError, sqlite3.Error) as exc:
      raise VisionError(f"I could not index {analysis.title}: {exc}") from exc
    return {
        "path": path,
        "title": analysis.title,
        "format": analysis.format,
        "width": analysis.width,
        "height": analysis.height,
        "chars": analysis.chars,
        "summary": analysis.summary,
    }

  def search(self, query, limit=10):
    db = self._require()
    text = _clean(query)
    if not text:
      return []
    words = re.findall(r"[A-Za-z0-9']+", text)
    if self._fts and words:
      expression = " AND ".join(words)
      try:
        rows = db.execute(
            "SELECT v.path, v.title, v.format, v.width, v.height, v.summary,"
            " snippet(vision_images_fts, 1, '[', ']', '...', 12) AS snippet"
            " FROM vision_images_fts"
            " JOIN vision_images v ON v.id = vision_images_fts.rowid"
            " WHERE vision_images_fts MATCH ?"
            " ORDER BY rank LIMIT ?",
            (expression, int(limit)),
        ).fetchall()
        if rows:
          return [dict(row) for row in rows]
      except sqlite3.OperationalError:
        pass
    rows = db.execute(
        "SELECT path, title, format, width, height, summary,"
        " substr(summary, 1, 240) AS snippet"
        " FROM vision_images"
        " WHERE title LIKE ? OR summary LIKE ?"
        " ORDER BY title COLLATE NOCASE LIMIT ?",
        (f"%{text}%", f"%{text}%", int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


class VisionService:
  """Inspect, compare and index local images without a visual model."""

  def __init__(
      self,
      documents_config,
      tools_config=None,
      document_service=None,
      screenshot_provider=None,
      ocr_reader=None,
      store=None,
  ):
    self._documents_config = documents_config
    self._tools = tools_config
    self._documents = document_service
    self._screenshot_provider = screenshot_provider
    self._ocr_reader = ocr_reader
    self._store = store

  def close(self):
    if self._store is not None:
      self._store.close()
      self._store = None

  def _get_store(self):
    if self._store is None:
      db = getattr(self._documents_config, "resolved_db", None)
      if db is None:
        raise VisionError("the image index database is not configured")
      self._store = VisionStore(db)
    return self._store

  def resolve(self, raw):
    if self._documents is not None:
      try:
        return self._documents.resolve(raw)
      except Exception as exc:
        raise VisionError(str(exc)) from exc
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
      raise VisionError("a path is required")
    root = Path(getattr(self._tools, "resolved_root", ".")).resolve()
    path = Path(text).expanduser()
    target = (path if path.is_absolute() else root / path).resolve()
    if target != root and root not in target.parents:
      raise VisionError(f"{text} is outside the allowed folder ({root})")
    return target

  def _read_text(self, path):
    if self._ocr_reader is not None:
      raw = self._ocr_reader(path)
    elif self._documents is not None:
      try:
        raw = self._documents.read(path)
      except Exception as exc:
        log.info("OCR was unavailable for %s: %s", path, exc)
        return "", {"ocr_error": str(exc)}
    else:
      return "", {}
    if isinstance(raw, str):
      return _clean(raw), {}
    text = _clean(getattr(raw, "text", "") or "")
    metadata = dict(getattr(raw, "metadata", {}) or {})
    return text, metadata

  def _open_image(self, path):
    try:
      from PIL import Image
    except ImportError as exc:
      raise VisionError(
          "Visual intelligence needs Pillow; run pip install -r requirements.txt"
      ) from exc
    target = self.resolve(path)
    if not target.is_file():
      raise VisionError(f"{target.name} is not a file I can inspect")
    try:
      size = target.stat().st_size
    except OSError as exc:
      raise VisionError(f"I could not inspect {target.name}: {exc}") from exc
    if size > MAX_IMAGE_BYTES:
      raise VisionError(
          f"{target.name} is larger than the {MAX_IMAGE_BYTES} byte limit"
      )
    try:
      image = Image.open(str(target))
      image.load()
    except Exception as exc:
      raise VisionError(f"I could not open {target.name}: {exc}") from exc
    if image.width * image.height > MAX_OCR_PIXELS:
      image.close()
      raise VisionError(
          f"that image is too large ({image.width}x{image.height})"
      )
    return target, image

  @staticmethod
  def _metadata(target, image):
    try:
      from PIL import ExifTags
    except ImportError:
      ExifTags = None
    info = dict(getattr(image, "info", {}) or {})
    exif = {}
    try:
      raw = image.getexif()
      for key, value in list(raw.items())[:30]:
        name = (
            ExifTags.TAGS.get(key, str(key))
            if ExifTags is not None
            else str(key)
        )
        exif[str(name)] = str(value)[:160]
    except Exception:
      exif = {}
    return {
        "format": str(image.format or target.suffix.lstrip(".").upper()),
        "mode": str(image.mode),
        "width": int(image.width),
        "height": int(image.height),
        "bytes": int(target.stat().st_size),
        "frames": int(getattr(image, "n_frames", 1) or 1),
        "animated": bool(getattr(image, "is_animated", False)),
        "dpi": str(info.get("dpi", ""))[:80],
        "has_transparency": bool(
            "transparency" in info or image.mode in {"RGBA", "LA"}
        ),
        "icc_profile": bool(info.get("icc_profile")),
        "exif": exif,
    }

  @staticmethod
  def _colors_and_stats(image):
    from PIL import ImageStat

    rgb = image.convert("RGB")
    thumb = rgb.copy()
    thumb.thumbnail((96, 96))
    colors = []
    try:
      quantized = thumb.quantize(colors=5, method=2)
      palette = quantized.getpalette() or []
      counts = sorted(
          quantized.getcolors() or [], key=lambda item: item[0], reverse=True
      )
      total = max(1, sum(count for count, _ in counts))
      for count, index in counts[:5]:
        base = index * 3
        if base + 2 >= len(palette):
          continue
        red, green, blue = palette[base:base + 3]
        colors.append(
            {
                "hex": f"#{red:02x}{green:02x}{blue:02x}",
                "percent": round(count / total * 100, 1),
            }
        )
      quantized.close()
    except Exception:
      colors = []
    gray = rgb.convert("L")
    stat = ImageStat.Stat(gray)
    brightness = round(float(stat.mean[0]), 1)
    contrast = round(float(stat.stddev[0]), 1)
    rgb.close()
    thumb.close()
    gray.close()
    return colors, brightness, contrast

  @staticmethod
  def _elements(image):
    """Suggest large edge-rich rectangles without OpenCV or a visual model."""
    try:
      import numpy as np
    except ImportError:
      return []
    try:
      gray = image.convert("L")
      thumb = gray.copy()
      thumb.thumbnail((160, 160))
      array = np.asarray(thumb, dtype=np.int16)
      if array.shape[0] < 3 or array.shape[1] < 3:
        return []
      vertical = np.abs(np.diff(array, axis=0, prepend=array[:1, :]))
      horizontal = np.abs(np.diff(array, axis=1, prepend=array[:, :1]))
      edges = np.maximum(vertical, horizontal)
      threshold = max(10, float(edges.mean() + edges.std() * 1.4))
      mask = edges > threshold
      visited = np.zeros(mask.shape, dtype=bool)
      height, width = mask.shape
      components = []
      for y in range(height):
        for x in range(width):
          if not mask[y, x] or visited[y, x]:
            continue
          stack = [(y, x)]
          visited[y, x] = True
          min_y = max_y = y
          min_x = max_x = x
          count = 0
          while stack:
            cy, cx = stack.pop()
            count += 1
            min_y = min(min_y, cy)
            max_y = max(max_y, cy)
            min_x = min(min_x, cx)
            max_x = max(max_x, cx)
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
              if (
                  0 <= ny < height
                  and 0 <= nx < width
                  and mask[ny, nx]
                  and not visited[ny, nx]
              ):
                visited[ny, nx] = True
                stack.append((ny, nx))
          box_w = max_x - min_x + 1
          box_h = max_y - min_y + 1
          if count >= 8 and box_w >= 4 and box_h >= 3:
            components.append((count, min_x, min_y, box_w, box_h))
      components.sort(reverse=True)
      scale_x = image.width / max(1, width)
      scale_y = image.height / max(1, height)
      results = []
      for count, x, y, box_w, box_h in components[:MAX_ELEMENTS]:
        results.append(
            {
                "x": round(x * scale_x),
                "y": round(y * scale_y),
                "width": round(box_w * scale_x),
                "height": round(box_h * scale_y),
                "area_percent": round(count / (width * height) * 100, 2),
            }
        )
      return results
    except Exception as exc:
      log.debug("UI element hint failed: %s", exc)
      return []
    finally:
      try:
        gray.close()
        thumb.close()
      except Exception:
        pass

  def analyze(self, path):
    target, image = self._open_image(path)
    try:
      metadata = self._metadata(target, image)
      colors, brightness, contrast = self._colors_and_stats(image)
      elements = self._elements(image)
      text, ocr_metadata = self._read_text(target)
      width, height = image.width, image.height
    finally:
      image.close()
    observations = self._observations(
        text, metadata, colors, brightness, contrast, elements
    )
    category = self._category(target, metadata, text, elements)
    analysis = VisualAnalysis(
        path=target,
        title=target.stem,
        format=metadata["format"],
        mode=metadata["mode"],
        width=width,
        height=height,
        size=metadata["bytes"],
        text=text,
        metadata={**metadata, **ocr_metadata},
        colors=colors,
        brightness=brightness,
        contrast=contrast,
        elements=elements,
        observations=observations,
        category=category,
    )
    analysis.summary = self._summary(analysis)
    return analysis

  @staticmethod
  def _observations(text, metadata, colors, brightness, contrast, elements):
    points = [
        f"{metadata['width']}x{metadata['height']} {metadata['format']}"
        f" ({metadata['bytes']} bytes).",
    ]
    if colors:
      points.append(
          "Dominant colours: "
          + ", ".join(
              f"{item['hex']} ({item['percent']}%)" for item in colors[:3]
          )
          + "."
      )
    if brightness < 35:
      points.append("The image is very dark.")
    elif contrast < 18:
      points.append("The image has low contrast.")
    if elements:
      points.append(f"Detected {len(elements)} large edge/UI region(s).")
    if text:
      points.append(f"OCR found {len(text.split())} word(s).")
    else:
      points.append("No readable text was recognized.")
    return points

  @staticmethod
  def _category(path, metadata, text, elements):
    name = path.name.lower()
    if "screenshot" in name or "screen" in name:
      return "screenshot"
    if _ERROR_PATTERN.search(text):
      return "troubleshooting"
    if elements:
      return "interface"
    if _NUMBER.search(text):
      return "chart"
    if text:
      return "text-image"
    return "image"

  @staticmethod
  def _summary(analysis):
    if analysis.text:
      summary = _short_summary(analysis.text)
      if summary:
        return summary
    return " ".join(analysis.observations[:3])

  def text(self, path):
    target = self.resolve(path)
    text, metadata = self._read_text(target)
    if not text:
      raise VisionError(f"I found no readable text in {target.name}")
    return text, metadata

  def summary(self, path):
    analysis = self.analyze(path)
    points = [analysis.summary]
    points.extend(analysis.observations)
    return "\n".join(point for point in points if point)

  def metadata(self, path):
    target, image = self._open_image(path)
    try:
      return self._metadata(target, image)
    finally:
      image.close()

  def elements(self, path):
    target, image = self._open_image(path)
    try:
      return self._elements(image)
    finally:
      image.close()

  def compare(self, first, second):
    first_analysis = self.analyze(first)
    second_analysis = self.analyze(second)
    try:
      similarity = self._pixel_similarity(first, second)
    except VisionError:
      similarity = 0.0
    first_lines = [
        line.strip() for line in first_analysis.text.splitlines() if line.strip()
    ]
    second_lines = [
        line.strip() for line in second_analysis.text.splitlines() if line.strip()
    ]
    matcher = difflib.SequenceMatcher(None, first_lines, second_lines)
    added = []
    removed = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
      if tag in {"replace", "delete"}:
        removed.extend(first_lines[i1:i2])
      if tag in {"replace", "insert"}:
        added.extend(second_lines[j1:j2])
    first_words = set(_WORD.findall(first_analysis.text.lower()))
    second_words = set(_WORD.findall(second_analysis.text.lower()))
    return {
        "first": first_analysis,
        "second": second_analysis,
        "pixel_similarity": similarity,
        "text_similarity": round(matcher.ratio() * 100, 1),
        "added": added[:10],
        "removed": removed[:10],
        "only_first": sorted(first_words - second_words)[:8],
        "only_second": sorted(second_words - first_words)[:8],
        "size_changed": (
            first_analysis.width,
            first_analysis.height,
        )
        != (second_analysis.width, second_analysis.height),
    }

  def _pixel_similarity(self, first, second):
    from PIL import Image, ImageChops, ImageStat

    first_target = self.resolve(first)
    second_target = self.resolve(second)
    try:
      with Image.open(str(first_target)) as one, Image.open(
          str(second_target)
      ) as two:
        one = one.convert("L").resize((64, 64))
        two = two.convert("L").resize((64, 64))
        difference = ImageChops.difference(one, two)
        mean = ImageStat.Stat(difference).mean[0]
        one.close()
        two.close()
        difference.close()
        return round(max(0.0, 1.0 - mean / 255.0) * 100, 1)
    except Exception as exc:
      raise VisionError(f"I could not compare the images: {exc}") from exc

  def chart(self, path):
    analysis = self.analyze(path)
    values = _numbers(analysis.text)
    labels = [
        line.strip()
        for line in analysis.text.splitlines()
        if line.strip() and not _NUMBER.fullmatch(line.strip())
    ][:6]
    trend = _trend(values)
    kind = "chart-like image"
    if analysis.elements:
      kind = "chart or dashboard-like image"
    return {
        "analysis": analysis,
        "kind": kind,
        "values": values[:20],
        "trend": trend,
        "labels": labels,
    }

  def diagram(self, path):
    analysis = self.analyze(path)
    labels = [
        line.strip()
        for line in analysis.text.splitlines()
        if line.strip()
    ][:10]
    vertical = {"top": 0, "middle": 0, "bottom": 0}
    for element in analysis.elements:
      center = element["y"] + element["height"] / 2
      if center < analysis.height / 3:
        vertical["top"] += 1
      elif center < analysis.height * 2 / 3:
        vertical["middle"] += 1
      else:
        vertical["bottom"] += 1
    return {
        "analysis": analysis,
        "labels": labels,
        "boxes": len(analysis.elements),
        "vertical": vertical,
    }

  def troubleshoot(self, path):
    analysis = self.analyze(path)
    checks = []
    if analysis.contrast < 18:
      checks.append("Low contrast; increase window size or zoom before OCR.")
    if analysis.brightness < 35:
      checks.append("The image is dark; check the display brightness.")
    if analysis.width < 800 or analysis.height < 600:
      checks.append("Low resolution; a larger screenshot may OCR better.")
    if not analysis.text:
      checks.append("No readable text; crop the relevant window and retry.")
    if _ERROR_PATTERN.search(analysis.text):
      checks.append(
          "Error words were recognized; read the final exception line and"
          " check the referenced path, permission or service."
      )
    if analysis.metadata.get("ocr_partial") or analysis.metadata.get(
        "ocr_partial_pages"
    ):
      checks.append("OCR reported partial recognition; verify the image.")
    if analysis.elements:
      checks.append(
          f"{len(analysis.elements)} large UI region(s) detected; crop the"
          " active window for a cleaner result."
      )
    if not checks:
      checks.append("No obvious visual or OCR problem was detected.")
    return analysis, checks

  def latest(self):
    """Return the newest screenshot in the sandbox for path-less commands."""
    if self._tools is None:
      raise VisionError("screenshots are not configured")
    folder = Path(self._tools.resolved_root) / "screenshots"
    if not folder.is_dir():
      raise VisionError("there are no screenshots in the sandbox")
    files = [
        item
        for item in folder.iterdir()
        if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
    ]
    if not files:
      raise VisionError("there are no screenshots in the sandbox")
    return self.resolve(max(files, key=lambda item: item.stat().st_mtime))

  def capture(self):
    if self._screenshot_provider is not None:
      value = self._screenshot_provider()
    else:
      if self._tools is None:
        raise VisionError("screenshots are not configured")
      from tools.desktop import take_screenshot

      result = take_screenshot(self._tools)
      if not result.ok:
        raise VisionError(result.text)
      value = result.data.get("path", "")
    return self.resolve(value)

  def screenshot(self):
    path = self.capture()
    return self.analyze(path)

  def index(self, path):
    analysis = self.analyze(path)
    return self._get_store().save(analysis)

  def index_folder(self, path, recursive=True):
    root = self.resolve(path)
    if not root.is_dir():
      raise VisionError(f"{root.name} is not a folder")
    files = self._walk(root, recursive)
    result = {
        "path": str(root),
        "found": len(files),
        "indexed": 0,
        "failed": 0,
        "errors": [],
    }
    for file in files:
      try:
        self.index(file)
        result["indexed"] += 1
      except (OSError, VisionError) as exc:
        result["failed"] += 1
        if len(result["errors"]) < 5:
          result["errors"].append(f"{file.name}: {exc}")
    return result

  def _walk(self, root, recursive):
    stack = [(root, 0)]
    found = []
    while stack and len(found) < 500:
      current, depth = stack.pop()
      try:
        entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
      except OSError:
        continue
      for entry in entries:
        if entry.is_dir():
          if (
              recursive
              and entry.name.lower() not in SKIP_DIRECTORIES
              and not entry.name.startswith(".")
          ):
            stack.append((entry, depth + 1))
          continue
        if entry.suffix.lower() in IMAGE_EXTENSIONS:
          found.append(entry)
        if len(found) >= 500:
          break
    return found

  def search(self, query, limit=10):
    return self._get_store().search(query, limit)


def format_analysis(analysis):
  """Render a concise analysis for a command or tool result."""
  lines = [
      f"{analysis.title}: {analysis.width}x{analysis.height}"
      f" {analysis.format}, {analysis.size} bytes.",
      f"Category: {analysis.category}.",
  ]
  if analysis.colors:
    lines.append(
        "Colours: "
        + ", ".join(
            f"{item['hex']} ({item['percent']}%)"
            for item in analysis.colors[:4]
        )
    )
  lines.append(
      f"Brightness {analysis.brightness}/255, contrast {analysis.contrast}."
  )
  if analysis.elements:
    lines.append(f"UI regions: {len(analysis.elements)} detected.")
  if analysis.text:
    lines.append(f"OCR text ({len(analysis.text.split())} words):")
    lines.append(analysis.text[:3000])
  else:
    lines.append("OCR text: none recognized.")
  if analysis.summary:
    lines.append(f"Summary: {analysis.summary}")
  return "\n".join(lines)
