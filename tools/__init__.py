"""Computer interaction tools and the registry factory used by the assistant."""

from __future__ import annotations

from tools import apps, clipboard, desktop, files, system
from tools.base import (
    CONFIRM,
    SAFE,
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
)

__all__ = [
    "CONFIRM",
    "SAFE",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
]


def build_registry(settings):
  """Build the registry for the current configuration."""
  config = settings.tools
  registry = ToolRegistry()

  def shown(value):
    """Render a path briefly for a spoken confirmation."""
    text = str(value or "").replace("\\", "/").strip()
    return text[:-1] if len(text) > 1 and text.endswith("/") else text

  def add(
      name,
      description,
      handler,
      arguments=None,
      required=(),
      permission=SAFE,
      timeout_s=10.0,
      preview=None,
  ):
    registry.register(
        Tool(
            name=name,
            description=description,
            arguments=dict(arguments or {}),
            handler=handler,
            required=tuple(required),
            permission=permission,
            timeout_s=timeout_s,
            preview=preview,
        )
    )

  # System information
  add(
      "system_info",
      "Report CPU, memory, disk or battery status",
      lambda kind="all": system.system_info(config, kind),
      {"kind": str},
  )
  add(
      "processes",
      "List the processes using the most memory",
      lambda limit=8: system.top_processes(config, limit),
      {"limit": int},
  )

  # Applications, websites, folders and commands
  add(
      "open_app",
      "Open an application",
      lambda application: apps.open_app(config, application),
      {"application": str},
      required=("application",),
  )
  add(
      "close_app",
      "Close an application's open windows",
      lambda application: apps.close_app(config, application),
      {"application": str},
      required=("application",),
      permission=CONFIRM,
      preview=lambda arguments: apps.preview_close_app(config, arguments),
  )
  add(
      "close_all_apps",
      "Close every open application window, leaving system and background"
      " processes alone",
      lambda: apps.close_all_apps(config),
      permission=CONFIRM,
      preview=lambda arguments: apps.preview_close_all(config),
  )
  add(
      "open_url",
      "Open a website",
      lambda url: apps.open_url(config, url),
      {"url": str},
      required=("url",),
  )
  add(
      "open_path",
      "Open a file or folder",
      lambda path: apps.open_path(config, path),
      {"path": str},
      required=("path",),
  )
  add(
      "run_command",
      "Run an allowed terminal command",
      lambda command, arguments="": apps.run_command(config, command, arguments),
      {"command": str, "arguments": str},
      required=("command",),
      permission=CONFIRM,
      timeout_s=config.command_timeout_s + 5.0,
      preview=lambda arguments: (
          f'Run the command {arguments.get("command", "")}'
          + (
              f' {arguments["arguments"]}'
              if arguments.get("arguments")
              else ""
          )
      ),
  )

  # Files and folders
  add(
      "read_file",
      "Read a text file",
      lambda path: files.read_file(config, path),
      {"path": str},
      required=("path",),
  )
  add(
      "write_file",
      "Write a text file",
      lambda path, content: files.write_file(config, path, content),
      {"path": str, "content": str},
      required=("path", "content"),
      permission=CONFIRM,
      preview=lambda arguments: (
          f'Write {len(arguments.get("content", ""))} characters to'
          f' "{shown(arguments.get("path"))}"'
      ),
  )
  add(
      "create_folder",
      "Create a folder",
      lambda path: files.create_folder(config, path),
      {"path": str},
      required=("path",),
      permission=CONFIRM,
      preview=lambda arguments: (
          f'Create the folder "{shown(arguments.get("path"))}"'
      ),
  )
  add(
      "rename_path",
      "Rename a file or folder",
      lambda source, target: files.rename_path(config, source, target),
      {"source": str, "target": str},
      required=("source", "target"),
      permission=CONFIRM,
      preview=lambda arguments: (
          f'Rename "{shown(arguments.get("source"))}" to'
          f' "{shown(arguments.get("target"))}"'
      ),
  )
  add(
      "copy_path",
      "Copy a file or folder",
      lambda source, target: files.copy_path(config, source, target),
      {"source": str, "target": str},
      required=("source", "target"),
      permission=CONFIRM,
      preview=lambda arguments: (
          f'Copy "{shown(arguments.get("source"))}" to'
          f' "{shown(arguments.get("target"))}"'
      ),
  )
  add(
      "move_path",
      "Move a file or folder",
      lambda source, target: files.move_path(config, source, target),
      {"source": str, "target": str},
      required=("source", "target"),
      permission=CONFIRM,
      preview=lambda arguments: (
          f'Move "{shown(arguments.get("source"))}" to'
          f' "{shown(arguments.get("target"))}"'
      ),
  )
  add(
      "delete_path",
      "Delete a file or folder",
      lambda path: files.delete_path(config, path),
      {"path": str},
      required=("path",),
      permission=CONFIRM,
      preview=lambda arguments: (
          f'Delete "{shown(arguments.get("path"))}"'
      ),
  )
  add(
      "list_files",
      "List a folder",
      lambda path=".": files.list_files(config, path),
      {"path": str},
  )
  add(
      "search_files",
      "Search for files by name",
      lambda name, path=".": files.search_files(config, name, path),
      {"name": str, "path": str},
      required=("name",),
  )

  # Desktop
  add(
      "screenshot",
      "Capture the screen",
      lambda: desktop.take_screenshot(config),
      timeout_s=35.0,
  )
  add(
      "notify",
      "Show a Windows notification",
      lambda message: desktop.show_notification(config, message),
      {"message": str},
      required=("message",),
  )
  add(
      "windows_list",
      "List visible windows",
      lambda: desktop.list_windows(config),
  )
  add(
      "window_action",
      "Focus, minimize or maximize a window",
      lambda title, action: desktop.window_action(config, title, action),
      {"title": str, "action": str},
      required=("title", "action"),
  )

  # Clipboard
  add(
      "clipboard_copy",
      "Copy text to the clipboard",
      lambda text: clipboard.copy_text(config, text),
      {"text": str},
      required=("text",),
      timeout_s=15.0,
  )
  add(
      "clipboard_read",
      "Read the clipboard",
      lambda: clipboard.read_clipboard(config),
      permission=CONFIRM,
      timeout_s=15.0,
      preview=lambda arguments: "Read the clipboard",
  )

  return registry
