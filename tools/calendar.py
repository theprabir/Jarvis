"""Validated calendar tools backed by the shared calendar service."""

from __future__ import annotations

from core.calendar import (
    CalendarError,
    format_agenda,
    format_event,
    parse_event_input,
)
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Calendar is disabled.")
  return service


def _fail(exc):
  raise ToolError(str(exc)) from exc


def _data(event):
  return {
      "id": event.id,
      "title": event.title,
      "details": event.details,
      "start_at": event.start_at.isoformat(timespec="minutes"),
      "end_at": event.end_at.isoformat(timespec="minutes"),
      "duration_minutes": event.duration_minutes,
  }


def add_event(provider, title, start, duration=60, details=""):
  service = _service(provider)
  if isinstance(start, str):
    parsed = parse_event_input(
        f"{title} at {start}" if " at " not in start.lower() else start
    )
    start_at = parsed.get("start_at")
  else:
    start_at = start
  if start_at is None:
    raise ToolError("I couldn't understand the event start time.")
  try:
    event = service.add(
        title,
        start_at,
        duration_minutes=duration,
        details=details,
    )
  except CalendarError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Event #{event.id} added: {format_event(event)}.",
      _data(event),
  )


def agenda_calendar(provider, day=""):
  service = _service(provider)
  try:
    agenda = service.agenda(day or None)
  except CalendarError as exc:
    _fail(exc)
  return ToolResult(
      True,
      format_agenda(agenda),
      {
          "day": agenda.day.isoformat(),
          "count": len(agenda.events),
          "events": [_data(event) for event in agenda.events],
      },
  )


def list_calendar(provider, limit=20):
  service = _service(provider)
  try:
    events = service.upcoming(limit=limit)
  except CalendarError as exc:
    _fail(exc)
  if not events:
    return ToolResult(True, "No upcoming events.", {"count": 0, "events": []})
  return ToolResult(
      True,
      "Upcoming events:\n" + "\n".join(format_event(event) for event in events),
      {"count": len(events), "events": [_data(event) for event in events]},
  )


def show_calendar(provider, id):
  service = _service(provider)
  try:
    event = service.get(id)
  except CalendarError as exc:
    _fail(exc)
  if event is None:
    raise ToolError("there is no event with that id.")
  text = format_event(event)
  if event.details:
    text += f"\nDetails: {event.details}"
  return ToolResult(True, text, _data(event))


def update_calendar(
    provider, id, title="", start="", duration=0, details=""
):
  service = _service(provider)
  fields = {}
  if title:
    fields["title"] = title
  if start:
    fields["start_at"] = start
  if duration:
    fields["duration_minutes"] = duration
  if details:
    fields["details"] = details
  if not fields:
    raise ToolError("nothing to update.")
  try:
    event = service.update(id, **fields)
  except CalendarError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Event #{event.id} updated: {format_event(event)}.",
      _data(event),
  )


def delete_calendar(provider, id):
  service = _service(provider)
  try:
    event = service.delete(id)
  except CalendarError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Event #{event.id} removed: {event.title}.",
      _data(event),
  )
