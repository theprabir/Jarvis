"""Validated goal tools backed by the shared goal service."""

from __future__ import annotations

from core.goals import GoalError, format_goal, format_goal_detail
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Goal tracking is disabled.")
  return service


def _fail(exc):
  raise ToolError(str(exc)) from exc


def _data(goal):
  return {
      "id": goal.id,
      "title": goal.title,
      "details": goal.details,
      "target_value": goal.target_value,
      "current_value": goal.current_value,
      "unit": goal.unit,
      "progress_percent": goal.progress_percent,
      "target_date": goal.target_date.isoformat() if goal.target_date else "",
      "status": goal.status,
      "completed_at": goal.completed_at,
  }


def _number(value):
  if value in (None, ""):
    return None
  try:
    number = float(value)
  except (TypeError, ValueError):
    raise ToolError("target must be a number.") from None
  return int(number) if number.is_integer() else number


def add_goal(
    provider,
    title,
    target="",
    unit="",
    due="",
    details="",
):
  service = _service(provider)
  try:
    goal = service.add(
        title,
        details=details,
        target_value=_number(target),
        unit=unit,
        target_date=due,
    )
  except GoalError as exc:
    _fail(exc)
  text = f"Goal #{goal.id} added: {goal.title}."
  if goal.target_value is not None:
    text += (
        f" Target: {goal.target_value:g} {goal.unit}".rstrip() + "."
    )
  if goal.target_date is not None:
    text += f" Target date: {goal.target_date.isoformat()}."
  return ToolResult(True, text, _data(goal))


def list_goals(provider, status="active"):
  service = _service(provider)
  wanted = " ".join(str(status or "active").split()).lower()
  if wanted in {"all", "any"}:
    wanted = None
  elif wanted not in {"active", "completed"}:
    raise ToolError("status must be active, completed or all.")
  try:
    goals = service.list(status=wanted)
  except GoalError as exc:
    _fail(exc)
  if not goals:
    text = "You have no active goals." if wanted == "active" else "No goals found."
  else:
    text = "Goals:\n" + "\n".join(format_goal(goal) for goal in goals)
  return ToolResult(
      True,
      text,
      {"count": len(goals), "goals": [_data(goal) for goal in goals]},
  )


def show_goal(provider, id):
  service = _service(provider)
  try:
    goal = service.get(id)
  except GoalError as exc:
    _fail(exc)
  if goal is None:
    raise ToolError("there is no goal with that id.")
  return ToolResult(True, format_goal_detail(goal), _data(goal))


def progress_goal(provider, id, value, add=False):
  service = _service(provider)
  try:
    goal = service.progress(id, value, add=add)
  except GoalError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Goal #{goal.id} progress updated: {format_goal(goal)}.",
      _data(goal),
  )


def complete_goal(provider, id):
  service = _service(provider)
  try:
    goal = service.complete(id)
  except GoalError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Goal #{goal.id} completed: {goal.title}.",
      _data(goal),
  )


def reopen_goal(provider, id):
  service = _service(provider)
  try:
    goal = service.reopen(id)
  except GoalError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Goal #{goal.id} reopened: {goal.title}.",
      _data(goal),
  )


def update_goal(
    provider,
    id,
    title="",
    details="",
    target="",
    unit="",
    due="",
    clear_due=False,
):
  service = _service(provider)
  fields = {}
  if title:
    fields["title"] = title
  if details:
    fields["details"] = details
  if target:
    fields["target_value"] = _number(target)
  if unit:
    fields["unit"] = unit
  if clear_due:
    fields["target_date"] = ""
  elif due:
    fields["target_date"] = due
  if not fields:
    raise ToolError("nothing to update.")
  try:
    goal = service.update(id, **fields)
  except GoalError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Goal #{goal.id} updated: {goal.title}.",
      _data(goal),
  )


def delete_goal(provider, id):
  service = _service(provider)
  try:
    goal = service.delete(id)
  except GoalError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Goal #{goal.id} removed: {goal.title}.",
      _data(goal),
  )
