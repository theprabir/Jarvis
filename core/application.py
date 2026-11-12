"""Active-application intelligence and confirmed browser automation.

The Windows APIs stay behind injectable providers so the logic is deterministic
and testable without touching the desktop. Reading the foreground application
and window title is read-only. Clipboard reads are explicit, and every key
sequence or screenshot/OCR action is confirmation-only at the tool layer.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

BROWSER_SHORTCUTS = {
    "new_tab": ("ctrl+t", "Open a new tab"),
    "close_tab": ("ctrl+w", "Close the current tab"),
    "reopen_tab": ("ctrl+shift+t", "Reopen the last closed tab"),
    "next_tab": ("ctrl+tab", "Switch to the next tab"),
    "previous_tab": ("ctrl+shift+tab", "Switch to the previous tab"),
    "reload": ("ctrl+r", "Reload the page"),
    "address": ("ctrl+l", "Focus the address bar"),
    "find": ("ctrl+f", "Find in the page"),
    "back": ("alt+left", "Go back"),
    "forward": ("alt+right", "Go forward"),
}
EDITOR_SHORTCUTS = {
    "save": ("ctrl+s", "Save"),
    "find": ("ctrl+f", "Find"),
    "command_palette": ("ctrl+shift+p", "Open the command palette"),
    "quick_open": ("ctrl+p", "Quick open a file"),
    "terminal": ("ctrl+`", "Open the integrated terminal"),
}
GENERIC_SHORTCUTS = {
    "copy": ("ctrl+c", "Copy"),
    "paste": ("ctrl+v", "Paste"),
    "cut": ("ctrl+x", "Cut"),
    "undo": ("ctrl+z", "Undo"),
    "save": ("ctrl+s", "Save"),
    "find": ("ctrl+f", "Find"),
}
SHORTCUT_ACTS = {
    "browser": BROWSER_SHORTCUTS,
    "editor": EDITOR_SHORTCUTS,
    "generic": GENERIC_SHORTCUTS,
}
ERROR_GUIDANCE = (
    (r"ModuleNotFoundError|ImportError", "The module is missing or not on the import path. Install it or fix the import/venv."),
    (r"SyntaxError|IndentationError|TabError", "The Python parser rejected the file. Check the reported line, indentation and unfinished syntax."),
    (r"FileNotFoundError|No such file or directory", "A referenced path does not exist. Check the working directory and the exact path."),
    (r"KeyError", "A dictionary or mapping lookup used a key that is not present. Check the key and use a guarded lookup."),
    (r"PermissionError|Access is denied|Permission denied", "The process lacks permission for that file or action. Check ownership, read-only flags and elevation."),
    (r"ConnectionRefused|ECONNREFUSED|Failed to connect", "The target service refused the connection. Confirm the server is running and the host/port are correct."),
    (r"Timeout|timed out|ETIMEDOUT", "The operation exceeded its time limit. Check network/service health or raise the timeout."),
    (r"Uncaught TypeError|Cannot read propert", "JavaScript tried to use a missing value. Guard the object before reading the property."),
    (r"HTTP\s*40[13]|403 Forbidden|401 Unauthorized", "The request was rejected. Check authentication, permissions and the requested URL."),
    (r"HTTP\s*404|404 Not Found", "The requested resource was not found. Check the URL and whether the resource moved."),
    (r"HTTP\s*5\d\d|500 Internal Server Error", "The remote service failed. Inspect its logs before retrying."),
)


class ApplicationError(Exception):
  """An application-context or confirmed action could not be completed."""


def classify_clipboard(text):
  """Classify pasted content without interpreting or transmitting it."""
  content = str(text or "").strip()
  if not content:
    return {"kind": "empty", "chars": 0, "preview": ""}
  lower = content.lower()
  if re.match(r"^https?://\S+$", lower):
    kind = "url"
  elif "traceback (most recent call last)" in lower or re.search(
      r"(?:error|exception)\s*:", lower
  ):
    kind = "error"
  elif re.search(r"(?:^|\n)\s*(?:def |class |import |from |function |const |let |var )", content):
    kind = "code"
  else:
    kind = "text"
  return {
      "kind": kind,
      "chars": len(content),
      "preview": " ".join(content.split())[:240],
  }


class ApplicationService:
  """Inspect the foreground app and perform explicitly confirmed actions."""

  def __init__(
      self,
      config,
      tools_config=None,
      documents_config=None,
      active_window=None,
      clipboard_reader=None,
      key_sender=None,
      screenshot_provider=None,
      page_fetcher=None,
      document_reader=None,
  ):
    self._config = config
    self._tools = tools_config
    self._documents = documents_config
    self._active_provider = active_window
    self._clipboard_reader = clipboard_reader
    self._key_sender = key_sender
    self._screenshot_provider = screenshot_provider
    self._page_fetcher = page_fetcher
    self._document_reader = document_reader

  def active(self):
    raw = self._active()
    title = " ".join(str(raw.get("title") or "").split())
    process = _process_name(raw.get("process") or raw.get("pid") or "")
    browser_names = {_process_name(name) for name in self._config.browser_processes}
    browser = process in browser_names
    return {
        "title": title,
        "process": process,
        "pid": raw.get("pid", 0),
        "hwnd": raw.get("hwnd", 0),
        "application": _friendly_name(process),
        "browser": browser,
    }

  def context(self):
    active = self.active()
    category = self._category(active)
    actions = self._context_actions(active, category)
    return {
        "active": active,
        "category": category,
        "shortcuts": self._shortcuts(category),
        "actions": actions,
        "clipboard": "not read; use /app clipboard explicitly",
    }

  def shortcuts(self):
    active = self.active()
    category = self._category(active)
    return {
        "application": active["application"],
        "category": category,
        "shortcuts": self._shortcuts(category),
    }

  def clipboard(self):
    text = self._read_clipboard()
    if len(text) > self._config.max_clipboard_chars:
      text = text[: self._config.max_clipboard_chars].rstrip() + "..."
    info = classify_clipboard(text)
    info["content"] = text
    return info

  def explain_clipboard(self):
    info = self.clipboard()
    if info["kind"] == "empty":
      return "The clipboard is empty, sir.", info
    if info["kind"] != "error":
      return (
          "The clipboard does not look like an error, sir: "
          + info["preview"],
          info,
      )
    guidance = _error_guidance(info["content"])
    return (
        f"Copied error ({info['chars']} characters):\n"
        f"{info['preview']}\n\n{guidance}",
        info,
    )

  def selected_text(self):
    info = self.clipboard()
    if info["kind"] == "empty":
      raise ApplicationError("the clipboard is empty")
    return info["content"]

  def search_selected(self):
    text = self.selected_text()
    query = " ".join(text.split())
    if len(query) > 500:
      query = query[:500].rsplit(" ", 1)[0]
    if not query:
      raise ApplicationError("there is no selected text to search")
    return query

  def shortcut(self, action):
    active = self.active()
    category = self._category(active)
    table = SHORTCUT_ACTS[category]
    item = table.get(str(action or "").strip().lower())
    if item is None:
      available = ", ".join(sorted(table))
      raise ApplicationError(
          f"{action!r} is not a shortcut for {active['application']}; "
          f"available: {available}"
      )
    keys, label = item
    self._send(keys)
    return f"{label} in {active['application']}.", {
        "action": action,
        "keys": keys,
        "application": active["application"],
    }

  def browser_action(self, action):
    action = str(action or "").strip().lower()
    active = self.active()
    if not active["browser"]:
      raise ApplicationError("the active window is not a supported browser")
    if action not in BROWSER_SHORTCUTS:
      raise ApplicationError("use new_tab, close_tab, next_tab, previous_tab, reload, address, back or forward")
    keys, label = BROWSER_SHORTCUTS[action]
    self._send(keys)
    return f"{label} in {active['application']}.", {
        "action": action,
        "keys": keys,
        "application": active["application"],
    }

  def browser_url(self):
    active = self.active()
    if not active["browser"]:
      raise ApplicationError("the active window is not a supported browser")
    self._send("ctrl+l")
    time.sleep(0.12)
    self._send("ctrl+c")
    time.sleep(0.12)
    text = self._read_clipboard().strip()
    if not re.match(r"^https?://", text, re.I):
      raise ApplicationError("the browser did not put a URL on the clipboard")
    return text.splitlines()[0].strip()

  def page_summary(self):
    active = self.active()
    if not active["browser"]:
      raise ApplicationError("the active window is not a supported browser")
    url = self.browser_url()
    text = self._fetch_page(url)
    if not text:
      raise ApplicationError("the page had no readable article text")
    summary = _extractive_summary(text, self._config.max_page_chars)
    return (
        f"Page: {active['title'] or url}\n{url}\n\n{summary}",
        {"url": url, "title": active["title"], "chars": len(text)},
    )

  def screenshot_text(self):
    if self._documents is None and self._document_reader is None:
      raise ApplicationError("document OCR is not configured")
    path = self._screenshot()
    text, data = self._read_image(path)
    return (
        f"Screenshot OCR ({data.get('quality', 'unknown')} quality):\n{text}",
        {"path": str(path), **data},
    )

  def documentation_query(self, query):
    topic = " ".join(str(query or "").split())
    if not topic:
      raise ApplicationError("a documentation topic is required")
    try:
      active = self.active()
    except ApplicationError:
      active = {"application": "application"}
    return f"{active['application']} {topic} official documentation"

  def _category(self, active):
    if active["browser"]:
      return "browser"
    name = active["process"]
    if name in {
        "code",
        "code-insiders",
        "devenv",
        "idea64",
        "pycharm64",
        "sublime_text",
        "notepad++",
        "notepad",
    }:
      return "editor"
    return "generic"

  def _shortcuts(self, category):
    return [
        {"action": action, "keys": keys, "label": label}
        for action, (keys, label) in SHORTCUT_ACTS[category].items()
    ]

  @staticmethod
  def _context_actions(active, category):
    if category == "browser":
      return [
          "summarize this page",
          "search selected text",
          "new tab",
          "close tab",
          "next tab",
      ]
    if category == "editor":
      return [
          "explain copied error",
          "search selected text",
          "find application documentation",
      ]
    return ["explain copied error", "search selected text", "show shortcuts"]

  def _active(self):
    if self._active_provider is not None:
      return self._active_provider()
    try:
      from tools.desktop import active_window

      raw = active_window()
    except Exception as exc:
      raise ApplicationError(f"could not read the active window: {exc}") from exc
    pid = raw.get("pid", 0)
    try:
      import psutil

      if pid:
        raw["process"] = psutil.Process(pid).name()
    except Exception:
      raw.setdefault("process", "")
    return raw

  def _read_clipboard(self):
    if self._clipboard_reader is not None:
      raw = self._clipboard_reader()
    else:
      from tools.clipboard import read_clipboard

      if self._tools is None:
        raise ApplicationError("clipboard access is not configured")
      result = read_clipboard(self._tools)
      if not result.ok:
        raise ApplicationError(result.text)
      raw = result.data.get("content", "")
    return str(raw or "")

  def _send(self, keys):
    if self._key_sender is not None:
      self._key_sender(keys)
      return
    try:
      from tools.desktop import send_shortcut

      send_shortcut(keys)
    except Exception as exc:
      raise ApplicationError(str(exc)) from exc

  def _screenshot(self):
    from pathlib import Path

    if self._screenshot_provider is not None:
      value = self._screenshot_provider()
    else:
      if self._tools is None:
        raise ApplicationError("screenshots are not configured")
      from tools.desktop import take_screenshot

      result = take_screenshot(self._tools)
      if not result.ok:
        raise ApplicationError(result.text)
      value = result.data.get("path", "")
    path = Path(str(value))
    if not path.is_file():
      raise ApplicationError("the screenshot was not created")
    return path

  def _read_image(self, path):
    if self._document_reader is not None:
      return self._document_reader(path)
    try:
      from core.documents import read_document

      document = read_document(self._documents, path)
    except Exception as exc:
      raise ApplicationError(f"OCR failed: {exc}") from exc
    text = " ".join(str(getattr(document, "text", "") or "").split())
    if not text:
      raise ApplicationError("the screenshot had no readable text")
    quality = getattr(document, "quality", None)
    quality_text = (
        quality.get("label", "unknown") if isinstance(quality, dict) else "unknown"
    )
    return text[: self._config.max_page_chars], {
        "quality": quality_text,
        "chars": len(text),
    }

  def _fetch_page(self, url):
    if self._page_fetcher is not None:
      return str(self._page_fetcher(url) or "")
    import httpx

    from web.extract import download, extract_text

    try:
      with httpx.Client(timeout=12.0, follow_redirects=True) as client:
        markup = download(client, url, timeout_s=12.0, max_bytes=400_000)
    except Exception as exc:
      raise ApplicationError(f"could not fetch the page: {exc}") from exc
    return extract_text(markup, url)


def _process_name(value):
  if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
    try:
      import psutil

      return _process_name(psutil.Process(int(value)).name())
    except Exception:
      return ""
  text = str(value or "").strip().lower()
  if text.endswith(".exe"):
    text = text[:-4]
  return text


def _friendly_name(process):
  names = {
      "chrome": "Google Chrome",
      "msedge": "Microsoft Edge",
      "firefox": "Mozilla Firefox",
      "brave": "Brave",
      "opera": "Opera",
      "vivaldi": "Vivaldi",
      "code": "Visual Studio Code",
      "devenv": "Visual Studio",
      "explorer": "File Explorer",
      "notepad": "Notepad",
      "pycharm64": "PyCharm",
      "idea64": "IntelliJ IDEA",
  }
  return names.get(process, process.title() if process else "unknown application")


def _error_guidance(text):
  for pattern, guidance in ERROR_GUIDANCE:
    if re.search(pattern, text, re.I):
      return guidance
  return (
      "The copied text looks like an error. Read the final exception line first,"
      " then check the referenced file, path or service."
  )


def _extractive_summary(text, limit):
  body = " ".join(str(text or "").split())
  if len(body) <= limit:
    return body
  sentences = re.split(r"(?<=[.!?])\s+", body)
  result = []
  size = 0
  for sentence in sentences:
    if not sentence:
      continue
    if result and size + len(sentence) + 1 > limit:
      break
    result.append(sentence)
    size += len(sentence) + 1
  return " ".join(result) or body[:limit].rstrip() + "..."
