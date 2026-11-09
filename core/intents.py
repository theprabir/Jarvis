"""Deterministic intent recognition, entity extraction and JSON validation.

Requests that Python can answer (arithmetic, current time and date, computer
actions, built-in commands) are recognized here and answered without guessing.
The router returns ``None`` for everything else, so ambiguous or open-ended
requests fall back to the LLM. ``Intent`` doubles as the validated structured
output format used by tool calling.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from core.calculator import CalculationError, evaluate
from core.dates import find_date_reference

from core.memory import classify as classify_memory
from core.utilities import (
    UtilityError,
    convert,
    format_quantity,
    format_size,
    parse_duration,
    parse_reminder,
    transform_text,
)

CONFIDENCE_THRESHOLD = 0.7

TOOL_INTENTS = frozenset(
    {
        "system_info",
        "processes",
        "open_app",
        "close_app",
        "close_all_apps",
        "open_url",
        "open_path",
        "read_file",
        "write_file",
        "create_folder",
        "rename_path",
        "copy_path",
        "move_path",
        "delete_path",
        "list_files",
        "search_files",
        "run_command",
        "screenshot",
        "notify",
        "windows_list",
        "window_action",
        "clipboard_copy",
        "clipboard_read",
    }
)

FILE_INTENTS = frozenset(
    {
        "create_folder",
        "write_file",
        "read_file",
        "list_files",
        "search_files",
        "rename_path",
        "copy_path",
        "move_path",
        "delete_path",
    }
)

CONVERSATION_INTENTS = frozenset(
    {
        "time",
        "date",
        "math",
        "conversation",
        "help",
        "new",
        "mode",
        "web_search",
        "remember_memory",
        "search_memory",
        "list_memory",
        "delete_memory",
        "learned_memory",
        "memory_episodes",
        "unit_convert",
        "text_transform",
        "format_size",
        "weather",
        "timer_set",
        "reminder_set",
        "timer_list",
        "timer_cancel",
        "history",
        "forget",
        "intent",
        "confirm",
        "cancel",
        "tools",
    }
)

INTENT_NAMES = TOOL_INTENTS | CONVERSATION_INTENTS
COMMANDS = frozenset(
    {
        "help",
        "new",
        "mode",
        "history",
        "forget",
        "intent",
        "confirm",
        "cancel",
        "tools",
    }
)

_TIME_PATTERNS = (
    r"\bwhat(?:'s| is)? the time\b",
    r"\bwhat time is it\b",
    r"\bcurrent time\b",
    r"\btime (?:now|please)\b",
    r"\bdo you know the time\b",
)

_DATE_PATTERNS = (
    r"\bwhat(?:'s| is)? the date\b",
    r"\bwhat day is it\b",
    r"\bwhat day is today\b",
    r"\btoday'?s date\b",
)

_MATH_WORDS = (
    ("multiplied by", "*"),
    ("divided by", "/"),
    ("to the power of", "**"),
    ("times", "*"),
    ("plus", "+"),
    ("minus", "-"),
    ("squared", "**2"),
)

_MATH_PREFIX = re.compile(
    r"^\s*(?:what's|whats|what is|how much is|calculate|compute|evaluate"
    r"|solve|tell me)\s+"
)
_MATH_FILLER = re.compile(r"\b(?:please|sir|for me|the result of)\b")

_TLDS = {
    "ai",
    "app",
    "co",
    "com",
    "de",
    "dev",
    "edu",
    "fr",
    "gov",
    "in",
    "info",
    "io",
    "me",
    "net",
    "org",
    "tv",
    "uk",
    "xyz",
}

_FILE_EXTENSIONS = {
    "csv",
    "db",
    "docx",
    "exe",
    "gif",
    "html",
    "ini",
    "jpeg",
    "jpg",
    "js",
    "json",
    "log",
    "md",
    "mp3",
    "mp4",
    "pdf",
    "png",
    "py",
    "toml",
    "ts",
    "txt",
    "wav",
    "xlsx",
    "yaml",
    "yml",
    "zip",
}


class IntentError(ValueError):
  """Raised when a structured intent payload fails validation."""


@dataclass(frozen=True)
class Intent:
  """A recognized intent with extracted entities and a confidence score."""

  name: str
  confidence: float
  entities: dict = field(default_factory=dict)
  source: str = "deterministic"

  def to_dict(self):
    return {
        "intent": self.name,
        "confidence": round(float(self.confidence), 2),
        "entities": dict(self.entities),
        "source": self.source,
    }

  def to_json(self):
    return json.dumps(self.to_dict(), sort_keys=True)

  @classmethod
  def from_dict(cls, data):
    """Validate an untrusted intent payload and build an Intent."""
    if not isinstance(data, dict):
      raise IntentError("intent payload must be an object")
    name = data.get("intent")
    if name not in INTENT_NAMES:
      raise IntentError(f"unknown intent: {name!r}")
    confidence = data.get("confidence", 0.0)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
      raise IntentError("confidence must be a number between 0 and 1")
    entities = data.get("entities", {})
    if not isinstance(entities, dict):
      raise IntentError("entities must be an object")
    for key, value in entities.items():
      if not isinstance(key, str):
        raise IntentError("entity names must be strings")
      if isinstance(value, bool):
        continue
      if not isinstance(value, (str, int, float)):
        raise IntentError(f"entity {key!r} has an unsupported value type")
    source = data.get("source", "deterministic")
    if source not in {"deterministic", "llm"}:
      raise IntentError(f"unknown source: {source!r}")
    return cls(
        name=name,
        confidence=float(confidence),
        entities=dict(entities),
        source=source,
    )


_UNIT_CONVERT = re.compile(
    r"^(?:convert|change)\s+(?P<value>-?\d+(?:\.\d+)?)\s*"
    r"(?P<source>[a-z°]+)\s+(?:to|into)\s+(?P<target>[a-z°]+)$"
)
_UNIT_IN = re.compile(
    r"^how many\s+(?P<target>[a-z]+)\s+(?:are\s+)?in\s+"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<source>[a-z°]+)$"
)
_TEXT_CONVERT = re.compile(
    r"^convert\s+(?P<text>.+?)\s+to\s+"
    r"(?P<operation>uppercase|lowercase|title case|reverse)$"
)
_TEXT_OP = re.compile(
    r"^(?P<operation>uppercase|upper case|lowercase|lower case|"
    r"title case|reverse|capitalize|capitalise)\s+"
    r"(?:(?:the|this)\s+text\s+)?(?P<text>.+)$"
)
_TEXT_COUNT = re.compile(
    r"^how many\s+(?P<operation>words|characters|chars|letters)\s+"
    r"(?:are\s+)?in\s+(?P<text>.+)$"
)
_SIZE_FORMAT = re.compile(
    r"^(?:format|show)\s+(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>b|bytes?|kb|mb|gb|tb)\s+(?:as\s+)?"
    r"(?:human[- ]readable|size)$"
)
_SIZE_HUMAN = re.compile(
    r"^(?:what is|how big is|human readable)\s+"
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>b|bytes?|kb|mb|gb|tb)$"
)
_WEATHER = re.compile(
    r"^(?:what(?:'s| is)|how(?:'s| is))\s+(?:the\s+)?weather"
    r"(?:\s+(?:like|today|now))?"
    r"(?:\s+(?:in|at|for)\s+(?P<place>.+))?$"
)
_WEATHER_ALT = re.compile(
    r"^(?:weather|forecast)\s+(?:in|for|at)\s+(?P<place>.+)$"
)
_TIMER_LABEL = re.compile(
    r"^(?:set\s+)?(?:a\s+)?timer\s+(?:for\s+)?(?P<duration>.+?)"
    r"\s+(?:called|named)\s+(?P<label>.+)$"
)
_TIMER = re.compile(
    r"^(?:set\s+)?(?:a\s+)?timer\s+(?:for\s+)?(?P<duration>.+)$"
)
_TIMER_LIST = re.compile(
    r"^(?:list|show)\s+(?:my\s+)?(?:timers?|reminders?)$"
    r"|^what\s+(?:timers?|reminders?)\s+(?:are\s+running|do\s+i\s+have)$"
)
_TIMER_CANCEL = re.compile(
    r"^(?:cancel|stop|delete)\s+(?P<all>all\s+)?"
    r"(?P<kind>timers?|reminders?)(?:\s+(?P<id>\d+))?$"
)


def recognize_utilities(text):
  """Recognize conversions, text operations and size formatting."""
  lowered = (text or "").lower().strip().rstrip("?.!")
  if not lowered:
    return None
  match = _UNIT_CONVERT.match(lowered) or _UNIT_IN.match(lowered)
  if match:
    value = float(match.group("value"))
    source = match.group("source")
    target = match.group("target")
    try:
      result, unit = convert(value, source, target)
    except UtilityError as exc:
      return Intent(
          "unit_convert",
          0.9,
          {
              "value": value,
              "source": source,
              "target": target,
              "error": str(exc),
          },
      )
    return Intent(
        "unit_convert",
        0.92,
        {
            "value": value,
            "source": source,
            "target": target,
            "result": format_quantity(result),
            "unit": unit,
        },
    )
  match = _TEXT_CONVERT.match(lowered) or _TEXT_OP.match(lowered)
  if match:
    return _text_intent(match.group("operation"), match.group("text"))
  match = _TEXT_COUNT.match(lowered)
  if match:
    return _text_intent(match.group("operation"), match.group("text"))
  match = _SIZE_FORMAT.match(lowered) or _SIZE_HUMAN.match(lowered)
  if match:
    return Intent(
        "format_size",
        0.92,
        {"result": format_size(match.group("value"), match.group("unit"))},
    )
  return None


def _text_intent(operation, body):
  value = str(body or "").strip().strip('"').strip("'")
  try:
    result = transform_text(operation, value)
  except UtilityError as exc:
    return Intent(
        "text_transform",
        0.9,
        {"operation": operation, "text": value, "error": str(exc)},
    )
  return Intent(
      "text_transform",
      0.92,
      {"operation": operation, "text": value, "result": result},
  )


def recognize_weather(text):
  lowered = (text or "").lower().strip().rstrip("?.!")
  if not lowered:
    return None
  match = _WEATHER.match(lowered) or _WEATHER_ALT.match(lowered)
  if not match:
    return None
  place = _clean(match.group("place") or "")
  return Intent("weather", 0.9, {"location": place})


def recognize_timers(text):
  """Recognize timers and reminders, including their absolute due time."""
  lowered = (text or "").lower().strip().rstrip("?.!")
  if not lowered:
    return None
  if lowered.startswith("remind me"):
    parsed = parse_reminder(lowered)
    if parsed:
      due, task = parsed
      return Intent(
          "reminder_set",
          0.9,
          {
              "due_at": due.isoformat(timespec="minutes"),
              "text": task,
          },
      )
    return None
  if _TIMER_LIST.match(lowered):
    return Intent("timer_list", 0.9, {})
  match = _TIMER_CANCEL.match(lowered)
  if match:
    return Intent(
        "timer_cancel",
        0.9,
        {
            "kind": match.group("kind").rstrip("s"),
            "id": int(match.group("id")) if match.group("id") else 0,
            "all": bool(match.group("all")),
        },
    )
  match = _TIMER_LABEL.match(lowered) or _TIMER.match(lowered)
  if match:
    seconds = parse_duration(match.group("duration"))
    if not seconds:
      return None
    return Intent(
        "timer_set",
        0.92,
        {
            "seconds": seconds,
            "label": _clean(match.groupdict().get("label") or ""),
        },
    )
  return None


def recognize_time(text):
  lowered = text.lower()
  if any(re.search(pattern, lowered) for pattern in _TIME_PATTERNS):
    return Intent(name="time", confidence=0.92)
  return None


def recognize_date(text):
  lowered = text.lower()
  if not any(re.search(pattern, lowered) for pattern in _DATE_PATTERNS):
    return None
  reference = find_date_reference(lowered)
  entities = reference or {}
  return Intent(
      name="date",
      confidence=0.92 if reference else 0.85,
      entities=entities,
  )


def recognize_math(text):
  """Recognize a pure arithmetic question and evaluate it locally."""
  candidate = _math_expression(text)
  if candidate is None:
    return None
  entities = {"expression": candidate}
  try:
    entities["result"] = evaluate(candidate)
  except CalculationError as exc:
    entities["error"] = str(exc)
  return Intent(name="math", confidence=0.95, entities=entities)


def _math_expression(text):
  lowered = text.lower().strip().rstrip("?.!")
  for word, symbol in _MATH_WORDS:
    lowered = re.sub(rf"\b{re.escape(word)}\b", symbol, lowered)
  lowered = _MATH_PREFIX.sub("", lowered)
  lowered = _MATH_FILLER.sub(" ", lowered)
  lowered = lowered.replace("^", "**").strip()
  lowered = lowered.strip(" =")
  if not lowered or not re.search(r"\d", lowered):
    return None
  if not re.search(r"[+\-*/%]", lowered):
    return None
  # Leftover letters mean this is a word problem, not a calculation.
  if re.search(r"[a-z]", lowered):
    return None
  return lowered


def _clean(value):
  return str(value or "").strip().strip('"').strip("'").strip()


# A spoken request often packs several actions into one sentence. Only the
# first action is handled, so paths stop at the next instruction instead of
# swallowing it ("create a folder named docs & write readme.md in it").
_ACTION_WORD = (
    r"(?:create|make|write|put|add|save|delete|remove|read|list|open"
    r"|launch|run|move|copy|rename|close|send|set|find|search|in|inside)"
)
_CLAUSE_BREAK = re.compile(
    rf"\s*(?:;|&|\bthen\b|,\s*(?:and|then)\s+(?={_ACTION_WORD}\b)"
    rf"|\s+(?:and|then)\s+(?={_ACTION_WORD}\b))\s*"
)
_LOCATION_SUFFIX = re.compile(
    r"\s+(?:on|in|at)\s+(?:the\s+|my\s+)?"
    r"(?P<where>desktop|documents|downloads)\b.*$"
)
_HOME_FOLDERS = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
}


def _first_clause(text):
  """Return the part of a request before the next instruction."""
  match = _CLAUSE_BREAK.search(text)
  return (text[: match.start()] if match else text).strip(" ,.;")


def _target_path(text, named=False):
  """Extract one path from a request without its extra instructions."""
  value = _first_clause(_clean(text))
  if named:
    value = re.sub(r"^(?:named|called)\s+", "", value).strip()
  location = _LOCATION_SUFFIX.search(value)
  if location:
    name = value[: location.start()].strip()
    where = _HOME_FOLDERS[location.group("where")]
    return f"~/{where}/{name}" if name else ""
  if named:
    parent = re.search(r"\s+in\s+(?P<parent>[^\s]+)\s*$", value)
    if parent and parent.group("parent").lower() not in _HOME_FOLDERS:
      name = value[: parent.start()].strip()
      if name:
        return f"{parent.group('parent').strip('/')}/{name}"
  return value


def _looks_like_url(token):
  text = _clean(token)
  if not text:
    return False
  if re.match(r"^(?:https?://|www\.)", text, re.I):
    return True
  host = text.split("/")[0]
  if "." not in host:
    return False
  return host.rsplit(".", 1)[-1].lower() in _TLDS


def _looks_like_path(token):
  text = _clean(token)
  if not text:
    return False
  if "\\" in text or "/" in text or text.startswith("."):
    return True
  if "." in text:
    return text.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS
  return False


_WEB_PATTERNS = (
    (
        r"^(?:search|look)\s+(?:the\s+)?(?:web|internet|online)\s+for\s+"
        r"(?P<query>.+)$",
        "",
    ),
    (
        r"^(?:search|look)\s+for\s+(?P<query>.+?)\s+(?:online"
        r"|on\s+the\s+(?:web|internet))$",
        "",
    ),
    (r"^(?:google|look up)\s+(?P<query>.+)$", ""),
    (
        r"^(?:find|get)\s+(?:me\s+)?(?:information|info|details)\s+"
        r"(?:about|on)\s+(?P<query>.+)$",
        "",
    ),
    (
        r"^(?:what(?:'s| is)\s+)?(?:the\s+)?latest\s+news\s+"
        r"(?:about|on)\s+(?P<query>.+)$",
        " news",
    ),
    (r"^(?:news|headlines)\s+(?:about|on)\s+(?P<query>.+)$", " news"),
)

_WIKIPEDIA_RE = re.compile(r"^wikipedia\s+(?:for\s+)?(?P<query>.+)$")


def recognize_web(text):
  """Recognize an explicit request to look something up on the web."""
  lowered = (text or "").lower().strip().rstrip("?.!")
  if not lowered:
    return None
  match = _WIKIPEDIA_RE.match(lowered)
  if match:
    query = _clean(match.group("query"))
    if len(query) >= 2:
      return Intent(
          "web_search", 0.9, {"query": query, "wikipedia": True}
      )
  for pattern, suffix in _WEB_PATTERNS:
    match = re.match(pattern, lowered)
    if not match:
      continue
    query = _clean(match.group("query"))
    if len(query) < 2:
      return None
    return Intent("web_search", 0.88, {"query": query + suffix})
  return None


_MEMORY_REMEMBER = (
    re.compile(
        r"^(?:please\s+)?remember\s+(?:(?P<temporary>for\s+now)\s+)?"
        r"(?:that\s+)?(?P<content>.+)$"
    ),
    re.compile(
        r"^(?:please\s+)?don'?t\s+forget\s+(?:that\s+)?(?P<content>.+)$"
    ),
    re.compile(r"^(?P<content>.+?)[,;]?\s+remember\s+(?:that|this)$"),
)
_MEMORY_LIST = re.compile(
    r"^(?:show|list)\s+(?:me\s+)?(?:your\s+)?(?:memories|memory)$"
)
_MEMORY_RECALL = re.compile(
    r"^(?:what|who|where|when)\s+do\s+you\s+remember"
    r"(?:\s+about\s+(?P<query>.+))?$"
)
_MEMORY_SEARCH = re.compile(
    r"^(?:do\s+you\s+remember\s+(?P<query>.+?)"
    r"|search\s+(?:your\s+)?memory\s+for\s+(?P<term>.+?)"
    r"|look\s+(?:in|through)\s+(?:your\s+)?memory\s+for\s+(?P<item>.+?))$"
)
_MEMORY_DELETE = re.compile(
    r"^(?:forget|delete)\s+(?:the\s+)?memor(?:y|ies)"
    r"(?:\s+(?:about|of|for))?\s+(?P<query>.+)$"
)
_MEMORY_LEARNED = re.compile(
    r"^(?:show|list)\s+(?:me\s+)?(?:what\s+you(?:'ve| have)\s+learned"
    r"|learned\s+memories)$"
    r"|^what\s+have\s+you\s+learned(?:\s+about\s+me)?$"
)
_MEMORY_EPISODES = re.compile(
    r"^(?:show|list)\s+(?:me\s+)?(?:recent\s+)?"
    r"(?:episodes|events)$"
    r"|^what\s+(?:have\s+we\s+(?:talked|spoken)\s+about"
    r"|did\s+we\s+(?:talk|discuss)\s+about)$"
)


def recognize_memory(text):
  """Recognize a request to save, search, list or delete memories."""
  source = (text or "").strip().rstrip("?.!")
  lowered = source.lower()
  if not lowered:
    return None
  for pattern in _MEMORY_REMEMBER:
    match = pattern.match(lowered)
    if not match:
      continue
    start, end = match.span("content")
    content = _clean(source[start:end])
    if len(content) < 2:
      return None
    return Intent(
        "remember_memory",
        0.9,
        {
            "content": content,
            "category": classify_memory(content),
            "temporary": bool(match.groupdict().get("temporary")),
        },
    )
  if _MEMORY_LEARNED.match(lowered):
    return Intent("learned_memory", 0.9, {})
  if _MEMORY_EPISODES.match(lowered):
    return Intent("memory_episodes", 0.9, {})
  if _MEMORY_LIST.match(lowered):
    return Intent("list_memory", 0.9, {})
  match = _MEMORY_RECALL.match(lowered)
  if match:
    query = _clean(match.group("query") or "")
    if query:
      return Intent("search_memory", 0.9, {"query": query})
    return Intent("list_memory", 0.9, {})
  match = _MEMORY_SEARCH.match(lowered)
  if match:
    query = _clean(
        match.group("query")
        or match.group("term")
        or match.group("item")
        or ""
    )
    if query:
      return Intent("search_memory", 0.9, {"query": query})
  match = _MEMORY_DELETE.match(lowered)
  if match:
    query = _clean(match.group("query"))
    if len(query) >= 2:
      return Intent("delete_memory", 0.9, {"query": query})
  return None


def recognize_computer(text, allowed_apps=(), allowed_commands=()):
  """Recognize a computer-interaction request and extract its arguments."""
  lowered = text.lower().strip().rstrip("?.!")
  if not lowered:
    return None
  apps = {str(name).strip().lower() for name in allowed_apps}
  commands = {str(name).strip().lower() for name in allowed_commands}

  if "screenshot" in lowered or "screen shot" in lowered:
    if re.search(r"\b(take|grab|capture|get|make)\b", lowered) or lowered in {
        "screenshot",
        "screen shot",
    }:
      return Intent("screenshot", 0.95)

  if (
      "clipboard" in lowered
      and not re.match(r"^(?:copy|put)\b", lowered)
      and re.search(r"\b(what|what's|read|show|see|check|peek|paste)\b", lowered)
  ):
    return Intent("clipboard_read", 0.9)

  match = re.match(
      r"^(?:copy|put) (?P<text>.+?) (?:to|into|on) (?:the |my )?clipboard$",
      lowered,
  )
  if match:
    return Intent(
        "clipboard_copy", 0.9, {"text": _clean(match.group("text"))}
    )

  match = re.match(
      r"^(?:notify me|show (?:a )?notification|send (?:a )?notification)"
      r"(?: (?:that|about|saying))?[:\s]+(?P<message>.+)$",
      lowered,
  )
  if match:
    return Intent("notify", 0.9, {"message": match.group("message").strip()})

  if re.fullmatch(
      r"(?:list|show) (?:the )?(?:open )?windows", lowered
  ) or "what windows are open" in lowered:
    return Intent("windows_list", 0.9)

  match = re.match(
      r"^(?P<action>minimize|maximize|focus|switch to|bring up)"
      r" (?:the )?(?P<title>.+?)(?: window)?$",
      lowered,
  )
  if match:
    action = match.group("action")
    if action in {"switch to", "bring up"}:
      action = "focus"
    return Intent(
        "window_action",
        0.9,
        {"title": _clean(match.group("title")), "action": action},
    )

  if (
      "system status" in lowered
      or "system information" in lowered
      or "how is the system" in lowered
  ):
    return Intent("system_info", 0.9, {"kind": "all"})
  if re.search(r"\bbattery\b", lowered):
    return Intent("system_info", 0.9, {"kind": "battery"})
  if re.search(r"\b(cpu|processor)\b", lowered) and re.search(
      r"\b(usage|load|percent|using|used|busy|status|much)\b", lowered
  ):
    return Intent("system_info", 0.9, {"kind": "cpu"})
  if re.search(r"\b(ram|memory)\b", lowered) and re.search(
      r"\b(usage|used|free|available|much|percent|left)\b", lowered
  ):
    return Intent("system_info", 0.9, {"kind": "memory"})
  if re.search(r"\b(disk|storage|drive)\b", lowered) and re.search(
      r"\b(space|free|usage|full|much|percent|left)\b", lowered
  ):
    return Intent("system_info", 0.9, {"kind": "disk"})

  if re.search(r"\b(processes|tasklist|task list)\b", lowered) and re.search(
      r"\b(list|top|running|show|what|which)\b", lowered
  ):
    return Intent("processes", 0.9)

  match = re.match(
      r"^(?:read|show me|display|what'?s in) (?:the )?file (?P<path>.+)$",
      lowered,
  )
  if match:
    path = _target_path(match.group("path"))
    if path:
      return Intent("read_file", 0.9, {"path": path})

  match = re.match(
      r"^(?:write|save) (?P<content>.+?) (?:to|into) (?:the )?file"
      r" (?P<path>.+)$",
      lowered,
  )
  if match:
    path = _target_path(match.group("path"))
    if path:
      return Intent(
          "write_file",
          0.9,
          {
              "path": path,
              "content": _clean(match.group("content")),
          },
      )

  match = re.match(
      r"^create (?:a )?(?:file|note) (?P<path>\S+?)"
      r"(?: with (?:(?:content|text) )?(?P<content>.+))?$",
      lowered,
  )
  if match:
    return Intent(
        "write_file",
        0.9,
        {
            "path": _clean(match.group("path")),
            "content": _clean(match.group("content") or ""),
        },
    )

  match = re.match(
      r"^create (?:a )?(?:new )?(?:folder|directory) (?P<path>.+)$",
      lowered,
  )
  if match:
    path = _target_path(match.group("path"), named=True)
    if path:
      return Intent("create_folder", 0.9, {"path": path})

  match = re.match(
      r"^(?:delete|remove) (?:the )?(?:file|folder|directory)"
      r" (?P<path>.+)$",
      lowered,
  )
  if match:
    path = _target_path(match.group("path"))
    if path:
      return Intent("delete_path", 0.9, {"path": path})

  match = re.match(
      r"^(?:list|show) (?:the )?files(?: in (?P<path>.+))?$", lowered
  )
  if match:
    return Intent(
        "list_files", 0.9, {"path": _target_path(match.group("path") or ".")}
    )

  match = re.match(
      r"^(?:find|search(?: for)?) (?:files? )?(?:named |called )?"
      r"(?P<name>\S+?)(?: in (?P<path>.+))?$",
      lowered,
  )
  if match:
    return Intent(
        "search_files",
        0.9,
        {
            "name": _clean(match.group("name")),
            "path": _target_path(match.group("path") or "."),
        },
    )

  match = re.match(r"^rename (?P<source>\S+) to (?P<target>.+)$", lowered)
  if match:
    return Intent(
        "rename_path",
        0.9,
        {
            "source": _clean(match.group("source")),
            "target": _target_path(match.group("target")),
        },
    )

  match = re.match(r"^copy (?P<source>\S+) to (?P<target>.+)$", lowered)
  if match:
    return Intent(
        "copy_path",
        0.9,
        {
            "source": _clean(match.group("source")),
            "target": _target_path(match.group("target")),
        },
    )

  match = re.match(r"^move (?P<source>\S+) to (?P<target>.+)$", lowered)
  if match:
    return Intent(
        "move_path",
        0.9,
        {
            "source": _clean(match.group("source")),
            "target": _target_path(match.group("target")),
        },
    )

  if re.fullmatch(
      r"(?:close|quit|stop|shut down|shutdown) (?:all|every|everything)"
      r"(?: (?:the )?(?:apps|applications|programs|windows))?",
      lowered,
  ):
    return Intent("close_all_apps", 0.95)

  match = re.match(
      r"^(?:open|launch|start) (?:(?:the) )?(?:file )?(?P<target>.+)$",
      lowered,
  )
  if match:
    target = _clean(match.group("target"))
    if _looks_like_path(target) and not _looks_like_url(target):
      return Intent("open_path", 0.9, {"path": target})
    if _looks_like_url(target):
      return Intent("open_url", 0.9, {"url": target})
    name = target[:-4] if target.endswith(".exe") else target
    if name in apps:
      return Intent("open_app", 0.9, {"application": name})
    return None

  match = re.match(
      r"^(?:close|quit) (?:(?:the) )?(?P<target>.+?)"
      r"(?: (?:window|app|application))?$",
      lowered,
  )
  if match:
    target = _clean(match.group("target"))
    name = target[:-4] if target.endswith(".exe") else target
    if name in apps:
      return Intent("close_app", 0.9, {"application": name})
    return None

  match = re.match(
      r"^(?:run|execute) (?:(?:the )?command )?"
      r"(?P<command>[a-z0-9_.-]+)(?: (?P<arguments>.+))?$",
      lowered,
  )
  if match and match.group("command") in commands:
    return Intent(
        "run_command",
        0.9,
        {
            "command": match.group("command"),
            "arguments": (match.group("arguments") or "").strip(),
        },
    )

  return None


# -- multi-step file plans ----------------------------------------------

_FILE_VERB = (
    r"(?:create|make|write|save|delete|remove|read|list|rename|copy|move"
    r"|find|search|add|put)"
)
_PLAN_SPLIT = re.compile(
    rf"\s*(?:;|\bthen\b"
    rf"|&\s*(?=(?:{_FILE_VERB}\b|in that folder\b|inside that folder\b"
    rf"|in it\b|inside it\b))"
    rf"|,\s*(?={_FILE_VERB}\b)"
    rf"|\band\b\s*(?=(?:{_FILE_VERB}\b|in that folder\b"
    rf"|inside that folder\b|in it\b|inside it\b)))\s*"
)
_FOLDER_PREFIX = re.compile(
    r"^(?:in|inside|into)\s+(?:that|the|this)\s+folder\b[,:]?\s*"
    r"|^(?:in|inside)\s+it\b[,:]?\s*"
)
_MANY_FILES = re.compile(
    r"^create\s+(?:(?P<count>\d+|two|three|four|five)\s+)?files?"
    r"(?:\s+(?:named|called))?\s*[:\-]?\s*(?P<names>.+)$"
)
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5}


def _expand_file_list(clause):
  """Turn "create two files - a.txt & b.txt" into one action per file."""
  match = _MANY_FILES.match(clause)
  if not match or not match.group("count"):
    return None
  raw = match.group("count")
  expected = int(raw) if raw.isdigit() else _COUNT_WORDS.get(raw, 0)
  names = [
      _clean(part)
      for part in re.split(r"\s*(?:,|&|\band\b)\s*", match.group("names"))
      if _clean(part)
  ]
  if not expected or len(names) != expected:
    return None
  return [
      Intent("write_file", 0.9, {"path": name, "content": ""})
      for name in names
  ]


def _inside_folder(arguments, folder):
  """Prefix relative paths in an action with the folder just created."""
  if not folder:
    return dict(arguments)
  updated = dict(arguments)
  for key in ("path", "source", "target"):
    value = str(updated.get(key) or "")
    if value and not value.startswith(("~", "/")) and ":" not in value:
      updated[key] = f"{folder}/{value}"
  return updated


def _merge_prefixes(clauses):
  """Re-attach a bare "in that folder" to the action that follows it."""
  merged = []
  prefix = ""
  for clause in clauses:
    if _FOLDER_PREFIX.fullmatch(clause):
      prefix = clause
      continue
    merged.append(f"{prefix} {clause}".strip() if prefix else clause)
    prefix = ""
  if prefix:
    merged.append(prefix)
  return merged


def plan_file_actions(text, allowed_apps=(), allowed_commands=()):
  """Split one sentence into an ordered plan of file actions.

  Every clause must state an explicit action. The plan stops at the first
  clause that is not understood, so nothing is guessed, and ``None`` is
  returned unless at least two actions were found.
  """
  lowered = (text or "").lower().strip().rstrip("?.!")
  if not lowered or lowered.startswith("/"):
    return None
  clauses = [
      clause.strip(" ,;.-")
      for clause in _PLAN_SPLIT.split(lowered)
      if clause.strip(" ,;.-")
  ]
  clauses = _merge_prefixes(clauses)
  actions = []
  folder = ""
  for clause in clauses:
    prefix = _FOLDER_PREFIX.match(clause)
    scoped = prefix is not None
    if prefix:
      clause = clause[prefix.end():].strip()
    expanded = _expand_file_list(clause)
    if expanded is None:
      intent = recognize_computer(clause, allowed_apps, allowed_commands)
      if intent is None or intent.name not in FILE_INTENTS:
        break
      expanded = [intent]
    for intent in expanded:
      # A folder created in the same sentence is a sibling unless the user
      # asked for it inside the previous one.
      if intent.name == "create_folder" and not scoped:
        arguments = dict(intent.entities)
      else:
        arguments = _inside_folder(intent.entities, folder)
      actions.append((intent.name, arguments, clause))
      if intent.name == "create_folder":
        folder = arguments["path"]
  if len(actions) < 2:
    return None
  return actions


class IntentRouter:
  """Choose a deterministic intent for a request, or None to use the LLM."""

  def __init__(
      self,
      confidence_threshold=CONFIDENCE_THRESHOLD,
      allowed_apps=(),
      allowed_commands=(),
  ):
    self.threshold = confidence_threshold
    self.allowed_apps = tuple(allowed_apps)
    self.allowed_commands = tuple(allowed_commands)

  def route(self, text):
    text = (text or "").strip()
    if not text:
      return None
    if text.startswith("/"):
      parts = text.split(maxsplit=1)
      name = parts[0].lstrip("/").lower()
      if name in {"web", "search"} and len(parts) == 2:
        query = parts[1].strip()
        if query:
          return Intent("web_search", 1.0, {"query": query})
        return None
      if name in COMMANDS:
        return Intent(name=name, confidence=1.0)
      return None

    intent = recognize_computer(
        text, self.allowed_apps, self.allowed_commands
    )
    if intent is not None:
      return intent
    intent = recognize_web(text)
    if intent is not None:
      return intent
    intent = recognize_memory(text)
    if intent is not None:
      return intent
    for recognizer in (
        recognize_utilities,
        recognize_weather,
        recognize_timers,
    ):
      intent = recognizer(text)
      if intent is not None:
        return intent
    for recognizer in (recognize_time, recognize_date, recognize_math):
      intent = recognizer(text)
      if intent is not None:
        return intent
    return None

  def is_confident(self, intent):
    return intent is not None and intent.confidence >= self.threshold
