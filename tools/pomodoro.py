"""Validated Pomodoro tools backed by the shared timer service."""

from __future__ import annotations

from core.pomodoro import PomodoroError
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Pomodoro is disabled.")
  return service


def _status_data(status):
  return {
      "running": status.running,
      "phase": status.phase,
      "remaining_s": status.remaining_s,
      "completed_work": status.completed_work,
      "work_minutes": status.work_minutes,
  }


def start(provider, minutes=0):
  service = _service(provider)
  try:
    status = service.start(minutes or None)
  except PomodoroError as exc:
    raise ToolError(str(exc)) from exc
  return ToolResult(
      True,
      f"Pomodoro {status.phase_label} started for {status.work_minutes} minutes.",
      _status_data(status),
  )


def status(provider):
  service = _service(provider)
  current = service.status()
  if not current.running:
    return ToolResult(True, "No Pomodoro session is running.", _status_data(current))
  minutes, seconds = divmod(current.remaining_s, 60)
  return ToolResult(
      True,
      f"Pomodoro {current.phase_label}: {minutes}m {seconds}s remaining.",
      _status_data(current),
  )


def stop(provider):
  service = _service(provider)
  current = service.status()
  if not current.running:
    return ToolResult(True, "No Pomodoro session is running.", _status_data(current))
  service.stop()
  return ToolResult(True, "Pomodoro session stopped.", _status_data(current))


def skip(provider):
  service = _service(provider)
  try:
    current = service.skip()
  except PomodoroError as exc:
    raise ToolError(str(exc)) from exc
  return ToolResult(
      True,
      f"Skipped to Pomodoro {current.phase_label}.",
      _status_data(current),
  )
