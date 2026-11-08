"""Launching allowlisted applications, websites, paths and commands.

Applications and terminal commands must appear in the configuration
allowlists. Commands run without a shell, with validated arguments, so nothing
can be chained or injected.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import psutil

from tools.base import ToolError, ToolResult
from tools.desktop import request_close, visible_windows

log = logging.getLogger(__name__)

SHELL_CHARACTERS = set("&|<>^%!`$")
MAX_ARGUMENTS = 8
MAX_OUTPUT_CHARS = 1500
POWERSHELL_FLAGS = ["-NoProfile", "-NonInteractive"]

# Windows, shell hosts and JARVIS itself are never closed by "close all".
PROTECTED_PROCESSES = frozenset(
    {
        "applicationframehost",
        "cmd",
        "conhost",
        "csrss",
        "ctfmon",
        "dwm",
        "explorer",
        "fontdrvhost",
        "lockapp",
        "lsass",
        "powershell",
        "pwsh",
        "python",
        "pythonw",
        "runtimebroker",
        "searchhost",
        "services",
        "shellexperiencehost",
        "sihost",
        "smss",
        "startmenuexperiencehost",
        "svchost",
        "systemsettings",
        "textinputhost",
        "windowsterminal",
        "winlogon",
    }
)


def validate_url(url):
  """Return a normalized http(s) URL or raise ToolError."""
  text = (url or "").strip()
  if not text:
    raise ToolError("a website address is required")
  if len(text) > 2000:
    raise ToolError("that address is too long")
  if any(character.isspace() for character in text):
    raise ToolError("that address contains spaces")
  candidate = text if "://" in text else f"https://{text}"
  parsed = urlparse(candidate)
  if parsed.scheme not in {"http", "https"}:
    raise ToolError("only http and https addresses can be opened")
  host = parsed.netloc
  if not host or ("." not in host and host != "localhost"):
    raise ToolError("that address has no host name")
  return candidate


def open_url(config, url, opener=None):
  candidate = validate_url(url)
  openers = opener or webbrowser.open
  try:
    opened = openers(candidate)
  except Exception as exc:
    raise ToolError(f"Windows could not open the browser: {exc}") from exc
  if opened is False:
    raise ToolError("Windows did not open a browser for that address")
  return ToolResult(
      True, f"Opening {candidate} in the browser, sir.", {"url": candidate}
  )


def _allowed_apps(config):
  return {str(name).strip().lower() for name in config.allowed_apps}


def _clean_app(name):
  cleaned = str(name or "").strip().lower()
  if cleaned.endswith(".exe"):
    cleaned = cleaned[:-4]
  return cleaned


def open_app(config, application):
  name = _clean_app(application)
  if not name:
    raise ToolError("an application name is required")
  if name not in _allowed_apps(config):
    raise ToolError(f"{application} is not on the allowed application list")

  executable = shutil.which(name) or shutil.which(f"{name}.exe") or name
  try:
    if executable.lower().endswith(".exe"):
      subprocess.Popen([executable], close_fds=True)
    else:
      os.startfile(executable)  # App Paths (e.g. msedge, chrome)
  except OSError as exc:
    raise ToolError(f"I could not open {name}: {exc}") from exc
  return ToolResult(
      True, f"Opening {name}, sir.", {"application": name, "executable": executable}
  )


def open_path(config, path):
  text = str(path or "").strip().strip('"').strip("'")
  if not text:
    raise ToolError("a path is required")
  candidate = Path(text).expanduser()
  if not candidate.is_absolute():
    candidate = config.resolved_root / candidate
  target = candidate.resolve()
  if not target.exists():
    raise ToolError(f"{text} does not exist")
  try:
    os.startfile(str(target))
  except OSError as exc:
    raise ToolError(f"I could not open {target.name}: {exc}") from exc
  return ToolResult(True, f"Opening {target.name}.", {"path": str(target)})


def run_command(config, command, arguments=""):
  name = _clean_app(command)
  if not name:
    raise ToolError("a command is required")
  allowed = {str(entry).strip().lower() for entry in config.allowed_commands}
  if name not in allowed:
    raise ToolError(f"{command} is not an allowed command")

  try:
    parts = shlex.split(arguments or "")
  except ValueError as exc:
    raise ToolError(f"I could not read those arguments: {exc}") from exc
  if len(parts) > MAX_ARGUMENTS:
    raise ToolError("too many arguments")
  for part in parts:
    if SHELL_CHARACTERS & set(part):
      raise ToolError("shell characters are not allowed in arguments")
    if len(part) > 200:
      raise ToolError("an argument is too long")

  executable = shutil.which(name) or shutil.which(f"{name}.exe")
  if executable is None:
    raise ToolError(f"{name} was not found on this system")

  try:
    completed = subprocess.run(
        [executable, *parts],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=config.command_timeout_s,
        shell=False,
    )
  except subprocess.TimeoutExpired as exc:
    raise ToolError(
        f"{name} did not finish within {config.command_timeout_s:.0f} seconds"
    ) from exc
  except OSError as exc:
    raise ToolError(f"I could not run {name}: {exc}") from exc

  output = (completed.stdout or "").strip() or (completed.stderr or "").strip()
  if not output:
    output = f"{name} finished with exit code {completed.returncode}."
  shown = output
  if len(shown) > MAX_OUTPUT_CHARS:
    shown = shown[:MAX_OUTPUT_CHARS].rstrip() + " ... output truncated."

  ok = completed.returncode == 0
  text = shown if ok else f"{name} failed: {shown}"
  return ToolResult(
      ok,
      text,
      {
          "command": name,
          "arguments": parts,
          "exit_code": completed.returncode,
          "output": output,
      },
  )


def _base_name(name):
  text = str(name or "").strip().lower()
  return text[:-4] if text.endswith(".exe") else text


def _process_names():
  """Map process id to its name without the .exe suffix."""
  names = {}
  for process in psutil.process_iter(["pid", "name"]):
    try:
      info = process.info
      names[int(info["pid"])] = _base_name(info.get("name"))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
      continue
  return names


def _self_pids():
  """JARVIS itself, its parents and its children are off limits."""
  try:
    current = psutil.Process()
  except psutil.Error:
    return set()
  pids = {current.pid}
  try:
    pids.update(parent.pid for parent in current.parents())
    pids.update(child.pid for child in current.children(recursive=True))
  except psutil.Error:
    pass
  return pids


def select_close_targets(windows, names, own_pids):
  """Choose closable windows: visible apps only, never system or our own."""
  selected = {}
  for window in windows:
    pid = window["pid"]
    name = names.get(pid, "")
    if pid in own_pids or name in PROTECTED_PROCESSES:
      continue
    if pid in selected:
      continue
    selected[pid] = dict(window, process=name)
  return list(selected.values())


def _all_targets():
  return select_close_targets(visible_windows(), _process_names(), _self_pids())


def close_app(config, application):
  """Close only the named application's windows (or its processes)."""
  name = _clean_app(application)
  if not name:
    raise ToolError("an application name is required")
  if name not in _allowed_apps(config):
    raise ToolError(f"{application} is not on the allowed application list")
  if name in PROTECTED_PROCESSES:
    raise ToolError(f"{name} is a system process and will not be closed")

  names = _process_names()
  matching = {pid for pid, process in names.items() if process == name}
  own = _self_pids()
  windows = [
      window for window in visible_windows() if window["pid"] in matching
  ]

  asked = 0
  for window in windows:
    try:
      request_close(window["hwnd"])
      asked += 1
    except ToolError:
      log.debug("Could not close window %r", window["title"], exc_info=True)

  # A named, windowless instance is stopped, but only because the user named
  # it explicitly; system and JARVIS's own processes stay protected.
  terminated = 0
  windowed = {window["pid"] for window in windows}
  for pid in matching - windowed:
    if pid in own:
      continue
    try:
      psutil.Process(pid).terminate()
      terminated += 1
    except (psutil.NoSuchProcess, psutil.AccessDenied):
      continue

  if not asked and not terminated:
    raise ToolError(f"{name} is not running")

  actions = []
  if asked:
    actions.append(f"asked {asked} window{'s' if asked != 1 else ''} to close")
  if terminated:
    actions.append(
        f"stopped {terminated} background process"
        f"{'es' if terminated != 1 else ''}"
    )
  return ToolResult(
      True,
      f"Closing {name}: " + " and ".join(actions) + ".",
      {"application": name, "windows": asked, "processes": terminated},
  )


def close_all_apps(config):
  """Ask every open application window to close, leaving system processes."""
  targets = _all_targets()
  if not targets:
    raise ToolError("there are no open application windows to close")

  asked = 0
  for target in targets:
    try:
      request_close(target["hwnd"])
      asked += 1
    except ToolError:
      log.debug("Could not close %r", target["title"], exc_info=True)
  if not asked:
    raise ToolError("Windows did not accept any close requests")

  names = sorted({target["process"] or target["title"] for target in targets})
  return ToolResult(
      True,
      f"Asked {asked} application window{'s' if asked != 1 else ''} to close: "
      + ", ".join(names)
      + ".",
      {"closed": asked, "applications": names},
  )


def preview_close_app(config, arguments):
  name = _clean_app(arguments.get("application"))
  if name not in _allowed_apps(config):
    return f"Close {name or 'that application'} (it is not on the allowed list)"
  names = _process_names()
  count = len(
      [
          window
          for window in visible_windows()
          if names.get(window["pid"]) == name
      ]
  )
  return f"Close {count} {name} window{'s' if count != 1 else ''}"


def preview_close_all(config):
  targets = _all_targets()
  if not targets:
    return "Close all open applications (none are currently open)"
  names = sorted({target["process"] or target["title"] for target in targets})
  return (
      f"Close {len(targets)} open application window"
      f"{'s' if len(targets) != 1 else ''} ({', '.join(names)})"
  )
