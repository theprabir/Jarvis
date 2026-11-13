"""Logical agent roles sharing the one local language model.

Feature set 4.16 introduces specialized roles without adding another model or
background process. A role is a small configuration: a responsibility, a set
of intents and request keywords, and a system-prompt suffix. Deterministic
services still handle their existing work; the role only shapes the fallback
model turn or makes the ownership of a request explicit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SHARED_RULES = (
    " You are one logical role inside JARVIS, sharing the same local model."
    " Follow this role's boundary, use only the conversation and supplied"
    " tool results, and never invent observations, citations, memories or"
    " file contents."
)


@dataclass(frozen=True)
class AgentRole:
  """One logical role and the requests it owns."""

  name: str
  label: str
  description: str
  system_prompt: str
  intents: frozenset = field(default_factory=frozenset)
  keywords: tuple = ()

  def detail(self):
    return (
        f"{self.label}: {self.description}"
        f" Use: use the {self.name} agent to ..."
    )


ROLES = {
    "main": AgentRole(
        name="main",
        label="Main agent",
        description="Coordinates the assistant and chooses a specialist.",
        system_prompt=(
            "Role: Main agent." + _SHARED_RULES
            + " Coordinate the request, delegate when a specialist is"
            " clearly responsible, and otherwise answer directly."
        ),
        keywords=(),
    ),
    "research": AgentRole(
        name="research",
        label="Research agent",
        description="Web research, source comparison and evidence-based answers.",
        system_prompt=(
            "Role: Research agent." + _SHARED_RULES
            + " Focus on web research. Base claims on retrieved evidence,"
            " cite sources when available, and say plainly when evidence is"
            " missing instead of guessing."
        ),
        intents=frozenset(
            {
                "web_search",
                "research",
                "research_compare",
                "research_docs",
                "research_history",
                "research_saved",
                "research_domain",
            }
        ),
        keywords=(
            "research",
            "source",
            "sources",
            "cite",
            "citation",
            "evidence",
            "web search",
            "latest news",
            "article",
            "study",
            "compare sources",
        ),
    ),
    "coding": AgentRole(
        name="coding",
        label="Coding agent",
        description="Programming, debugging and local developer tasks.",
        system_prompt=(
            "Role: Coding agent." + _SHARED_RULES
            + " Focus on programming. Explain the likely cause, propose the"
            " smallest safe change and distinguish inspection from execution."
            " Never claim code ran unless it did."
        ),
        intents=frozenset(
            {
                "developer_explain",
                "developer_inspect",
                "developer_search",
                "developer_find",
                "developer_architecture",
                "developer_tests",
                "developer_debug",
            }
        ),
        keywords=(
            "code",
            "coding",
            "python",
            "bug",
            "debug",
            "traceback",
            "exception",
            "function",
            "class",
            "refactor",
            "compile",
            "syntax",
            "api",
            "script",
            "programming",
            "stack trace",
            "test failure",
        ),
    ),
    "productivity": AgentRole(
        name="productivity",
        label="Productivity agent",
        description="Tasks, scheduling, reminders and time planning.",
        system_prompt=(
            "Role: Productivity agent." + _SHARED_RULES
            + " Focus on tasks, scheduling and priorities. Ask for missing"
            " dates, times or priorities; never invent calendar entries or"
            " completed work."
        ),
        intents=frozenset(
            {
                "todo_add",
                "todo_list",
                "todo_show",
                "todo_done",
                "todo_delete",
                "todo_snooze",
                "plan_day",
                "plan_week",
                "timer_set",
                "reminder_set",
                "timer_list",
                "timer_cancel",
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
                "pomodoro_start",
                "pomodoro_status",
                "pomodoro_stop",
                "pomodoro_skip",
                "summary_daily",
                "summary_meeting",
            }
        ),
        keywords=(
            "todo",
            "to-do",
            "task list",
            "schedule",
            "calendar",
            "remind",
            "reminder",
            "deadline",
            "priority",
            "plan my day",
            "plan my week",
            "habit",
            "goal",
            "pomodoro",
            "focus session",
            "time tracking",
        ),
    ),
    "file": AgentRole(
        name="file",
        label="File agent",
        description="Filesystem operations inside the configured sandbox.",
        system_prompt=(
            "Role: File agent." + _SHARED_RULES
            + " Focus on filesystem operations. State exact paths, stay inside"
            " the configured sandbox and ask before destructive actions."
        ),
        intents=frozenset(
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
                "open_path",
                "task_organize",
                "document_convert",
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
            }
        ),
        keywords=(
            "file",
            "files",
            "folder",
            "directory",
            "downloads",
            "documents",
            "desktop",
            "move file",
            "copy file",
            "rename",
            "delete file",
            "organize",
            "inventory",
            "path",
        ),
    ),
    "memory": AgentRole(
        name="memory",
        label="Memory agent",
        description="Stored memories, preferences and recall.",
        system_prompt=(
            "Role: Memory agent." + _SHARED_RULES
            + " Focus on saved memories and preferences. Use only supplied"
            " memory context, distinguish stored facts from inference and"
            " never fabricate a memory."
        ),
        intents=frozenset(
            {
                "remember_memory",
                "search_memory",
                "list_memory",
                "delete_memory",
                "learned_memory",
                "memory_episodes",
            }
        ),
        keywords=(
            "remember",
            "memory",
            "memories",
            "recall",
            "preference",
            "preferences",
            "what do you know about me",
            "saved fact",
        ),
    ),
    "verification": AgentRole(
        name="verification",
        label="Verification agent",
        description="Result validation and explicit uncertainty reporting.",
        system_prompt=(
            "Role: Verification agent." + _SHARED_RULES
            + " Focus on validating results. Check the claimed outcome against"
            " available evidence, report passed, failed or unverified, and"
            " name any missing checks."
        ),
        keywords=(
            "verify",
            "validate",
            "validation",
            "did it work",
            "check the result",
            "confirm the result",
            "test the result",
            "result check",
            "proof",
        ),
    ),
}

ROLE_ORDER = (
    "verification",
    "research",
    "coding",
    "file",
    "productivity",
    "memory",
    "main",
)

INTENT_ROLES = {}
for _role in ROLES.values():
  for _intent in _role.intents:
    INTENT_ROLES[_intent] = _role


@dataclass(frozen=True)
class RoleSelection:
  """The role chosen for one request and the request text to use."""

  role: AgentRole
  request: str
  explicit: bool = False


_ROLE_NAMES = "|".join(
    re.escape(name) for name in sorted(ROLES, key=len, reverse=True)
)
_EXPLICIT_PATTERNS = (
    re.compile(
        r"^(?:please\s+)?(?:use|ask|consult|delegate\s+to)\s+"
        r"(?:the\s+)?(?P<role>" + _ROLE_NAMES + r")\s+"
        r"(?:agent|role)(?:\s+to)?\s+(?P<request>.+)$",
        re.I,
    ),
    re.compile(
        r"^as\s+(?:the\s+|a\s+)?(?P<role>" + _ROLE_NAMES + r")\s+agent"
        r"\s*,?\s*(?P<request>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?P<role>" + _ROLE_NAMES + r")\s+agent\s*:\s*"
        r"(?P<request>.+)$",
        re.I,
    ),
)


def _matches(text, keyword):
  pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
  return re.search(pattern, text, re.I) is not None


class RoleRouter:
  """Choose a logical role without asking the model to choose one."""

  def __init__(self, roles=None):
    self._roles = dict(roles or ROLES)

  def get(self, name):
    return self._roles.get(str(name or "").strip().lower())

  def names(self):
    return tuple(self._roles)

  def parse_explicit(self, text):
    """Recognize ``use the coding agent to ...`` and similar phrases."""
    value = " ".join(str(text or "").split()).strip()
    if not value:
      return None
    for pattern in _EXPLICIT_PATTERNS:
      match = pattern.match(value)
      if match is None:
        continue
      role = self.get(match.group("role"))
      request = match.group("request").strip()
      if role is not None and request:
        return RoleSelection(role, request, True)
    return None

  def select(self, text, intent=None):
    """Pick the explicit, intent-backed or best keyword role."""
    value = " ".join(str(text or "").split()).strip()
    explicit = self.parse_explicit(value)
    if explicit is not None:
      return explicit
    if intent is not None:
      role = INTENT_ROLES.get(intent.name)
      if role is not None:
        return RoleSelection(role, value)
    scores = {}
    for name in ROLE_ORDER:
      role = self._roles.get(name)
      if role is None or role.name == "main":
        continue
      scores[name] = sum(
          1 for keyword in role.keywords if _matches(value, keyword)
      )
    best = max(scores, key=lambda name: scores[name], default="main")
    if scores.get(best, 0):
      return RoleSelection(self._roles[best], value)
    return RoleSelection(self._roles["main"], value)

  def help_text(self):
    lines = ["Agent roles: use the NAME agent to REQUEST, or /role NAME."]
    for name in ("main", "research", "coding", "productivity", "file",
                 "memory", "verification"):
      role = self._roles.get(name)
      if role is not None:
        lines.append(f"{role.name}: {role.label} - {role.description}")
    return "\n".join(lines)
