"""Validated productivity tools backed by the shared to-do service.

The handlers never open a second database. ``build_registry`` passes either a
``ProductivityService`` or a zero-argument provider, so an assistant plan and a
direct tool call operate on exactly the same items and reminder scheduler.
"""

from __future__ import annotations

from datetime import datetime

from core.productivity import (
    ProductivityError,
    format_plan,
    format_todo,
    parse_when,
)
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Productivity is disabled.")
  return service


def _fail(exc):
  raise ToolError(str(exc)) from exc


def _datetime(value, label, prefer_future):
  text = " ".join(str(value or "").split()).strip()
  if not text:
    return None
  if "T" in text or " " in text:
    try:
      return datetime.fromisoformat(text)
    except ValueError:
      pass
  try:
    parsed = parse_when(text, prefer_future=prefer_future)
  except ProductivityError as exc:
    _fail(exc)
  if parsed is None:
    raise ToolError(f"I couldn't understand the {label} time.")
  return parsed


def _item_data(item):
  return {
      "id": item.id,
      "title": item.title,
      "details": item.details,
      "priority": item.priority,
      "status": item.status,
      "due_at": item.due_at.isoformat(timespec="minutes") if item.due_at else "",
      "remind_at": (
          item.remind_at.isoformat(timespec="minutes")
          if item.remind_at
          else ""
      ),
      "recurrence": item.recurrence,
      "tags": item.tags,
      "parent_id": item.parent_id,
      "completed_at": item.completed_at,
  }


def _added_text(item):
  text = f"To-do #{item.id} added: {item.title}."
  if item.due_at is not None:
    text += f" Due {item.due_at.strftime('%A %d %B at %H:%M')}."
  if item.remind_at is not None and item.remind_at != item.due_at:
    text += f" Reminder at {item.remind_at.strftime('%H:%M')}."
  return text


def _plan_data(plan):
  return {
      "start": plan.start.isoformat(),
      "end": plan.end.isoformat(),
      "total_open": plan.total_open,
      "overdue": [_item_data(item) for item in plan.overdue],
      "days": [
          {
              "date": entry.day.isoformat(),
              "items": [_item_data(item) for item in entry.items],
          }
          for entry in plan.days
      ],
      "unscheduled": [_item_data(item) for item in plan.unscheduled],
      "later": [_item_data(item) for item in plan.later],
  }


def plan_day(provider, day=""):
  service = _service(provider)
  try:
    plan = service.daily_plan(day or None)
  except ProductivityError as exc:
    _fail(exc)
  return ToolResult(True, format_plan(plan), _plan_data(plan))


def plan_week(provider, start=""):
  service = _service(provider)
  try:
    plan = service.weekly_plan(start or None)
  except ProductivityError as exc:
    _fail(exc)
  return ToolResult(True, format_plan(plan), _plan_data(plan))


def add_todo(
    provider,
    title,
    details="",
    priority="",
    due="",
    remind="",
    recurrence="none",
    tags="",
):
  service = _service(provider)
  due_at = _datetime(due, "due", prefer_future=False)
  remind_at = _datetime(remind, "reminder", prefer_future=True)
  try:
    item = service.add(
        title,
        details=details,
        priority=priority or None,
        due_at=due_at,
        remind_at=remind_at,
        recurrence=recurrence or "none",
        tags=tags,
    )
  except ProductivityError as exc:
    _fail(exc)
  return ToolResult(True, _added_text(item), _item_data(item))


def list_todos(provider, status="open", limit=20):
  service = _service(provider)
  wanted = " ".join(str(status or "open").split()).lower()
  if wanted in {"all", "any", "history"}:
    wanted = None
  elif wanted not in {"open", "done", "cancelled"}:
    raise ToolError("status must be open, done, cancelled or all.")
  limit = max(1, min(int(limit), 100))
  try:
    rows = service.list(status=wanted, limit=limit)
  except ProductivityError as exc:
    _fail(exc)
  if not rows:
    text = "You have no open to-dos." if wanted == "open" else "No to-dos found."
  else:
    heading = "Open to-dos:" if wanted == "open" else "To-dos:"
    text = heading + "\n" + "\n".join(format_todo(item) for item in rows)
  return ToolResult(
      True,
      text,
      {"count": len(rows), "items": [_item_data(item) for item in rows]},
  )


def show_todo(provider, id):
  service = _service(provider)
  try:
    item = service.get(id)
  except ProductivityError as exc:
    _fail(exc)
  if item is None:
    raise ToolError("there is no to-do with that id.")
  lines = [format_todo(item), f"Details: {item.details or 'none'}"]
  if item.tags:
    lines.append(f"Tags: {item.tags}")
  return ToolResult(True, "\n".join(lines), _item_data(item))


def update_todo(
    provider,
    id,
    title="",
    details="",
    priority="",
    due="",
    remind="",
    recurrence="",
    tags="",
    clear_due=False,
    clear_remind=False,
):
  service = _service(provider)
  try:
    item = service.get(id)
  except ProductivityError as exc:
    _fail(exc)
  if item is None:
    raise ToolError("there is no to-do with that id.")
  fields = {}
  if title:
    fields["title"] = title
  if details:
    fields["details"] = details
  if priority:
    fields["priority"] = priority
  if recurrence:
    fields["recurrence"] = recurrence
  if tags:
    fields["tags"] = tags
  if clear_due:
    fields["due_at"] = None
    if due:
      raise ToolError("use either due or clear_due, not both.")
  elif due:
    fields["due_at"] = _datetime(due, "due", prefer_future=False)
  if clear_remind:
    fields["remind_at"] = None
    if remind:
      raise ToolError("use either remind or clear_remind, not both.")
  elif remind:
    fields["remind_at"] = _datetime(remind, "reminder", prefer_future=True)
  elif due and (
      item.remind_at is None or item.remind_at == item.due_at
  ):
    fields["remind_at"] = fields["due_at"]
  if not fields:
    raise ToolError("nothing to update.")
  try:
    item = service.update(id, **fields)
  except ProductivityError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"To-do #{item.id} updated: {item.title}.",
      _item_data(item),
  )


def complete_todo(provider, id):
  service = _service(provider)
  try:
    done, next_item = service.complete(id)
  except ProductivityError as exc:
    _fail(exc)
  text = f"To-do #{done.id} completed: {done.title}."
  data = {"todo": _item_data(done), "next": None}
  if next_item is not None:
    text += (
        f" Next occurrence: #{next_item.id} due"
        f" {next_item.due_at.strftime('%A %d %B at %H:%M')}."
    )
    data["next"] = _item_data(next_item)
  return ToolResult(True, text, data)


def snooze_todo(provider, id, minutes=10):
  service = _service(provider)
  try:
    item = service.snooze(id, minutes)
  except ProductivityError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"To-do #{item.id} snoozed until"
      f" {item.remind_at.strftime('%A %d %B at %H:%M')}.",
      _item_data(item),
  )


def delete_todo(provider, id):
  service = _service(provider)
  try:
    item = service.delete(id)
  except ProductivityError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"To-do #{item.id} removed: {item.title}.",
      _item_data(item),
  )
