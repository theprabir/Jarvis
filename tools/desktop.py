"""Desktop tools: screenshots, notifications and window management.

Screenshots and notifications use PowerShell with .NET (no extra packages).
Window enumeration and control use user32 through ctypes.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
from ctypes import wintypes
from datetime import datetime

from tools.base import ToolError, ToolResult

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 300
CREATE_NO_WINDOW = 0x08000000
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
WM_CLOSE = 0x0010

_SCREENSHOT_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -AssemblyName System.Drawing;"
    "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
    "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
    "$g=[System.Drawing.Graphics]::FromImage($bmp);"
    "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
    "$bmp.Save(__PATH__,[System.Drawing.Imaging.ImageFormat]::Png);"
    "$bmp.Dispose();$g.Dispose();"
)

_NOTIFY_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -AssemblyName System.Drawing;"
    "$n=New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon=[System.Drawing.SystemIcons]::Information;"
    "$n.BalloonTipTitle='JARVIS';"
    "$n.BalloonTipText=__TEXT__;"
    "$n.Visible=$true;"
    "$n.ShowBalloonTip(5000);"
    "Start-Sleep -Milliseconds 6000;"
    "$n.Dispose();"
)

_user32 = None
if os.name == "nt":
  _user32 = ctypes.WinDLL("user32", use_last_error=True)
  _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
  _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
  _user32.EnumWindows.restype = wintypes.BOOL
  _user32.IsWindowVisible.argtypes = [wintypes.HWND]
  _user32.IsWindowVisible.restype = wintypes.BOOL
  _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
  _user32.GetWindowTextLengthW.restype = ctypes.c_int
  _user32.GetWindowTextW.argtypes = [
      wintypes.HWND,
      wintypes.LPWSTR,
      ctypes.c_int,
  ]
  _user32.GetWindowTextW.restype = ctypes.c_int
  _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
  _user32.ShowWindow.restype = wintypes.BOOL
  _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
  _user32.SetForegroundWindow.restype = wintypes.BOOL
  _user32.GetWindowThreadProcessId.argtypes = [
      wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
  ]
  _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
  _user32.PostMessageW.argtypes = [
      wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
  ]
  _user32.PostMessageW.restype = wintypes.BOOL


def _powershell():
  return shutil.which("powershell") or shutil.which("pwsh")


def _quote(text):
  """Quote a string for PowerShell single-quoted syntax."""
  return "'" + str(text).replace("'", "''") + "'"


def _run_powershell(script, timeout):
  executable = _powershell()
  if executable is None:
    raise ToolError("PowerShell was not found on this system")
  try:
    return subprocess.run(
        [executable, *["-NoProfile", "-NonInteractive"], "-Command", script],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
  except subprocess.TimeoutExpired as exc:
    raise ToolError("PowerShell did not finish in time") from exc
  except OSError as exc:
    raise ToolError(f"I could not start PowerShell: {exc}") from exc


def take_screenshot(config):
  folder = config.resolved_root / "screenshots"
  try:
    folder.mkdir(parents=True, exist_ok=True)
  except OSError as exc:
    raise ToolError(f"I could not create the screenshot folder: {exc}") from exc

  target = folder / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
  script = _SCREENSHOT_SCRIPT.replace("__PATH__", _quote(str(target)))
  completed = _run_powershell(script, timeout=30)
  if completed.returncode != 0 or not target.is_file():
    detail = (completed.stderr or completed.stdout or "").strip()[:200]
    raise ToolError("I could not capture the screen" + (f": {detail}" if detail else ""))
  return ToolResult(
      True,
      f"Screenshot saved to screenshots/{target.name}.",
      {"path": str(target)},
  )


def show_notification(config, message):
  text = " ".join(str(message or "").split())
  if not text:
    raise ToolError("a notification message is required")
  if len(text) > MAX_MESSAGE_CHARS:
    text = text[:MAX_MESSAGE_CHARS].rstrip()

  script = _NOTIFY_SCRIPT.replace("__TEXT__", _quote(text))
  executable = _powershell()
  if executable is None:
    raise ToolError("PowerShell was not found on this system")
  try:
    subprocess.Popen(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
  except OSError as exc:
    raise ToolError(f"I could not show the notification: {exc}") from exc
  return ToolResult(
      True, f"Notification shown: {text}", {"message": text}
  )


def _windows_with_pid():
  if _user32 is None:
    raise ToolError("Window tools are only available on Windows")
  found = []

  @_WNDENUMPROC
  def callback(hwnd, lparam):
    if not _user32.IsWindowVisible(hwnd):
      return True
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
      return True
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    title = buffer.value.strip()
    if not title:
      return True
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    found.append((hwnd, title, int(pid.value)))
    return True

  _user32.EnumWindows(callback, 0)
  return found


def _windows():
  return [(hwnd, title) for hwnd, title, _pid in _windows_with_pid()]


def visible_windows():
  """Return visible titled windows with the id of the owning process."""
  return [
      {"hwnd": hwnd, "title": title, "pid": pid}
      for hwnd, title, pid in _windows_with_pid()
  ]


def request_close(hwnd):
  """Ask one window to close, letting the application prompt to save."""
  if _user32 is None:
    raise ToolError("Window tools are only available on Windows")
  if not _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
    raise ToolError("Windows did not accept the close request")


def list_windows(config):
  windows = _windows()
  if not windows:
    return ToolResult(True, "I see no visible windows, sir.", {"windows": []})
  titles = [title for _hwnd, title in windows[:15]]
  return ToolResult(
      True,
      "Open windows: " + "; ".join(titles) + ".",
      {"windows": titles},
  )


def window_action(config, title, action):
  action = (action or "").strip().lower()
  if action not in {"focus", "minimize", "maximize"}:
    raise ToolError("action must be focus, minimize or maximize")
  wanted = str(title or "").strip().lower()
  if not wanted:
    raise ToolError("a window title is required")

  matches = [
      (hwnd, text) for hwnd, text in _windows() if wanted in text.lower()
  ]
  if not matches:
    raise ToolError(f"No window matching {title!r} is open")

  hwnd, text = matches[0]
  if action == "minimize":
    _user32.ShowWindow(hwnd, SW_MINIMIZE)
    done = "Minimized"
  elif action == "maximize":
    _user32.ShowWindow(hwnd, SW_MAXIMIZE)
    done = "Maximized"
  else:
    _user32.ShowWindow(hwnd, SW_RESTORE)
    _user32.SetForegroundWindow(hwnd)
    done = "Focused"
  return ToolResult(
      True,
      f"{done} the {text} window.",
      {"window": text, "action": action},
  )
