"""Validated habit tools backed by the shared habit service."""

from __future__ import annotations

from core.habits import (
    HabitError,
    format_habit,
    format_habit_detail,
)
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Habit tracking is disabled.")
  return service


def _fail(exc):
  raise ToolError(str(exc)) from exc


def _data(summary):
  habit = summary.habit
  return {
      "id": habit.id,
      "title": habit.title,
      "details": habit.details,
      "reminder_time": habit.reminder_time,
      "streak": summary.streak,
      "best_streak": summary.best_streak,
      "done_today": summary.done_today,
      "total_checkins": summary.total_checkins,
  }


def add_habit(provider, title, details="", remind=""):
  service = _service(provider)
  try:
    summary = service.add(title, details=details, reminder_time=remind)
  except HabitError as exc:
    _fail(exc)
  text = f"Habit #{summary.id} added: {summary.title}."
  if summary.habit.reminder_time:
    text += f" I'll remind you daily at {summary.habit.reminder_time}."
  return ToolResult(True, text, _data(summary))


def list_habits(provider):
  service = _service(provider)
  try:
    summaries = service.list()
  except HabitError as exc:
    _fail(exc)
  if not summaries:
    return ToolResult(True, "You have no habits.", {"count": 0, "habits": []})
  text = "Habits:\n" + "\n".join(
      format_habit(summary) for summary in summaries
  )
  return ToolResult(
      True,
      text,
      {
          "count": len(summaries),
          "done_today": sum(1 for item in summaries if item.done_today),
          "habits": [_data(item) for item in summaries],
      },
  )


def show_habit(provider, id):
  service = _service(provider)
  try:
    summary = service.summary(id)
  except HabitError as exc:
    _fail(exc)
  return ToolResult(True, format_habit_detail(summary), _data(summary))


def complete_habit(provider, id):
  service = _service(provider)
  try:
    summary = service.check_in(id)
  except HabitError as exc:
    _fail(exc)
  text = f"Habit #{summary.id} checked in: {summary.title}."
  text += f" {summary.streak} day streak."
  return ToolResult(True, text, _data(summary))


def undo_habit(provider, id):
  service = _service(provider)
  try:
    summary = service.undo(id)
  except HabitError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Habit #{summary.id} check-in undone: {summary.title}."
      f" Current streak: {summary.streak} days.",
      _data(summary),
  )


def update_habit(provider, id, title="", details="", remind=""):
  service = _service(provider)
  fields = {}
  if title:
    fields["title"] = title
  if details:
    fields["details"] = details
  if remind:
    fields["reminder_time"] = remind
  if not fields:
    raise ToolError("nothing to update.")
  try:
    summary = service.update(id, **fields)
  except HabitError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Habit #{summary.id} updated: {summary.title}.",
      _data(summary),
  )


def delete_habit(provider, id):
  service = _service(provider)
  try:
    habit = service.delete(id)
  except HabitError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Habit #{habit.id} removed: {habit.title}.",
      {
          "id": habit.id,
          "title": habit.title,
          "reminder_time": habit.reminder_time,
      },
  )
