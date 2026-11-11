"""Validated daily and meeting summary tools."""

from __future__ import annotations

from core.summaries import SummaryError
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Summaries are disabled.")
  return service


def daily_summary(provider, day=""):
  service = _service(provider)
  try:
    text = service.daily(day)
  except SummaryError as exc:
    raise ToolError(str(exc)) from exc
  return ToolResult(True, text, {"day": day or "today", "summary": text})


def meeting_summary(provider, text):
  service = _service(provider)
  try:
    summary = service.meeting(text)
  except SummaryError as exc:
    raise ToolError(str(exc)) from exc
  return ToolResult(True, summary, {"summary": summary})
