"""Constrained autonomous goal planning for durable tasks.

The task engine already executes a plan; this module interprets a high-level
goal into one. Planning is deterministic and limited to registered tools, so a
small local model never gets to invent commands or bypass the tool sandbox.
Compound goals joined by "then" are chained through dependencies.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from core.intents import plan_file_actions, recognize_tasks
from core.tasks import KNOWN_FOLDERS, TaskError, TaskStep, plan_organize


@dataclass
class AgentPlan:
  """A planned goal, its steps and the summary shown after completion."""

  goal: str
  kind: str
  steps: list
  context: dict = field(default_factory=dict)
  summary: str = ""


SYSTEM_GOAL = re.compile(
    r"^(?:run\s+)?(?:a\s+)?(?:system|computer|pc)\s+"
    r"(?:check|health|status|report)$"
    r"|^(?:check|inspect)\s+(?:my\s+)?(?:system|computer|pc)"
    r"(?:\s+(?:health|status))?$",
    re.I,
)
INVENTORY_GOALS = (
    re.compile(
        r"^(?:take\s+)?(?:an?\s+)?inventory(?:\s+of)?\s+(?P<path>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?:audit|inspect)\s+(?P<path>.+?)"
        r"(?:\s+(?:folder|directory))?$",
        re.I,
    ),
    re.compile(
        r"^list\s+(?:the\s+)?(?:contents\s+of\s+)?(?P<path>.+)$",
        re.I,
    ),
)
SEARCH_GOAL = re.compile(
    r"^(?:find|locate|search\s+for)\s+(?P<name>.+?)"
    r"\s+(?:in|under|inside)\s+(?P<path>.+)$",
    re.I,
)
FOLDER_GOAL = re.compile(
    r"^(?:create|make)\s+(?:a\s+)?(?:new\s+)?"
    r"(?:folder|directory)\s+(?:named\s+|called\s+)?(?P<path>.+)$",
    re.I,
)
SPLIT_GOALS = re.compile(r"\s+then\s+", re.I)


def _clean_path(value):
  text = " ".join(str(value or "").split()).strip().strip('"').strip("'")
  text = re.sub(r"\s+(?:folder|directory)$", "", text, flags=re.I).strip()
  text = re.sub(r"^(?:my|the)\s+", "", text, flags=re.I).strip()
  known = text.lower()
  if known in KNOWN_FOLDERS and "/" not in text and "\\" not in text:
    return KNOWN_FOLDERS[known]
  return text or "."


def _clean_name(value):
  return " ".join(str(value or "").split()).strip().strip('"').strip("'")


class GoalAgent:
  """Turn a supported high-level goal into a validated task plan."""

  def __init__(
      self,
      registry,
      allowed_apps=(),
      allowed_commands=(),
      max_steps=5000,
  ):
    if registry is None:
      raise TaskError("Computer tools are disabled")
    self._registry = registry
    self._allowed_apps = tuple(allowed_apps or ())
    self._allowed_commands = tuple(allowed_commands or ())
    self._max_steps = max(3, int(max_steps))

  def plan(self, goal):
    """Return an :class:`AgentPlan`, or ``None`` when the goal is unsupported."""
    text = " ".join(str(goal or "").split()).strip()
    if not text:
      return None

    parts = [part.strip() for part in SPLIT_GOALS.split(text)]
    if len(parts) > 1 and all(parts):
      plan = self._plan_compound(text, parts)
      if plan is not None:
        return self._limit(plan)

    for planner in (
        self._plan_organize,
        self._plan_file_actions,
        self._plan_system,
        self._plan_inventory,
        self._plan_search,
        self._plan_folder,
    ):
      plan = planner(text)
      if plan is not None:
        return self._limit(plan)
    return None

  def _plan_organize(self, text):
    recognized = recognize_tasks(text)
    if recognized is None or recognized.name != "task_organize":
      return None
    path = recognized.entities.get("path") or "Downloads"
    steps, context = plan_organize(
        self._registry, path, self._max_steps
    )
    label = context.get("label") or path
    files = int(context.get("files") or 0)
    categories = len(context.get("categories") or {})
    if files:
      summary = (
          f"organize {files} files into {categories} folders in {label}"
      )
    else:
      summary = f"inspect {label}"
    return AgentPlan(
        goal=f"Organize {label}",
        kind="organize",
        steps=steps,
        context={**context, "agent": True, "summary": summary},
        summary=summary,
    )

  def _plan_file_actions(self, text):
    actions = plan_file_actions(
        text, self._allowed_apps, self._allowed_commands
    )
    if not actions:
      return None
    steps = []
    previous = None
    for index, (name, arguments, clause) in enumerate(actions, 1):
      if self._registry.get(name) is None:
        raise TaskError(f"there is no tool named {name}")
      key = f"agent_file_{index:02d}"
      steps.append(
          TaskStep(
              key=key,
              name=(clause or name).strip(),
              tool=name,
              arguments=dict(arguments),
              depends_on=(previous,) if previous else (),
              retries=1,
          )
      )
      previous = key
    summary = f"run {len(steps)} file steps"
    return AgentPlan(
        goal=text,
        kind="file_plan",
        steps=steps,
        context={
            "actions": len(steps),
            "agent": True,
            "summary": summary,
        },
        summary=summary,
    )

  def _plan_system(self, text):
    if not SYSTEM_GOAL.match(text):
      return None
    steps = [
        TaskStep(
            key="system_info",
            name="Check system health",
            tool="system_info",
            arguments={"kind": "all"},
            retries=1,
        ),
        TaskStep(
            key="top_processes",
            name="List the top processes",
            tool="processes",
            arguments={"limit": 5},
            retries=1,
            parallel=True,
        ),
    ]
    summary = "check system health and the top processes"
    return AgentPlan(
        goal="Check system health",
        kind="system_check",
        steps=steps,
        context={"agent": True, "summary": summary},
        summary=summary,
    )

  def _plan_inventory(self, text):
    match = None
    for pattern in INVENTORY_GOALS:
      match = pattern.match(text)
      if match:
        break
    if not match:
      return None
    path = _clean_path(match.group("path"))
    summary = f"take inventory of {path}"
    steps = [
        TaskStep(
            key="inventory",
            name=f"Inventory {path}",
            tool="list_files",
            arguments={"path": path, "limit": self._max_steps},
            retries=1,
        )
    ]
    return AgentPlan(
        goal=f"Inventory {path}",
        kind="inventory",
        steps=steps,
        context={"path": path, "agent": True, "summary": summary},
        summary=summary,
    )

  def _plan_search(self, text):
    match = SEARCH_GOAL.match(text)
    if not match:
      return None
    name = _clean_name(match.group("name"))
    path = _clean_path(match.group("path"))
    if not name:
      return None
    summary = f"find {name} under {path}"
    steps = [
        TaskStep(
            key="search",
            name=f"Find {name}",
            tool="search_files",
            arguments={"name": name, "path": path},
            retries=1,
        )
    ]
    return AgentPlan(
        goal=f"Find {name}",
        kind="search",
        steps=steps,
        context={
            "name": name,
            "path": path,
            "agent": True,
            "summary": summary,
        },
        summary=summary,
    )

  def _plan_folder(self, text):
    match = FOLDER_GOAL.match(text)
    if not match:
      return None
    path = _clean_path(match.group("path"))
    normalized = path.replace("\\", "/").rstrip("/")
    if not normalized:
      return None
    parent, _, name = normalized.rpartition("/")
    parent = parent or "."
    steps = [
        TaskStep(
            key="create_folder",
            name=f"Create the folder {name}",
            tool="create_folder",
            arguments={"path": path},
            retries=1,
        ),
        TaskStep(
            key="verify_folder",
            name=f"Verify the folder {name}",
            tool="list_files",
            arguments={"path": parent, "limit": self._max_steps},
            depends_on=("create_folder",),
            verify={"contains": [name + "/"]},
            retries=1,
        ),
    ]
    summary = f"create the folder {path}"
    return AgentPlan(
        goal=f"Create {path}",
        kind="folder",
        steps=steps,
        context={"path": path, "agent": True, "summary": summary},
        summary=summary,
    )

  def _plan_compound(self, text, parts):
    plans = []
    for part in parts:
      plan = self.plan(part)
      if plan is None:
        return None
      plans.append(plan)

    steps = []
    previous_terminal = set()
    for index, plan in enumerate(plans, 1):
      prefix = f"stage{index}_"
      initial = {
          step.key for step in plan.steps if not step.depends_on
      }
      terminal = {
          step.key for step in plan.steps
          if not any(
              step.key in other.depends_on for other in plan.steps
          )
      }
      for step in plan.steps:
        planned = copy.deepcopy(step)
        planned.key = prefix + step.key
        dependencies = [prefix + key for key in step.depends_on]
        if step.key in initial:
          dependencies.extend(sorted(previous_terminal))
        planned.depends_on = tuple(dict.fromkeys(dependencies))
        steps.append(planned)
      previous_terminal = {prefix + key for key in terminal}

    summary = "; then ".join(plan.summary for plan in plans)
    return AgentPlan(
        goal=text,
        kind="compound",
        steps=steps,
        context={
            "agent": True,
            "summary": summary,
            "stages": len(plans),
        },
        summary=summary,
    )

  def _limit(self, plan):
    if not plan.steps:
      raise TaskError("that goal produced no executable steps")
    if len(plan.steps) > self._max_steps:
      raise TaskError(
          f"that goal needs {len(plan.steps)} steps; the limit is"
          f" {self._max_steps}"
      )
    return plan
