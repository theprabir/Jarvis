"""Validated application-intelligence tool handlers."""

from __future__ import annotations

from core.application import ApplicationError
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Application intelligence is disabled.")
  return service


def _text(service, method, *args):
  try:
    return getattr(service, method)(*args)
  except ApplicationError as exc:
    raise ToolError(str(exc)) from exc


def context(provider):
  service = _service(provider)
  data = service.context()
  active = data["active"]
  return ToolResult(
      True,
      f"Active: {active['application']} — {active['title'] or '(no title)'}.",
      data,
  )


def shortcuts(provider):
  service = _service(provider)
  data = service.shortcuts()
  names = ", ".join(item["action"] for item in data["shortcuts"])
  return ToolResult(
      True,
      f"{data['application']} shortcuts: {names}.",
      data,
  )


def clipboard(provider):
  service = _service(provider)
  data = service.clipboard()
  return ToolResult(
      True,
      f"Clipboard: {data['chars']} characters ({data['kind']}).",
      data,
  )


def explain(provider):
  service = _service(provider)
  text, data = _text(service, "explain_clipboard")
  return ToolResult(True, text, data)


def browser(provider, action):
  service = _service(provider)
  text, data = _text(service, "browser_action", action)
  return ToolResult(True, text, data)


def shortcut(provider, action):
  service = _service(provider)
  text, data = _text(service, "shortcut", action)
  return ToolResult(True, text, data)


def page(provider):
  service = _service(provider)
  text, data = _text(service, "page_summary")
  return ToolResult(True, text, data)


def screenshot(provider):
  service = _service(provider)
  text, data = _text(service, "screenshot_text")
  return ToolResult(True, text, data)
