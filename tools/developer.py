"""Validated read-only developer tools."""

from __future__ import annotations

from core.developer import DeveloperError
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Developer tools are disabled.")
  return service


def explain_source(provider, path):
  service = _service(provider)
  try:
    text, data = service.explain_source(path)
  except DeveloperError as exc:
    raise ToolError(str(exc)) from exc
  return ToolResult(True, text, data)


def inspect_project(provider, path="."):
  service = _service(provider)
  try:
    text, data = service.inspect_project(path)
  except DeveloperError as exc:
    raise ToolError(str(exc)) from exc
  return ToolResult(True, text, data)
