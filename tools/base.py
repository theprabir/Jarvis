"""Tool registry primitives: schemas, permissions, validation and results.

A tool declares its arguments and their types, a permission level and a
timeout. The registry validates arguments before calling the handler and turns
every failure into a :class:`ToolResult`, so the conversation loop never sees
an exception from a tool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

SAFE = "safe"
CONFIRM = "confirm"


class ToolError(Exception):
  """Expected, user-facing tool failure (bad input, refused action)."""


@dataclass(frozen=True)
class ToolResult:
  """What a tool returns: a spoken-safe sentence plus optional structured data."""

  ok: bool
  text: str
  data: dict = field(default_factory=dict)
  error: str = ""

  def __post_init__(self):
    if not isinstance(self.text, str) or not self.text.strip():
      raise ValueError("tool result text must be a non-empty string")
    if not isinstance(self.data, dict):
      raise ValueError("tool result data must be a dict")


@dataclass(frozen=True)
class Tool:
  """A callable capability with validated arguments."""

  name: str
  description: str
  arguments: dict
  handler: Callable
  required: tuple = ()
  permission: str = SAFE
  timeout_s: float = 10.0
  preview: Callable = None

  def validate(self, arguments):
    """Check argument names and types; return a clean copy."""
    if not isinstance(arguments, dict):
      raise ToolError("arguments must be an object")
    unknown = sorted(set(arguments) - set(self.arguments))
    if unknown:
      raise ToolError("unknown argument(s): " + ", ".join(unknown))
    missing = [name for name in self.required if name not in arguments]
    if missing:
      raise ToolError("missing argument(s): " + ", ".join(missing))
    checked = {}
    for name, value in arguments.items():
      expected = self.arguments[name]
      if expected is int and isinstance(value, bool):
        raise ToolError(f"argument {name} must be an integer")
      if not isinstance(value, expected):
        raise ToolError(
            f"argument {name} must be {expected.__name__},"
            f" not {type(value).__name__}"
        )
      checked[name] = value
    return checked

  def describe(self, arguments):
    """Human-readable confirmation line for a pending action."""
    if self.preview is not None:
      try:
        return str(self.preview(arguments))
      except ToolError as exc:
        return f"{self.description} ({exc})"
      except Exception:
        log.debug("Tool preview failed", exc_info=True)
    details = ", ".join(f"{key} {value!r}" for key, value in arguments.items())
    return self.description + (f" ({details})" if details else "")


class ToolRegistry:
  """Name-to-tool lookup with validated execution."""

  def __init__(self):
    self._tools = {}

  def register(self, tool):
    if tool.name in self._tools:
      raise ValueError(f"tool already registered: {tool.name}")
    self._tools[tool.name] = tool

  def get(self, name):
    return self._tools.get(name)

  def names(self):
    return tuple(sorted(self._tools))

  def requires_confirmation(self, name):
    tool = self.get(name)
    return tool is not None and tool.permission == CONFIRM

  def execute(self, name, arguments=None):
    """Validate and run a tool; never raises for tool-level failures."""
    tool = self.get(name)
    if tool is None:
      return ToolResult(False, "I don't have a tool for that, sir.",
                        error=f"unknown tool: {name}")
    try:
      checked = tool.validate(arguments or {})
    except ToolError as exc:
      log.info("Tool %s rejected its arguments: %s", name, exc)
      return ToolResult(False, f"I can't run that: {exc}.", error=str(exc))

    try:
      result = tool.handler(**checked)
    except ToolError as exc:
      log.info("Tool %s failed: %s", name, exc)
      return ToolResult(False, str(exc), error=str(exc))
    except Exception as exc:  # a tool bug must not end the session
      log.exception("Tool %s raised an unexpected error", name)
      return ToolResult(
          False, "That tool failed unexpectedly, sir.", error=repr(exc)
      )

    if not isinstance(result, ToolResult):
      log.error("Tool %s returned %r", name, type(result).__name__)
      return ToolResult(
          False, "That tool returned an invalid result, sir.",
          error="invalid tool result",
      )
    return result
