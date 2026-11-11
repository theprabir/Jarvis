"""Validated time tracking tools backed by the shared service."""

from __future__ import annotations

from core.time_tracking import (
    TimeError,
    format_entry,
    format_report,
    format_week,
)
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Time tracking is disabled.")
  return service


def _fail(exc):
  raise ToolError(str(exc)) from exc


def _entry_data(entry, now=None):
  return {
      "id": entry.id,
      "label": entry.label,
      "started_at": entry.started_at.isoformat(timespec="seconds"),
      "stopped_at": (
          entry.stopped_at.isoformat(timespec="seconds")
          if entry.stopped_at
          else ""
      ),
      "active": entry.active,
      "seconds": entry.seconds(now),
  }


def _report_data(report):
  return {
      "day": report.day.isoformat(),
      "total_seconds": report.total_seconds,
      "totals": [
          {"label": total.label, "seconds": total.seconds}
          for total in report.totals
      ],
  }


def start_time(provider, label):
  service = _service(provider)
  try:
    entry = service.start(label)
  except TimeError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Started tracking {entry.label} at"
      f" {entry.started_at.strftime('%H:%M')}.",
      _entry_data(entry),
  )


def stop_time(provider):
  service = _service(provider)
  try:
    entry = service.stop()
  except TimeError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Stopped tracking {entry.label}. {format_entry(entry)}.",
      _entry_data(entry),
  )


def status_time(provider):
  service = _service(provider)
  try:
    entry = service.active()
  except TimeError as exc:
    _fail(exc)
  if entry is None:
    return ToolResult(True, "Nothing is being tracked.", {"active": None})
  return ToolResult(
      True,
      f"Tracking {entry.label}; {format_entry(entry)}.",
      _entry_data(entry),
  )


def report_time(provider, day=""):
  service = _service(provider)
  try:
    report = service.report(day or None)
  except TimeError as exc:
    _fail(exc)
  return ToolResult(True, format_report(report), _report_data(report))


def week_time(provider, start=""):
  service = _service(provider)
  try:
    reports = service.week(start or None)
  except TimeError as exc:
    _fail(exc)
  start_day = reports[0].day
  return ToolResult(
      True,
      format_week(reports, start_day),
      {
          "start": start_day.isoformat(),
          "total_seconds": sum(item.total_seconds for item in reports),
          "days": [_report_data(item) for item in reports],
      },
  )


def list_time(provider, limit=20):
  service = _service(provider)
  limit = max(1, min(int(limit), 100))
  try:
    entries = service.list(limit)
  except TimeError as exc:
    _fail(exc)
  if not entries:
    return ToolResult(True, "No time entries found.", {"count": 0, "entries": []})
  return ToolResult(
      True,
      "Time entries:\n"
      + "\n".join(format_entry(entry) for entry in entries),
      {"count": len(entries), "entries": [_entry_data(item) for item in entries]},
  )


def delete_time(provider, id):
  service = _service(provider)
  try:
    entry = service.delete(id)
  except TimeError as exc:
    _fail(exc)
  return ToolResult(
      True,
      f"Time entry #{entry.id} removed: {entry.label}.",
      _entry_data(entry),
  )
