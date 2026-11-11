"""Validated developer assistant tools.

Read-only tools are safe. Tools that write a file or start a process use the
``confirm`` permission so the assistant must ask before acting.
"""

from __future__ import annotations

import sys

from core.developer import DeveloperError
from tools.base import ToolError, ToolResult


def _service(provider):
  service = provider() if callable(provider) else provider
  if service is None:
    raise ToolError("Developer tools are disabled.")
  return service


def _result(provider, method, *args, **kwargs):
  service = _service(provider)
  try:
    text, data = getattr(service, method)(*args, **kwargs)
  except DeveloperError as exc:
    raise ToolError(str(exc)) from exc
  return ToolResult(True, text, data)


def explain_source(provider, path):
  return _result(provider, "explain_source", path)


def inspect_project(provider, path="."):
  return _result(provider, "inspect_project", path)


def search_code(provider, query, path="."):
  return _result(provider, "search_code", query, path)


def find_symbol(provider, name, path="."):
  return _result(provider, "find_symbol", name, path)


def explain_architecture(provider, path="."):
  return _result(provider, "explain_architecture", path)


def generate_snippet(provider, language="python", description=""):
  return _result(provider, "generate_snippet", language, description)


def debug_error(provider, error, path=""):
  return _result(provider, "debug_error", error, path)


def explain_terminal_error(provider, error):
  return _result(provider, "explain_terminal_error", error)


def interpret_test_failure(provider, output):
  return _result(provider, "interpret_test_failure", output)


def format_source(provider, path):
  return _result(provider, "format_source", path, write=True)


def generate_documentation(provider, path=".", output=""):
  return _result(provider, "generate_documentation", path, output, write=True)


def generate_readme(provider, path=".", output="README.generated.md"):
  return _result(provider, "generate_readme", path, output, write=True)


def run_tests(provider, path="."):
  return _result(provider, "run_tests", path)


def git_status(provider):
  return _result(provider, "git_status")


def git_log(provider, limit=10):
  return _result(provider, "git_log", limit)


def git_changes(provider):
  return _result(provider, "git_changes")


def commit_message(provider):
  return _result(provider, "commit_message")


def search_documentation(provider, query):
  return _result(provider, "search_documentation", query)


def create_template(provider, kind, name, path="."):
  return _result(provider, "create_template", kind, name, path)


def run_script(provider, path, arguments=""):
  return _result(provider, "run_script", path, arguments)


TOOL_METHODS = {
  "developer_explain": explain_source,
  "developer_inspect": inspect_project,
  "developer_search": search_code,
  "developer_find": find_symbol,
  "developer_architecture": explain_architecture,
  "developer_snippet": generate_snippet,
  "developer_debug": debug_error,
  "developer_terminal": explain_terminal_error,
  "developer_failures": interpret_test_failure,
  "developer_format": format_source,
  "developer_docs": generate_documentation,
  "developer_readme": generate_readme,
  "developer_tests": run_tests,
  "developer_git_status": git_status,
  "developer_git_log": git_log,
  "developer_git_changes": git_changes,
  "developer_commit_message": commit_message,
  "developer_search_docs": search_documentation,
  "developer_template": create_template,
  "developer_run_script": run_script,
}


def call(provider, name, arguments):
  """Dispatch a developer tool by its registered name."""
  handler = TOOL_METHODS.get(name)
  if handler is None:
    raise ToolError("Unknown developer action.")
  return handler(provider, **arguments)


if __name__ == "__main__":  # pragma: no cover
  print("developer tools:", ", ".join(sorted(TOOL_METHODS)), file=sys.stderr)
