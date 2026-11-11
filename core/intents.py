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

from core.goals import parse_goal_input
from core.habits import parse_habit_input
from core.memory import classify as classify_memory
from core.productivity import parse_todo_input
from core.calendar import parse_event_input
from core.mail import parse_email_input
from core.time_tracking import _report_date
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
        "document_convert",
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
        "todo_add",
        "todo_list",
        "todo_show",
        "todo_done",
        "todo_delete",
        "todo_snooze",
        "plan_day",
        "plan_week",
        "note_add",
        "note_list",
        "note_show",
        "note_search",
        "note_delete",
        "habit_add",
        "habit_list",
        "habit_show",
        "habit_done",
        "habit_undo",
        "habit_stats",
        "habit_delete",
        "goal_add",
        "goal_list",
        "goal_show",
        "goal_progress",
        "goal_complete",
        "goal_reopen",
        "goal_delete",
        "time_start",
        "time_stop",
        "time_status",
        "time_report",
        "time_week",
        "time_list",
        "time_delete",
        "calendar_add",
        "calendar_agenda",
        "calendar_show",
        "calendar_delete",
        "email_draft",
        "email_list",
        "email_show",
        "email_delete",
        "email_send",
        "email_search",
        "email_read",
        "email_summarize",
        "email_reply",
        "pomodoro_start",
        "pomodoro_status",
        "pomodoro_stop",
        "pomodoro_skip",
        "clipboard_list",
        "clipboard_search",
        "clipboard_show",
        "clipboard_delete",
        "clipboard_clear",
        "summary_daily",
        "summary_meeting",
        "developer_explain",
        "developer_inspect",
        "task_organize",
        "task_plan",
        "task_list",
        "task_show",
        "task_approve",
        "task_resume",
        "task_cancel",
        "task_history",
        "document_read",
        "document_index",
        "document_search",
        "document_ask",
        "document_summarize",
        "document_compare",
        "document_dates",
        "document_actions",
        "document_tables",
        "document_explain",
        "document_generate",
        "document_report",
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
        "tasks",
        "plan",
        "note",
        "notes",
        "habit",
        "habits",
        "goal",
        "goals",
        "time",
        "calendar",
        "cal",
        "email",
        "mail",
        "pomodoro",
        "pomo",
        "clipboard",
        "summary",
        "summaries",
        "dev",
        "developer",
        "todo",
        "todos",
        "documents",
        "docs",
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

_TODO_ADD = re.compile(
    r"^(?:please\s+)?(?:add|create|make)\s+(?:a\s+|an\s+)?(?:new\s+)?"
    r"(?:to-?do|todo item|to-?do item)\s*[:\-]?\s*(?P<body>.+)$",
    re.I,
)
_TODO_ADD_TO = re.compile(
    r"^(?:please\s+)?(?:add|put)\s+(?P<body>.+?)\s+"
    r"(?:to|on)\s+(?:my\s+)?(?:to-?do|todo)\s+list$",
    re.I,
)
_TODO_PREFIX = re.compile(r"^to-?do\s*[:\-]\s*(?P<body>.+)$", re.I)
_TODO_LIST = re.compile(
    r"^(?:list|show|what(?:'s| is)? on)\s+(?:my\s+)?"
    r"(?:to-?dos?|to-?do list|todo list)$"
    r"|^(?:what|which)\s+(?:are\s+)?(?:my\s+)?(?:to-?dos?|todo list)$",
    re.I,
)
_TODO_DONE = re.compile(
    r"^(?:(?:mark|set)\s+)?(?:to-?do|todo)\s+#?(?P<id>\d+)\s+"
    r"(?:as\s+)?(?:done|complete[d]?)$",
    re.I,
)
_TODO_DONE_ALT = re.compile(
    r"^(?:complete|finish|done)\s+(?:to-?do|todo)\s+#?(?P<id>\d+)$",
    re.I,
)
_TODO_SHOW = re.compile(
    r"^(?:show|describe|details?|open)\s+(?:to-?do|todo)\s+#?(?P<id>\d+)$",
    re.I,
)
_TODO_DELETE = re.compile(
    r"^(?:delete|remove|cancel)\s+(?:to-?do|todo)\s+#?(?P<id>\d+)$",
    re.I,
)
_TODO_SNOOZE = re.compile(
    r"^snooze\s+(?:to-?do|todo)\s+#?(?P<id>\d+)"
    r"(?:\s+(?:for|by)\s+(?P<duration>.+))?$",
    re.I,
)
_PLAN_DAY = re.compile(
    r"^(?:what(?:'s| is)\s+)?(?:my\s+)?(?:daily\s+)?plan"
    r"(?:\s+(?:for\s+)?(?:my\s+)?day)?"
    r"(?:\s+(?:for|on)\s+(?P<when_for>.+))?"
    r"(?:\s+(?P<when_bare>today|tomorrow|tonight|this\s+week"
    r"|next\s+week))?$",
    re.I,
)
_PLAN_WEEK = re.compile(
    r"^(?:what(?:'s| is)\s+)?(?:my\s+)?"
    r"(?:(?:weekly|week)\s+plan"
    r"|plan\s+(?:for\s+)?(?:my\s+)?(?P<when>this|next)?\s*week)$",
    re.I,
)
_NOTE_ADD = re.compile(
    r"^(?:please\s+)?(?:take|make|write|add|save)\s+"
    r"(?:a\s+|an\s+)?(?:quick\s+|new\s+)?note"
    r"(?:\s+(?:that|saying|about))?\s*[:\-]?\s*(?P<body>.+)$",
    re.I,
)
_NOTE_ALT = re.compile(
    r"^(?:(?:quick|new)\s+note|note)\s*(?:that|:|-|,)\s*"
    r"(?P<body>.+)$"
    r"|^(?:jot\s+down|note\s+down)\s+(?P<jot>.+)$"
    r"|^(?:quick|new)\s+note\s+(?P<quick>.+)$"
    r"|^note\s+(?P<plain>[a-z].{2,})$",
    re.I,
)
_NOTE_LIST = re.compile(
    r"^(?:(?:list|show|read)\s+(?:me\s+)?(?:my\s+)?notes"
    r"|what(?:'s| is)?\s+(?:in\s+)?my\s+notes"
    r"|what\s+(?:are\s+)?my\s+notes"
    r"|my\s+notes)$",
    re.I,
)
_NOTE_SHOW = re.compile(
    r"^(?:show|open|read|display)\s+note\s+#?(?P<id>\d+)$", re.I
)
_NOTE_DELETE = re.compile(
    r"^(?:delete|remove)\s+note\s+#?(?P<id>\d+)$", re.I
)
_NOTE_SEARCH = re.compile(
    r"^(?:search|find|look\s+through)\s+(?:my\s+)?notes"
    r"\s+(?:for|about|on)\s+(?P<query>.+)$"
    r"|^notes\s+(?:about|on)\s+(?P<about>.+)$"
    r"|^what\s+do\s+my\s+notes\s+say\s+(?:about|on)\s+(?P<say>.+)$",
    re.I,
)
_HABIT_ADD = re.compile(
    r"^(?:please\s+)?(?:add|create|start|track)\s+(?:a\s+|an\s+)?"
    r"(?:new\s+)?habit\s+(?:to\s+)?(?P<body>.+)$",
    re.I,
)
_HABIT_ADD_ALT = re.compile(
    r"^(?:please\s+)?(?:add|create|start|track)\s+"
    r"(?P<body>.+?)\s+as\s+a\s+habit$",
    re.I,
)
_HABIT_LIST = re.compile(
    r"^(?:(?:list|show)\s+(?:me\s+)?(?:my\s+)?habits"
    r"|what\s+(?:are\s+)?my\s+habits"
    r"|my\s+habits)$",
    re.I,
)
_HABIT_SHOW = re.compile(
    r"^(?:show|describe|details?|open)\s+habit\s+#?(?P<id>\d+)$", re.I
)
_HABIT_DONE = re.compile(
    r"^(?:(?:mark|set)\s+)?habit\s+#?(?P<id>\d+)\s+"
    r"(?:as\s+)?(?:done|complete[d]?)$"
    r"|^(?:complete|finish|check\s+off|did)\s+habit\s+#?(?P<id2>\d+)$"
    r"|^habit\s+#?(?P<id3>\d+)\s+(?:done|complete[d]?)$",
    re.I,
)
_HABIT_UNDO = re.compile(
    r"^(?:undo|uncheck|reset)\s+habit\s+#?(?P<id>\d+)$", re.I
)
_HABIT_STATS = re.compile(
    r"^habits?\s+stats$"
    r"|^(?:show|list)\s+(?:my\s+)?habit\s+streaks$"
    r"|^what(?:'s| is)?\s+my\s+habit\s+streaks?$",
    re.I,
)
_HABIT_DELETE = re.compile(
    r"^(?:delete|remove)\s+habit\s+#?(?P<id>\d+)$", re.I
)
_GOAL_ADD = re.compile(
    r"^(?:please\s+)?(?:add|create|set|start)\s+(?:a\s+|an\s+)?"
    r"(?:new\s+)?goal\s+(?:to\s+)?(?P<body>.+)$",
    re.I,
)
_GOAL_ADD_ALT = re.compile(
    r"^(?:please\s+)?(?:add|create|set|start)\s+"
    r"(?P<body>.+?)\s+as\s+a\s+goal$",
    re.I,
)
_GOAL_LIST = re.compile(
    r"^(?:(?:list|show)\s+(?:me\s+)?(?:my\s+)?goals"
    r"|what\s+(?:are\s+)?my\s+goals"
    r"|my\s+goals)$",
    re.I,
)
_GOAL_SHOW = re.compile(
    r"^(?:show|describe|details?|open)\s+goal\s+#?(?P<id>\d+)$", re.I
)
_GOAL_PROGRESS = re.compile(
    r"^(?:set|update)\s+goal\s+#?(?P<id>\d+)\s+"
    r"(?:progress\s+)?(?:to\s+)?(?P<value>\d+(?:\.\d+)?)$"
    r"|^goal\s+#?(?P<id2>\d+)\s+progress\s+"
    r"(?P<value2>\d+(?:\.\d+)?)$"
    r"|^(?:add\s+)?(?P<value3>\d+(?:\.\d+)?)\s+to\s+goal\s+"
    r"#?(?P<id3>\d+)$"
    r"|^goal\s+#?(?P<id4>\d+)\s+add\s+"
    r"(?P<value4>\d+(?:\.\d+)?)$",
    re.I,
)
_GOAL_COMPLETE = re.compile(
    r"^(?:complete|finish|mark)\s+goal\s+#?(?P<id>\d+)"
    r"(?:\s+(?:as\s+)?(?:done|complete[d]?))?$"
    r"|^goal\s+#?(?P<id2>\d+)\s+(?:done|complete[d]?)$",
    re.I,
)
_GOAL_REOPEN = re.compile(
    r"^(?:reopen|re-open|restart)\s+goal\s+#?(?P<id>\d+)$", re.I
)
_GOAL_DELETE = re.compile(
    r"^(?:delete|remove)\s+goal\s+#?(?P<id>\d+)$", re.I
)
_TIME_START = re.compile(
    r"^(?:start|begin)\s+(?:tracking|time\s+tracking)"
    r"(?:\s+(?:on|for))?\s+(?P<label>.+)$"
    r"|^(?:start|begin)\s+(?:the\s+)?clock\s+(?:on|for)\s+"
    r"(?P<clock>.+)$",
    re.I,
)
_TIME_STOP = re.compile(
    r"^(?:stop|end|finish)\s+(?:tracking|time\s+tracking|the\s+clock)$",
    re.I,
)
_TIME_STATUS = re.compile(
    r"^(?:what\s+am\s+i\s+tracking|time\s+tracking\s+status"
    r"|am\s+i\s+tracking|show\s+(?:the\s+)?active\s+time\s+entry)$",
    re.I,
)
_TIME_REPORT = re.compile(
    r"^(?:how\s+much\s+time\s+did\s+i\s+spend"
    r"|show\s+(?:my\s+)?time\s+totals?|time\s+totals?"
    r"|today'?s\s+time|time\s+today)"
    r"(?:\s+(?:on|for)\s+(?P<day>.+))?"
    r"(?:\s+(?P<day2>today|yesterday))?$",
    re.I,
)
_TIME_WEEK = re.compile(
    r"^(?:show\s+)?(?:my\s+)?(?:time\s+week|weekly\s+time\s+totals)"
    r"(?:\s+(?:starting|from)\s+(?P<start>.+))?$",
    re.I,
)
_TIME_LIST = re.compile(
    r"^(?:list|show)\s+(?:my\s+)?time\s+entries|^time\s+entries$", re.I
)
_TIME_DELETE = re.compile(
    r"^(?:delete|remove)\s+time\s+entry\s+#?(?P<id>\d+)$", re.I
)
_CALENDAR_ADD = re.compile(
    r"^(?:please\s+)?(?:add|create|schedule)\s+(?:a\s+|an\s+)?"
    r"(?:calendar\s+)?event\s+(?:for\s+)?(?P<body>.+)$",
    re.I,
)
_CALENDAR_AGENDA = re.compile(
    r"^(?:what(?:'s| is)\s+on\s+)?(?:my\s+)?(?:calendar|agenda)"
    r"(?:\s+(?:for|on)\s+(?P<day>.+))?"
    r"(?:\s+(?P<day2>today|tomorrow|yesterday))?$",
    re.I,
)
_CALENDAR_SHOW = re.compile(
    r"^(?:show|describe|details?|open)\s+(?:calendar\s+)?event\s+"
    r"#?(?P<id>\d+)$",
    re.I,
)
_CALENDAR_DELETE = re.compile(
    r"^(?:delete|remove)\s+(?:calendar\s+)?event\s+#?(?P<id>\d+)$",
    re.I,
)
_EMAIL_DRAFT = re.compile(
    r"^(?:please\s+)?(?:draft|write|compose|create|make|prepare)\s+"
    r"(?:me\s+)?(?:an?\s+)?(?:email|mail)\s+(?:to\s+)?(?P<body>.+)$",
    re.I,
)
_EMAIL_LIST = re.compile(
    r"^(?:list|show)\s+(?:my\s+)?(?:email\s+|mail\s+)?drafts$", re.I
)
_EMAIL_SHOW = re.compile(
    r"^(?:show|open|display)\s+(?:email\s+|mail\s+)?draft\s+#?(?P<id>\d+)$",
    re.I,
)
_EMAIL_SEND = re.compile(
    r"^send\s+(?:email\s+|mail\s+)?draft\s+#?(?P<id>\d+)$", re.I
)
_EMAIL_DELETE = re.compile(
    r"^(?:delete|remove)\s+(?:email\s+|mail\s+)?draft\s+#?(?P<id>\d+)$",
    re.I,
)
_EMAIL_SEARCH = re.compile(
    r"^(?:search|find)\s+(?:my\s+)?(?:email|mail|inbox)\s+"
    r"(?:for\s+)?(?P<query>.+)$",
    re.I,
)
_EMAIL_READ = re.compile(
    r"^(?:read|open|show)\s+(?:email|mail|message)\s+"
    r"(?:uid\s+)?#?(?P<uid>\d+)$",
    re.I,
)
_EMAIL_SUMMARIZE = re.compile(
    r"^summari[sz]e\s+(?:email|mail|message)\s+"
    r"(?:uid\s+)?#?(?P<uid>\d+)$",
    re.I,
)
_EMAIL_REPLY = re.compile(
    r"^(?:reply|respond)\s+(?:to\s+)?(?:email|mail|message)\s+"
    r"(?:uid\s+)?#?(?P<uid>\d+)$",
    re.I,
)
_POMODORO_START = re.compile(
    r"^(?:start|begin)\s+(?:a\s+)?(?:(?P<minutes>\d+)\s+minute\s+)?"
    r"pomodoro(?:\s+for\s+(?P<minutes2>\d+)\s+minutes?)?$",
    re.I,
)
_POMODORO_STATUS = re.compile(
    r"^(?:pomodoro\s+status|(?:what(?:'s| is)\s+)?(?:my\s+)?pomodoro"
    r"(?:\s+status)?|how(?:'s| is)\s+my\s+pomodoro)$",
    re.I,
)
_POMODORO_STOP = re.compile(
    r"^(?:stop|end|cancel)\s+(?:the\s+)?pomodoro(?:\s+session)?$",
    re.I,
)
_POMODORO_SKIP = re.compile(
    r"^(?:skip|next)\s+(?:the\s+)?pomodoro(?:\s+phase)?$",
    re.I,
)
_CLIPBOARD_LIST = re.compile(
    r"^(?:show|list|view)\s+(?:my\s+)?clipboard\s+history$", re.I
)
_CLIPBOARD_SEARCH = re.compile(
    r"^search\s+(?:my\s+)?clipboard(?:\s+history)?\s+for\s+(?P<query>.+)$",
    re.I,
)
_CLIPBOARD_SHOW = re.compile(
    r"^(?:show|open|view)\s+clipboard\s+(?:entry|item)\s+#?(?P<id>\d+)$",
    re.I,
)
_CLIPBOARD_DELETE = re.compile(
    r"^(?:delete|remove)\s+clipboard\s+(?:entry|item)\s+#?(?P<id>\d+)$",
    re.I,
)
_CLIPBOARD_CLEAR = re.compile(
    r"^(?:clear|delete)\s+(?:my\s+)?clipboard\s+history$", re.I
)
_SUMMARY_DAILY = re.compile(
    r"^(?:daily\s+summary|summary\s+(?:for\s+)?(?:today|my\s+day)"
    r"|summari[sz]e\s+(?:my\s+)?day|what\s+did\s+i\s+do(?:\s+today)?)$",
    re.I,
)
_SUMMARY_MEETING = re.compile(
    r"^(?:summari[sz]e|summary\s+of)\s+(?:a\s+)?meeting\s+"
    r"(?P<body>.+)$",
    re.I,
)
_DEVELOPER_EXPLAIN = re.compile(
    r"^(?:explain|describe|summari[sz]e)\s+(?:the\s+)?"
    r"(?:source(?:\s+code)?|code|file)\s+(?P<path>.+)$",
    re.I,
)
_DEVELOPER_INSPECT = re.compile(
    r"^(?:inspect|survey|show|list)\s+(?:the\s+)?"
    r"(?:project|repository|repo)(?:\s+(?:directory|structure|tree))?"
    r"(?:\s+(?P<path>.+))?$",
    re.I,
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


def recognize_todo(text):
  """Recognize explicit productivity to-dos, never task-plan controls."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _TODO_ADD.match(source)
  if match is None:
    match = _TODO_ADD_TO.match(source)
  if match is None:
    match = _TODO_PREFIX.match(source)
  if match is not None:
    parsed = parse_todo_input(match.group("body"))
    if not parsed.get("title"):
      return None
    entities = {"title": parsed["title"]}
    if parsed.get("priority"):
      entities["priority"] = parsed["priority"]
    if parsed.get("due_at"):
      entities["due_at"] = parsed["due_at"].isoformat(timespec="minutes")
    if parsed.get("remind_at"):
      entities["remind_at"] = parsed["remind_at"].isoformat(
          timespec="minutes"
      )
    if parsed.get("recurrence") != "none":
      entities["recurrence"] = parsed["recurrence"]
    return Intent("todo_add", 0.92, entities)

  if _TODO_LIST.match(source):
    return Intent("todo_list", 0.9, {})
  for pattern, name in (
      (_TODO_DONE, "todo_done"),
      (_TODO_DONE_ALT, "todo_done"),
      (_TODO_SHOW, "todo_show"),
      (_TODO_DELETE, "todo_delete"),
  ):
    match = pattern.match(source)
    if match:
      return Intent(name, 0.9, {"id": int(match.group("id"))})
  match = _TODO_SNOOZE.match(source)
  if match:
    seconds = parse_duration(match.group("duration") or "") or 600
    return Intent(
        "todo_snooze",
        0.9,
        {"id": int(match.group("id")), "minutes": max(1, seconds // 60)},
    )
  return None


def recognize_plan(text):
  """Recognize a request for a daily or weekly to-do agenda."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _PLAN_WEEK.match(source)
  if match:
    when = match.groupdict().get("when")
    return Intent(
        "plan_week",
        0.9,
        {"when": f"{when} week" if when else ""},
    )
  match = _PLAN_DAY.match(source)
  if match:
    when = match.group("when_for") or match.group("when_bare") or ""
    return Intent("plan_day", 0.9, {"when": _clean(when)})
  return None


def recognize_notes(text):
  """Recognize quick notes, listing, showing, search and deletion."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _NOTE_ADD.match(source) or _NOTE_ALT.match(source)
  if match:
    body = (
        match.groupdict().get("body")
        or match.groupdict().get("jot")
        or match.groupdict().get("quick")
        or match.groupdict().get("plain")
        or ""
    )
    body = re.sub(r"^to\s+", "", _clean(body), flags=re.I)
    if len(body) >= 2:
      return Intent("note_add", 0.92, {"content": body})
  if _NOTE_LIST.match(source):
    return Intent("note_list", 0.9, {})
  for pattern, name in (
      (_NOTE_SHOW, "note_show"),
      (_NOTE_DELETE, "note_delete"),
  ):
    match = pattern.match(source)
    if match:
      return Intent(name, 0.9, {"id": int(match.group("id"))})
  match = _NOTE_SEARCH.match(source)
  if match:
    query = _clean(
        match.groupdict().get("query")
        or match.groupdict().get("about")
        or match.groupdict().get("say")
        or ""
    )
    if len(query) >= 2:
      return Intent("note_search", 0.9, {"query": query})
  return None


def recognize_habits(text):
  """Recognize daily habit creation, check-ins, streaks and edits."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _HABIT_ADD.match(source) or _HABIT_ADD_ALT.match(source)
  if match:
    parsed = parse_habit_input(match.group("body"))
    if len(parsed.get("title", "")) >= 2:
      entities = {"title": parsed["title"]}
      if parsed.get("reminder_time"):
        entities["reminder_time"] = parsed["reminder_time"]
      return Intent("habit_add", 0.92, entities)
  if _HABIT_LIST.match(source):
    return Intent("habit_list", 0.9, {})
  if _HABIT_STATS.match(source):
    return Intent("habit_stats", 0.9, {})
  for pattern, name in (
      (_HABIT_SHOW, "habit_show"),
      (_HABIT_UNDO, "habit_undo"),
      (_HABIT_DELETE, "habit_delete"),
  ):
    match = pattern.match(source)
    if match:
      return Intent(name, 0.9, {"id": int(match.group("id"))})
  match = _HABIT_DONE.match(source)
  if match:
    identifier = (
        match.groupdict().get("id")
        or match.groupdict().get("id2")
        or match.groupdict().get("id3")
    )
    if identifier:
      return Intent("habit_done", 0.9, {"id": int(identifier)})
  return None


def recognize_goals(text):
  """Recognize goal creation, progress, completion and deletion."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _GOAL_ADD.match(source) or _GOAL_ADD_ALT.match(source)
  if match:
    parsed = parse_goal_input(match.group("body"))
    if len(parsed.get("title", "")) >= 2:
      entities = {"title": parsed["title"]}
      if parsed.get("target_value") is not None:
        entities["target_value"] = parsed["target_value"]
      if parsed.get("unit"):
        entities["unit"] = parsed["unit"]
      if parsed.get("target_date"):
        entities["target_date"] = parsed["target_date"]
      return Intent("goal_add", 0.92, entities)
  if _GOAL_LIST.match(source):
    return Intent("goal_list", 0.9, {})
  for pattern, name in (
      (_GOAL_SHOW, "goal_show"),
      (_GOAL_COMPLETE, "goal_complete"),
      (_GOAL_REOPEN, "goal_reopen"),
      (_GOAL_DELETE, "goal_delete"),
  ):
    match = pattern.match(source)
    if match:
      identifier = match.groupdict().get("id") or match.groupdict().get("id2")
      if identifier:
        return Intent(name, 0.9, {"id": int(identifier)})
  match = _GOAL_PROGRESS.match(source)
  if match:
    identifier = (
        match.groupdict().get("id")
        or match.groupdict().get("id2")
        or match.groupdict().get("id3")
        or match.groupdict().get("id4")
    )
    value = (
        match.groupdict().get("value")
        or match.groupdict().get("value2")
        or match.groupdict().get("value3")
        or match.groupdict().get("value4")
    )
    if identifier and value:
      return Intent(
          "goal_progress",
          0.9,
          {
              "id": int(identifier),
              "value": float(value),
              "add": bool(
                  match.groupdict().get("value3")
                  or match.groupdict().get("value4")
              ),
          },
      )
  return None


def recognize_time_tracking(text):
  """Recognize starting, stopping and reporting tracked time."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _TIME_START.match(source)
  if match:
    label = _clean(match.group("label") or match.group("clock") or "")
    if len(label) >= 2:
      return Intent("time_start", 0.92, {"label": label})
  if _TIME_STOP.match(source):
    return Intent("time_stop", 0.92, {})
  if _TIME_STATUS.match(source):
    return Intent("time_status", 0.9, {})
  match = _TIME_WEEK.match(source)
  if match:
    return Intent(
        "time_week", 0.9, {"start": _clean(match.group("start") or "")}
    )
  if _TIME_LIST.match(source):
    return Intent("time_list", 0.9, {})
  match = _TIME_DELETE.match(source)
  if match:
    return Intent("time_delete", 0.9, {"id": int(match.group("id"))})
  match = _TIME_REPORT.match(source)
  if match:
    day = _clean(match.group("day") or match.group("day2") or "")
    try:
      if day:
        _report_date(day)
    except Exception:
      day = ""
    return Intent("time_report", 0.9, {"day": day})
  return None


def recognize_calendar(text):
  """Recognize event creation, daily agendas and event controls."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _CALENDAR_ADD.match(source)
  if match:
    parsed = parse_event_input(match.group("body"))
    if len(parsed.get("title", "")) >= 2:
      entities = {
          "title": parsed["title"],
          "duration_minutes": parsed["duration_minutes"],
      }
      if parsed.get("details"):
        entities["details"] = parsed["details"]
      if parsed.get("start_at") is not None:
        entities["start_at"] = parsed["start_at"].isoformat(
            timespec="minutes"
        )
      return Intent("calendar_add", 0.92, entities)
  match = _CALENDAR_AGENDA.match(source)
  if match:
    day = _clean(match.group("day") or match.group("day2") or "")
    return Intent("calendar_agenda", 0.9, {"day": day})
  for pattern, name in (
      (_CALENDAR_SHOW, "calendar_show"),
      (_CALENDAR_DELETE, "calendar_delete"),
  ):
    match = pattern.match(source)
    if match:
      return Intent(name, 0.9, {"id": int(match.group("id"))})
  return None


def recognize_email(text):
  """Recognize local drafts and explicitly connected mailbox actions."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _EMAIL_DRAFT.match(source)
  if match:
    parsed = parse_email_input(match.group("body"))
    if parsed.get("recipient"):
      entities = {
          "recipient": parsed["recipient"],
          "subject": parsed["subject"],
          "body": parsed["body"],
      }
      return Intent("email_draft", 0.92, entities)
  if _EMAIL_LIST.match(source):
    return Intent("email_list", 0.9, {})
  if _EMAIL_SEARCH.match(source):
    query = _clean(_EMAIL_SEARCH.match(source).group("query"))
    if len(query) >= 2:
      return Intent("email_search", 0.9, {"query": query})
  for pattern, name, key in (
      (_EMAIL_SHOW, "email_show", "id"),
      (_EMAIL_SEND, "email_send", "id"),
      (_EMAIL_DELETE, "email_delete", "id"),
      (_EMAIL_READ, "email_read", "uid"),
      (_EMAIL_SUMMARIZE, "email_summarize", "uid"),
      (_EMAIL_REPLY, "email_reply", "uid"),
  ):
    match = pattern.match(source)
    if match:
      return Intent(name, 0.9, {key: int(match.group(key))})
  return None


def recognize_pomodoro(text):
  """Recognize work/break phase controls for the timer-backed Pomodoro."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _POMODORO_START.match(source)
  if match:
    value = match.group("minutes") or match.group("minutes2")
    return Intent(
        "pomodoro_start", 0.92, {"minutes": int(value) if value else 0}
    )
  if _POMODORO_STATUS.match(source):
    return Intent("pomodoro_status", 0.9, {})
  if _POMODORO_STOP.match(source):
    return Intent("pomodoro_stop", 0.9, {})
  if _POMODORO_SKIP.match(source):
    return Intent("pomodoro_skip", 0.9, {})
  return None


def recognize_clipboard_history(text):
  """Recognize clipboard history listing, search and cleanup."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  if _CLIPBOARD_LIST.match(source):
    return Intent("clipboard_list", 0.9, {})
  if _CLIPBOARD_CLEAR.match(source):
    return Intent("clipboard_clear", 0.9, {})
  match = _CLIPBOARD_SEARCH.match(source)
  if match:
    query = _clean(match.group("query"))
    if len(query) >= 2:
      return Intent("clipboard_search", 0.9, {"query": query})
  for pattern, name in (
      (_CLIPBOARD_SHOW, "clipboard_show"),
      (_CLIPBOARD_DELETE, "clipboard_delete"),
  ):
    match = pattern.match(source)
    if match:
      return Intent(name, 0.9, {"id": int(match.group("id"))})
  return None


def recognize_summaries(text):
  """Recognize deterministic daily and meeting summary requests."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  if _SUMMARY_DAILY.match(source):
    return Intent("summary_daily", 0.9, {})
  match = _SUMMARY_MEETING.match(source)
  if match:
    body = _clean(match.group("body"))
    if len(body) >= 2:
      return Intent("summary_meeting", 0.9, {"text": body})
  return None


def recognize_developer(text):
  """Recognize read-only source explanation and project inspection."""
  source = (text or "").strip().rstrip("?.!")
  if not source:
    return None
  match = _DEVELOPER_EXPLAIN.match(source)
  if match:
    path = _clean(match.group("path"))
    if path:
      return Intent("developer_explain", 0.9, {"path": path})
  match = _DEVELOPER_INSPECT.match(source)
  if match:
    return Intent(
        "developer_inspect", 0.9, {"path": _clean(match.group("path") or "")}
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


_TASK_ORGANIZE = re.compile(
    r"^(?:please\s+)?(?:organize|sort|tidy)\s+(?P<path>.+)$", re.I
)
_TASK_BACKGROUND = re.compile(
    r"\s+in\s+(?:the\s+)?background\b", re.I
)
_TASK_PLAN = re.compile(
    r"^(?:as a task|plan a task(?: to)?|task:)\s*,?\s*(?P<request>.+)$",
    re.I,
)
_TASK_LIST = (
    re.compile(r"^(?:list|show)\s+(?:my\s+)?tasks$"),
    re.compile(
        r"^(?:what\s+)?tasks?\s+(?:are\s+)?(?:running|pending|active)$"
    ),
    re.compile(r"^task\s+status$"),
    re.compile(r"^tasks$"),
)
_TASK_SHOW = re.compile(
    r"^(?:show|describe)\s+task\s+#?(?P<id>\d+)$"
)
_TASK_APPROVE = re.compile(
    r"^(?:approve|run|start)\s+task\s+#?(?P<id>\d+)$"
)
_TASK_RESUME = re.compile(
    r"^(?:resume|retry)\s+task\s+#?(?P<id>\d+)$"
)
_TASK_CANCEL = re.compile(
    r"^(?:cancel|stop)\s+task\s+#?(?P<id>\d+)$"
)
_TASK_HISTORY = re.compile(r"^task\s+(?:history|log)$")
_TASK_FOLDERS = {
    "downloads": "Downloads",
    "desktop": "Desktop",
    "documents": "Documents",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}


def recognize_tasks(text):
  """Recognize organization plans and their user-facing controls."""
  source = (text or "").strip().rstrip("?.!")
  lowered = source.lower()
  if not lowered:
    return None
  match = _TASK_ORGANIZE.match(source)
  if match:
    phrase = match.group("path").strip()
    background = bool(_TASK_BACKGROUND.search(phrase))
    phrase = _TASK_BACKGROUND.sub("", phrase).strip()
    had_folder_word = bool(
        re.search(r"\s+(?:folder|directory)$", phrase)
    )
    phrase = re.sub(r"\s+(?:folder|directory)$", "", phrase).strip()
    phrase = re.sub(r"^(?:my|the)\s+", "", phrase).strip()
    phrase = phrase.strip(' "\'')
    known = phrase.lower() in _TASK_FOLDERS
    path_like = "/" in phrase or "\\" in phrase
    if not phrase or not (had_folder_word or known or path_like):
      return None
    if known and "/" not in phrase and "\\" not in phrase:
      phrase = _TASK_FOLDERS[phrase.lower()]
    return Intent(
        "task_organize",
        0.9,
        {"path": phrase, "background": background},
    )
  match = _TASK_PLAN.match(source)
  if match:
    request = match.group("request").strip()
    background = bool(_TASK_BACKGROUND.search(request))
    request = _TASK_BACKGROUND.sub("", request).strip()
    if request:
      return Intent(
          "task_plan",
          0.9,
          {"request": request, "background": background},
      )
  for pattern in _TASK_LIST:
    if pattern.match(lowered):
      return Intent("task_list", 0.9)
  for pattern, name in (
      (_TASK_SHOW, "task_show"),
      (_TASK_APPROVE, "task_approve"),
      (_TASK_RESUME, "task_resume"),
      (_TASK_CANCEL, "task_cancel"),
  ):
    match = pattern.match(lowered)
    if match:
      return Intent(name, 0.9, {"id": int(match.group("id"))})
  if _TASK_HISTORY.match(lowered):
    return Intent("task_history", 0.9)
  return None


_DOCUMENT_FORMATS = r"pdf|docx|txt|md|markdown|csv|xlsx|xlsm"
_DOCUMENT_INDEX = re.compile(
    r"^(?:index|scan|add)\s+(?:(?:my|the)\s+)?"
    r"(?P<collection>documents?|document collection|files?)"
    r"(?:\s+(?:folder|directory))?"
    r"(?:\s+(?:in|from|under)\s+(?P<path>.+))?$",
    re.I,
)
_DOCUMENT_INDEX_FILE = re.compile(
    rf"^(?:index|scan|add)\s+(?P<path>.+\.(?:{_DOCUMENT_FORMATS}))$", re.I
)
_DOCUMENT_READ = re.compile(
    r"^(?:read|open|show|display)\s+(?:me\s+)?"
    r"(?:(?:the|this|my)\s+)?"
    r"(?:document|pdf|docx|spreadsheet|worksheet|markdown file|text file)"
    r"\s+(?P<path>.+)$",
    re.I,
)
_DOCUMENT_READ_FILE = re.compile(
    rf"^(?:read|extract text from)\s+(?:(?:the|this|my)\s+)?"
    rf"(?!file\b)(?P<path>.+\.(?:{_DOCUMENT_FORMATS}))$",
    re.I,
)
_DOCUMENT_READ_THIS = re.compile(
    r"^(?:read|open|show|display)\s+(?:me\s+)?(?:this|the)\s+document$",
    re.I,
)
_DOCUMENT_SUMMARIZE = re.compile(
    r"^(?:summarize|summarise|summary of|give me a summary of)\s+"
    r"(?P<body>.+)$",
    re.I,
)
_DOCUMENT_SEARCH = re.compile(
    r"^(?:search|find)\s+(?:(?:my|the)\s+)?(?:indexed\s+)?"
    r"documents?(?:\s+collection)?(?:\s+(?:for|about))?\s*[:\-]?\s*"
    r"(?P<query>.+)$",
    re.I,
)
_DOCUMENT_ASK = re.compile(
    r"^(?:ask|query)\s+(?:(?:my|the)\s+)?documents?"
    r"(?:\s+(?:about|regarding))?\s*[:\-]?\s*(?P<query>.+)$",
    re.I,
)
_DOCUMENT_SAY = re.compile(
    r"^(?:what|which)\s+do\s+(?:(?:my|the)\s+)?documents?\s+say"
    r"(?:\s+(?:about|regarding))?\s*[:\-]?\s*(?P<query>.+)$",
    re.I,
)
_DOCUMENT_DATES = re.compile(
    r"^(?:extract|find|list|show|get)\s+(?:the\s+)?"
    r"(?:important\s+|key\s+)?dates?\s+(?:from|in)\s+(?P<path>.+)$",
    re.I,
)
_DOCUMENT_ACTIONS = re.compile(
    r"^(?:extract|find|list|show|get)\s+(?:the\s+)?action\s+items?"
    r"\s+(?:from|in)\s+(?P<path>.+)$",
    re.I,
)
_DOCUMENT_TABLES = re.compile(
    r"^(?:extract|show|list|get)\s+(?:the\s+)?tables?\s+from\s+"
    r"(?:(?:the|this)\s+document\s+)?(?P<path>.+)$",
    re.I,
)
_DOCUMENT_COMPARE = re.compile(
    r"^compare\s+(?:(?:the\s+)?documents?\s+)?"
    r"(?P<first>.+?)\s+(?:and|with|to)\s+(?P<second>.+)$",
    re.I,
)
_DOCUMENT_EXPLAIN = re.compile(
    r"^explain\s+(?:(?:this|the)\s+)?(?:technical\s+)?"
    r"(?:document|pdf|report|file)\s+(?P<path>.+)$",
    re.I,
)
_DOCUMENT_EXPLAIN_FILE = re.compile(
    rf"^explain\s+(?P<path>.+\.(?:{_DOCUMENT_FORMATS}))$", re.I
)
_DOCUMENT_EXPLAIN_THIS = re.compile(
    r"^explain\s+(?:this|the)\s+(?:technical\s+)?(?:document|pdf|report)$",
    re.I,
)
_DOCUMENT_CONVERT = re.compile(
    rf"^convert\s+(?:(?:the\s+)?document\s+)?(?P<source>.+?)\s+"
    rf"(?:to|into)\s+(?P<format>{_DOCUMENT_FORMATS})"
    r"(?:\s+as\s+(?P<target>.+))?$",
    re.I,
)
_DOCUMENT_GENERATE = re.compile(
    rf"^(?:generate|create|write)\s+(?:a\s+|the\s+)?"
    rf"(?P<format>{_DOCUMENT_FORMATS})\s+"
    r"(?:document|file|report)"
    r"(?:\s+(?:named|called)\s+(?P<path>[^\s]+))?"
    r"(?:\s+(?:about|with|saying|containing)\s+(?P<prompt>.+))?$",
    re.I,
)
_DOCUMENT_GENERATE_PATH = re.compile(
    rf"^(?:generate|create|write)\s+(?:a\s+|the\s+)?"
    rf"(?:document|file|report)\s+(?:named|called)\s+"
    rf"(?P<path>[^\s]+\.(?:{_DOCUMENT_FORMATS}))"
    r"(?:\s+(?:about|with|saying|containing)\s+(?P<prompt>.+))?$",
    re.I,
)
_DOCUMENT_REPORT = re.compile(
    r"^(?:create|generate|make|write)\s+(?:a\s+)?(?:document\s+)?report"
    r"\s+(?:on|from|about)\s+(?P<paths>.+)$",
    re.I,
)


def _document_path_like(value):
  text = str(value or "").strip()
  suffix = text.rsplit(".", 1)[-1].lower() if "." in text else ""
  return (
      suffix in _DOCUMENT_FORMATS.split("|")
      or "/" in text
      or "\\" in text
  )


def _document_path(value):
  text = _target_path(_clean(value)).strip(' "\'')
  text = re.sub(
      r"^(?:the|this|that|my)\s+(?:document|file|pdf|report|spreadsheet)"
      r"\s+",
      "",
      text,
      flags=re.I,
  ).strip()
  if text.lower() in {
      "this document",
      "the document",
      "this file",
      "the file",
      "it",
  }:
    return ""
  return text


def recognize_documents(text):
  """Recognize document reading, indexing, retrieval and extraction."""
  source = (text or "").strip().rstrip("?.!")
  lowered = source.lower()
  if not lowered:
    return None

  match = _DOCUMENT_INDEX.match(source)
  if match:
    path = _document_path(match.group("path") or "")
    if not path:
      collection = match.group("collection").lower()
      path = "Documents" if collection.startswith("document") else "."
    return Intent("document_index", 0.9, {"path": path})
  match = _DOCUMENT_INDEX_FILE.match(source)
  if match:
    return Intent(
        "document_index", 0.9, {"path": _document_path(match.group("path"))}
    )

  if _DOCUMENT_READ_THIS.match(source):
    return Intent("document_read", 0.9, {"path": ""})
  match = _DOCUMENT_READ.match(source) or _DOCUMENT_READ_FILE.match(source)
  if match:
    path = _document_path(match.group("path"))
    if path and _document_path_like(path):
      return Intent("document_read", 0.9, {"path": path})

  match = _DOCUMENT_SUMMARIZE.match(source)
  if match:
    body = _clean(match.group("body"))
    path = _document_path(body)
    if not path:
      if body.lower() in {
          "this document",
          "the document",
          "this file",
          "the file",
          "it",
      }:
        return Intent("document_summarize", 0.9, {"path": ""})
      return None
    if _document_path_like(path):
      return Intent("document_summarize", 0.9, {"path": path})
    return None

  match = _DOCUMENT_SEARCH.match(source)
  if match:
    query = _clean(match.group("query"))
    if len(query) >= 2:
      return Intent("document_search", 0.9, {"query": query})
  match = _DOCUMENT_ASK.match(source) or _DOCUMENT_SAY.match(source)
  if match:
    query = _clean(match.group("query"))
    if len(query) >= 2:
      return Intent("document_ask", 0.9, {"query": query})

  for pattern, name in (
      (_DOCUMENT_DATES, "document_dates"),
      (_DOCUMENT_ACTIONS, "document_actions"),
      (_DOCUMENT_TABLES, "document_tables"),
  ):
    match = pattern.match(source)
    if match:
      path = _document_path(match.group("path"))
      if path and _document_path_like(path):
        return Intent(name, 0.9, {"path": path})
      if not path:
        return Intent(name, 0.9, {"path": ""})

  match = _DOCUMENT_COMPARE.match(source)
  if match:
    first = _document_path(match.group("first"))
    second = _document_path(match.group("second"))
    if _document_path_like(first) and _document_path_like(second):
      return Intent(
          "document_compare",
          0.9,
          {"first": first, "second": second},
      )

  if _DOCUMENT_EXPLAIN_THIS.match(source):
    return Intent("document_explain", 0.9, {"path": ""})
  match = _DOCUMENT_EXPLAIN.match(source) or _DOCUMENT_EXPLAIN_FILE.match(
      source
  )
  if match:
    path = _document_path(match.group("path"))
    if path and _document_path_like(path):
      return Intent("document_explain", 0.9, {"path": path})

  match = _DOCUMENT_CONVERT.match(source)
  if match:
    source_path = _document_path(match.group("source"))
    target = _document_path(match.group("target") or "")
    if source_path and _document_path_like(source_path):
      entities = {
          "source": source_path,
          "format": match.group("format").lower(),
      }
      if target:
        entities["target"] = target
      return Intent("document_convert", 0.9, entities)

  match = _DOCUMENT_GENERATE.match(source)
  if match:
    prompt = _clean(match.group("prompt") or "")
    path = _document_path(match.group("path") or "")
    if not prompt:
      return None
    return Intent(
        "document_generate",
        0.88,
        {
            "format": match.group("format").lower(),
            "path": path,
            "prompt": prompt,
        },
    )
  match = _DOCUMENT_GENERATE_PATH.match(source)
  if match:
    prompt = _clean(match.group("prompt") or "")
    if not prompt:
      return None
    path = _document_path(match.group("path"))
    return Intent(
        "document_generate",
        0.88,
        {"path": path, "prompt": prompt},
    )

  match = _DOCUMENT_REPORT.match(source)
  if match:
    paths = [
        _document_path(item)
        for item in re.split(r"\s+and\s+|\s*\|\s*", match.group("paths"))
        if _clean(item)
    ]
    if paths and all(_document_path_like(path) for path in paths):
      return Intent("document_report", 0.88, {"paths": "|".join(paths)})
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

    intent = recognize_tasks(text)
    if intent is not None:
      return intent
    intent = recognize_plan(text)
    if intent is not None:
      return intent
    intent = recognize_notes(text)
    if intent is not None:
      return intent
    intent = recognize_habits(text)
    if intent is not None:
      return intent
    intent = recognize_goals(text)
    if intent is not None:
      return intent
    intent = recognize_time_tracking(text)
    if intent is not None:
      return intent
    intent = recognize_calendar(text)
    if intent is not None:
      return intent
    intent = recognize_email(text)
    if intent is not None:
      return intent
    intent = recognize_summaries(text)
    if intent is not None:
      return intent
    intent = recognize_developer(text)
    if intent is not None:
      return intent
    intent = recognize_pomodoro(text)
    if intent is not None:
      return intent
    intent = recognize_clipboard_history(text)
    if intent is not None:
      return intent
    intent = recognize_todo(text)
    if intent is not None:
      return intent
    intent = recognize_documents(text)
    if intent is not None:
      return intent
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
