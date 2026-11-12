"""Computer interaction tools and the registry factory used by the assistant."""

from __future__ import annotations

from tools import (
    application,
    apps,
    calendar,
    clipboard,
    clipboard_history,
    desktop,
    developer,
    documents,
    files,
    goals,
    habits,
    mail,
    notes,
    pomodoro,
    productivity,
    summaries,
    system,
    time_tracking,
)
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


def build_registry(
    settings,
    document_service=None,
    productivity_service=None,
    notes_service=None,
    habits_service=None,
    goals_service=None,
    time_service=None,
    calendar_service=None,
    email_service=None,
    pomodoro_service=None,
    clipboard_service=None,
    summary_service=None,
    developer_service=None,
    application_service=None,
):
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
      lambda path=".", limit=0: files.list_files(config, path, limit or None),
      {"path": str, "limit": int},
  )
  add(
      "search_files",
      "Search for files by name",
      lambda name, path=".": files.search_files(config, name, path),
      {"name": str, "path": str},
      required=("name",),
  )

  # Document intelligence
  if document_service is not None:
    add(
        "document_read",
        "Read and extract text and tables from a document",
        lambda path: documents.read_document(document_service, path),
        {"path": str},
        required=("path",),
    )
    add(
        "document_index",
        "Index one document for local search",
        lambda path: documents.index_document(document_service, path),
        {"path": str},
        required=("path",),
        timeout_s=60.0,
    )
    add(
        "document_index_folder",
        "Index supported documents in a folder",
        lambda path=".", recursive=True: documents.index_folder(
            document_service, path, recursive
        ),
        {"path": str, "recursive": bool},
        timeout_s=300.0,
    )
    add(
        "document_search",
        "Search indexed document passages",
        lambda query: documents.search_documents(document_service, query),
        {"query": str},
        required=("query",),
    )
    add(
        "document_extract",
        "Extract dates, action items or tables from a document",
        lambda path, kind="dates": documents.extract_document(
            document_service, path, kind
        ),
        {"path": str, "kind": str},
        required=("path",),
    )
    add(
        "document_convert",
        "Convert a document to another supported format",
        lambda source, format="", target="": documents.convert_document(
            document_service, source, format, target
        ),
        {"source": str, "format": str, "target": str},
        required=("source",),
        permission=CONFIRM,
        timeout_s=60.0,
        preview=lambda arguments: (
            f'Convert "{shown(arguments.get("source"))}" to'
            f' {arguments.get("format") or arguments.get("target") or "a new format"}'
        ),
    )
    add(
        "document_write",
        "Create a document in a supported format",
        lambda path, content, title="": documents.write_document(
            document_service, path, content, title
        ),
        {"path": str, "content": str, "title": str},
        required=("path", "content"),
        permission=CONFIRM,
        timeout_s=60.0,
        preview=lambda arguments: (
            f'Create "{shown(arguments.get("path"))}" with'
            f' {len(arguments.get("content", ""))} characters'
        ),
    )
    add(
        "document_report",
        "Create a report from one or more documents",
        lambda paths, output, title="": documents.report_documents(
            document_service, paths, output, title
        ),
        {"paths": str, "output": str, "title": str},
        required=("paths", "output"),
        permission=CONFIRM,
        timeout_s=120.0,
        preview=lambda arguments: (
            f'Write a document report to "{shown(arguments.get("output"))}"'
        ),
    )

  # Local productivity to-dos. The provider may be the service itself or a
  # lazy callable, so an assistant does not open its database until first use.
  if productivity_service is not None and settings.productivity.enabled:
    add(
        "todo_add",
        "Add a local to-do with an optional due date, reminder and recurrence",
        lambda title, details="", priority="", due="", remind="",
        recurrence="none", tags="": productivity.add_todo(
            productivity_service,
            title,
            details,
            priority,
            due,
            remind,
            recurrence,
            tags,
        ),
        {
            "title": str,
            "details": str,
            "priority": str,
            "due": str,
            "remind": str,
            "recurrence": str,
            "tags": str,
        },
        required=("title",),
    )
    add(
        "todo_list",
        "List local to-dos by status",
        lambda status="open", limit=20: productivity.list_todos(
            productivity_service, status, limit
        ),
        {"status": str, "limit": int},
    )
    add(
        "plan_day",
        "Show a daily agenda from open to-dos",
        lambda day="": productivity.plan_day(productivity_service, day),
        {"day": str},
    )
    add(
        "plan_week",
        "Show a seven-day agenda from open to-dos",
        lambda start="": productivity.plan_week(productivity_service, start),
        {"start": str},
    )
    add(
        "todo_show",
        "Show one local to-do",
        lambda id: productivity.show_todo(productivity_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "todo_update",
        "Update a local to-do",
        lambda id, title="", details="", priority="", due="", remind="",
        recurrence="", tags="", clear_due=False, clear_remind=False: (
            productivity.update_todo(
                productivity_service,
                id,
                title,
                details,
                priority,
                due,
                remind,
                recurrence,
                tags,
                clear_due,
                clear_remind,
            )
        ),
        {
            "id": int,
            "title": str,
            "details": str,
            "priority": str,
            "due": str,
            "remind": str,
            "recurrence": str,
            "tags": str,
            "clear_due": bool,
            "clear_remind": bool,
        },
        required=("id",),
    )
    add(
        "todo_complete",
        "Complete a local to-do and create its next recurring occurrence",
        lambda id: productivity.complete_todo(productivity_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "todo_snooze",
        "Snooze a local to-do reminder",
        lambda id, minutes=10: productivity.snooze_todo(
            productivity_service, id, minutes
        ),
        {"id": int, "minutes": int},
        required=("id",),
    )
    add(
        "todo_delete",
        "Delete a local to-do",
        lambda id: productivity.delete_todo(productivity_service, id),
        {"id": int},
        required=("id",),
        permission=CONFIRM,
        preview=lambda arguments: f"Delete to-do #{arguments.get('id', '')}",
    )

  # Local notes. The provider is lazy for the same reason as productivity:
  # no database is opened until the first note tool call.
  if notes_service is not None and settings.notes.enabled:
    add(
        "note_add",
        "Save a local note",
        lambda content, title="": notes.add_note(
            notes_service, content, title
        ),
        {"content": str, "title": str},
        required=("content",),
    )
    add(
        "note_list",
        "List local notes",
        lambda limit=20: notes.list_notes(notes_service, limit),
        {"limit": int},
    )
    add(
        "note_show",
        "Show one local note",
        lambda id: notes.show_note(notes_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "note_search",
        "Search local notes",
        lambda query, limit=10: notes.search_notes(
            notes_service, query, limit
        ),
        {"query": str, "limit": int},
        required=("query",),
    )
    add(
        "note_update",
        "Update a local note",
        lambda id, content="", title="": notes.update_note(
            notes_service, id, content, title
        ),
        {"id": int, "content": str, "title": str},
        required=("id",),
    )
    add(
        "note_delete",
        "Delete a local note",
        lambda id: notes.delete_note(notes_service, id),
        {"id": int},
        required=("id",),
        permission=CONFIRM,
        preview=lambda arguments: f"Delete note #{arguments.get('id', '')}",
    )

  # Daily habits. The service is lazy for the same reason as to-dos and notes.
  if habits_service is not None and settings.habits.enabled:
    add(
        "habit_add",
        "Add a daily habit with an optional reminder time",
        lambda title, details="", remind="": habits.add_habit(
            habits_service, title, details, remind
        ),
        {"title": str, "details": str, "remind": str},
        required=("title",),
    )
    add(
        "habit_list",
        "List daily habits with today's state and streaks",
        lambda: habits.list_habits(habits_service),
    )
    add(
        "habit_show",
        "Show one habit, its streak and check-in details",
        lambda id: habits.show_habit(habits_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "habit_complete",
        "Check in a habit for today",
        lambda id: habits.complete_habit(habits_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "habit_undo",
        "Undo today's habit check-in",
        lambda id: habits.undo_habit(habits_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "habit_update",
        "Rename a habit, change details or change its daily reminder",
        lambda id, title="", details="", remind="": habits.update_habit(
            habits_service, id, title, details, remind
        ),
        {"id": int, "title": str, "details": str, "remind": str},
        required=("id",),
    )
    add(
        "habit_delete",
        "Delete a habit and its check-in history",
        lambda id: habits.delete_habit(habits_service, id),
        {"id": int},
        required=("id",),
        permission=CONFIRM,
        preview=lambda arguments: f"Delete habit #{arguments.get('id', '')}",
    )

  # Local goals. This domain is independent of to-dos, notes and habits.
  if goals_service is not None and settings.goals.enabled:
    add(
        "goal_add",
        "Add a goal with optional numeric target and target date",
        lambda title, target="", unit="", due="", details="": goals.add_goal(
            goals_service, title, target, unit, due, details
        ),
        {
            "title": str,
            "target": str,
            "unit": str,
            "due": str,
            "details": str,
        },
        required=("title",),
    )
    add(
        "goal_list",
        "List goals with progress and target dates",
        lambda status="active": goals.list_goals(goals_service, status),
        {"status": str},
    )
    add(
        "goal_show",
        "Show one goal with progress and target date",
        lambda id: goals.show_goal(goals_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "goal_progress",
        "Set or add numeric progress for a goal",
        lambda id, value, add=False: goals.progress_goal(
            goals_service, id, value, add
        ),
        {"id": int, "value": str, "add": bool},
        required=("id", "value"),
    )
    add(
        "goal_complete",
        "Mark a goal completed",
        lambda id: goals.complete_goal(goals_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "goal_reopen",
        "Reopen a completed goal",
        lambda id: goals.reopen_goal(goals_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "goal_update",
        "Update a goal title, details, target, unit or target date",
        lambda id, title="", details="", target="", unit="", due="",
        clear_due=False: goals.update_goal(
            goals_service, id, title, details, target, unit, due, clear_due
        ),
        {
            "id": int,
            "title": str,
            "details": str,
            "target": str,
            "unit": str,
            "due": str,
            "clear_due": bool,
        },
        required=("id",),
    )
    add(
        "goal_delete",
        "Delete a goal",
        lambda id: goals.delete_goal(goals_service, id),
        {"id": int},
        required=("id",),
        permission=CONFIRM,
        preview=lambda arguments: f"Delete goal #{arguments.get('id', '')}",
    )

  # Local time tracking. Elapsed time is derived from timestamps, so no
  # additional scheduler or worker is needed.
  if time_service is not None and settings.time.enabled:
    add(
        "time_start",
        "Start tracking time for a task",
        lambda label: time_tracking.start_time(time_service, label),
        {"label": str},
        required=("label",),
    )
    add(
        "time_stop",
        "Stop the active time entry",
        lambda: time_tracking.stop_time(time_service),
    )
    add(
        "time_status",
        "Show the active time entry and elapsed time",
        lambda: time_tracking.status_time(time_service),
    )
    add(
        "time_report",
        "Show tracked daily totals",
        lambda day="": time_tracking.report_time(time_service, day),
        {"day": str},
    )
    add(
        "time_week",
        "Show tracked totals for the current week",
        lambda start="": time_tracking.week_time(time_service, start),
        {"start": str},
    )
    add(
        "time_list",
        "List recent time entries",
        lambda limit=20: time_tracking.list_time(time_service, limit),
        {"limit": int},
    )
    add(
        "time_delete",
        "Delete a time entry",
        lambda id: time_tracking.delete_time(time_service, id),
        {"id": int},
        required=("id",),
        permission=CONFIRM,
        preview=lambda arguments: f"Delete time entry #{arguments.get('id', '')}",
    )

  # Local calendar events and daily agendas.
  if calendar_service is not None and settings.calendar.enabled:
    add(
        "calendar_add",
        "Create a local calendar event",
        lambda title, start, duration=60, details="": calendar.add_event(
            calendar_service, title, start, duration, details
        ),
        {"title": str, "start": str, "duration": int, "details": str},
        required=("title", "start"),
    )
    add(
        "calendar_agenda",
        "Show the daily calendar agenda",
        lambda day="": calendar.agenda_calendar(calendar_service, day),
        {"day": str},
    )
    add(
        "calendar_list",
        "List upcoming calendar events",
        lambda limit=20: calendar.list_calendar(calendar_service, limit),
        {"limit": int},
    )
    add(
        "calendar_show",
        "Show one calendar event",
        lambda id: calendar.show_calendar(calendar_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "calendar_update",
        "Update an event title, start, duration or details",
        lambda id, title="", start="", duration=0, details="": (
            calendar.update_calendar(
                calendar_service, id, title, start, duration, details
            )
        ),
        {
            "id": int,
            "title": str,
            "start": str,
            "duration": int,
            "details": str,
        },
        required=("id",),
    )
    add(
        "calendar_delete",
        "Delete a calendar event",
        lambda id: calendar.delete_calendar(calendar_service, id),
        {"id": int},
        required=("id",),
        permission=CONFIRM,
        preview=lambda arguments: f"Delete event #{arguments.get('id', '')}",
    )

  # Local email drafts and optional connected mailbox actions.
  if email_service is not None and settings.email.enabled:
    add(
        "email_draft",
        "Create a local email draft",
        lambda recipient, cc="", subject="", body="": mail.draft_email(
            email_service, recipient, cc, subject, body
        ),
        {"recipient": str, "cc": str, "subject": str, "body": str},
        required=("recipient",),
    )
    add(
        "email_list",
        "List local email drafts",
        lambda limit=20: mail.list_drafts(email_service, limit),
        {"limit": int},
    )
    add(
        "email_show",
        "Show a local email draft",
        lambda id: mail.show_draft(email_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "email_update",
        "Update a local email draft",
        lambda id, recipient="", cc="", subject="", body="": (
            mail.update_draft(email_service, id, recipient, cc, subject, body)
        ),
        {
            "id": int,
            "recipient": str,
            "cc": str,
            "subject": str,
            "body": str,
        },
        required=("id",),
    )
    add(
        "email_delete",
        "Delete a local email draft",
        lambda id: mail.delete_draft(email_service, id),
        {"id": int},
        required=("id",),
        permission=CONFIRM,
        preview=lambda arguments: f"Delete email draft #{arguments.get('id', '')}",
    )
    add(
        "email_send",
        "Send a local email draft through SMTP",
        lambda id: mail.send_draft(email_service, id),
        {"id": int},
        required=("id",),
        permission=CONFIRM,
        preview=lambda arguments: f"Send email draft #{arguments.get('id', '')}",
        timeout_s=settings.email.timeout_s + 5.0,
    )
    add(
        "email_search",
        "Search the connected mailbox",
        lambda query="", limit=0: mail.search_inbox(
            email_service, query, limit
        ),
        {"query": str, "limit": int},
    )
    add(
        "email_read",
        "Read a message from the connected mailbox",
        lambda uid: mail.read_message(email_service, uid),
        {"uid": int},
        required=("uid",),
    )
    add(
        "email_summarize",
        "Summarize a message from the connected mailbox",
        lambda uid: mail.summarize_message(email_service, uid),
        {"uid": int},
        required=("uid",),
    )
    add(
        "email_reply",
        "Create a reply draft for a connected mailbox message",
        lambda uid: mail.reply_message(email_service, uid),
        {"uid": int},
        required=("uid",),
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
      lambda text: clipboard.copy_text(
          config, text, clipboard_service if settings.clipboard.enabled else None
      ),
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

  # Clipboard history. Copying above records entries through this service.
  if clipboard_service is not None and settings.clipboard.enabled:
    add(
        "clipboard_list",
        "List recent clipboard history",
        lambda limit=20: clipboard_history.list_entries(clipboard_service, limit),
        {"limit": int},
    )
    add(
        "clipboard_search",
        "Search clipboard history",
        lambda query, limit=10: clipboard_history.search_entries(
            clipboard_service, query, limit
        ),
        {"query": str, "limit": int},
        required=("query",),
    )
    add(
        "clipboard_show",
        "Show one clipboard entry",
        lambda id: clipboard_history.show_entry(clipboard_service, id),
        {"id": int},
        required=("id",),
    )
    add(
        "clipboard_delete",
        "Delete one clipboard entry",
        lambda id: clipboard_history.delete_entry(clipboard_service, id),
        {"id": int},
        required=("id",),
        permission=CONFIRM,
        preview=lambda arguments: f"Delete clipboard entry #{arguments.get('id', '')}",
    )
    add(
        "clipboard_clear",
        "Delete all clipboard history",
        lambda: clipboard_history.clear_history(clipboard_service),
        permission=CONFIRM,
        preview=lambda arguments: "Delete all clipboard history",
    )

  # Pomodoro phases use the existing timer scheduler.
  if pomodoro_service is not None and settings.pomodoro.enabled:
    add(
        "pomodoro_start",
        "Start a Pomodoro work phase",
        lambda minutes=0: pomodoro.start(pomodoro_service, minutes),
        {"minutes": int},
    )
    add(
        "pomodoro_status",
        "Show the current Pomodoro phase",
        lambda: pomodoro.status(pomodoro_service),
    )
    add(
        "pomodoro_stop",
        "Stop the current Pomodoro session",
        lambda: pomodoro.stop(pomodoro_service),
    )
    add(
        "pomodoro_skip",
        "Skip to the next Pomodoro phase",
        lambda: pomodoro.skip(pomodoro_service),
    )

  # Deterministic summaries combine the existing local services.
  if summary_service is not None:
    add(
        "summary_daily",
        "Summarize to-dos, calendar, tracked time and habits for a day",
        lambda day="": summaries.daily_summary(summary_service, day),
        {"day": str},
    )
    add(
        "summary_meeting",
        "Summarize meeting notes",
        lambda text: summaries.meeting_summary(summary_service, text),
        {"text": str},
        required=("text",),
    )

  # Developer assistant. Writes and execution are confirmation-only.
  if developer_service is not None and settings.developer.enabled:
    add(
        "developer_explain",
        "Explain classes, functions and imports in a source file",
        lambda path: developer.explain_source(developer_service, path),
        {"path": str},
        required=("path",),
    )
    add(
        "developer_inspect",
        "Inspect a project's files, tests and structure",
        lambda path=".": developer.inspect_project(developer_service, path),
        {"path": str},
    )
    add(
        "developer_search",
        "Search source and text files for a word",
        lambda query, path=".": developer.search_code(
            developer_service, query, path
        ),
        {"query": str, "path": str},
        required=("query",),
    )
    add(
        "developer_find",
        "Find a Python function or class by name",
        lambda name, path=".": developer.find_symbol(
            developer_service, name, path
        ),
        {"name": str, "path": str},
        required=("name",),
    )
    add(
        "developer_architecture",
        "Explain a project's modules, imports and structure",
        lambda path=".": developer.explain_architecture(
            developer_service, path
        ),
        {"path": str},
    )
    add(
        "developer_snippet",
        "Generate a small code snippet from a local template",
        lambda language="python", description="": developer.generate_snippet(
            developer_service, language, description
        ),
        {"language": str, "description": str},
    )
    add(
        "developer_debug",
        "Explain a Python error or traceback",
        lambda error, path="": developer.debug_error(
            developer_service, error, path
        ),
        {"error": str, "path": str},
        required=("error",),
    )
    add(
        "developer_terminal",
        "Explain a terminal error message",
        lambda error: developer.explain_terminal_error(
            developer_service, error
        ),
        {"error": str},
        required=("error",),
    )
    add(
        "developer_failures",
        "Interpret unittest or pytest failure output",
        lambda output: developer.interpret_test_failure(
            developer_service, output
        ),
        {"output": str},
        required=("output",),
    )
    add(
        "developer_format",
        "Format a Python source file",
        lambda path: developer.format_source(developer_service, path),
        {"path": str},
        required=("path",),
        permission=CONFIRM,
        preview=lambda args: f"Format {shown(args['path'])}",
    )
    add(
        "developer_docs",
        "Generate Markdown API documentation for a project folder",
        lambda path=".", output="": developer.generate_documentation(
            developer_service, path, output
        ),
        {"path": str, "output": str},
        permission=CONFIRM,
        preview=lambda args: (
            f"Generate documentation for {shown(args.get('path', '.'))}"
        ),
    )
    add(
        "developer_readme",
        "Generate a README draft for a project folder",
        lambda path=".", output="README.generated.md": developer.generate_readme(
            developer_service, path, output
        ),
        {"path": str, "output": str},
        permission=CONFIRM,
        preview=lambda args: (
            f"Generate {shown(args.get('output', 'README.generated.md'))}"
        ),
    )
    add(
        "developer_tests",
        "Run the project's unittest suite",
        lambda path=".": developer.run_tests(developer_service, path),
        {"path": str},
        permission=CONFIRM,
        timeout_s=300.0,
        preview=lambda args: f"Run tests in {shown(args.get('path', '.'))}",
    )
    add(
        "developer_git_status",
        "Show read-only Git working tree status",
        lambda: developer.git_status(developer_service),
    )
    add(
        "developer_git_log",
        "Show recent read-only Git commits",
        lambda limit=10: developer.git_log(developer_service, limit),
        {"limit": int},
    )
    add(
        "developer_git_changes",
        "Summarize unstaged Git changes",
        lambda: developer.git_changes(developer_service),
    )
    add(
        "developer_commit_message",
        "Suggest a commit message without committing",
        lambda: developer.commit_message(developer_service),
    )
    add(
        "developer_search_docs",
        "Search local README and documentation files",
        lambda query: developer.search_documentation(
            developer_service, query
        ),
        {"query": str},
        required=("query",),
    )
    add(
        "developer_template",
        "Create a small project template inside the sandbox",
        lambda kind, name, path=".": developer.create_template(
            developer_service, kind, name, path
        ),
        {"kind": str, "name": str, "path": str},
        required=("kind", "name"),
        permission=CONFIRM,
        preview=lambda args: (
            f"Create {args['kind']} template {args['name']}"
        ),
    )
    add(
        "developer_run_script",
        "Run one Python script in a subprocess",
        lambda path, arguments="": developer.run_script(
            developer_service, path, arguments
        ),
        {"path": str, "arguments": str},
        required=("path",),
        permission=CONFIRM,
        timeout_s=120.0,
        preview=lambda args: f"Run Python script {shown(args['path'])}",
    )

  # Application intelligence. Clipboard reads and key sequences are confirmed.
  if application_service is not None and settings.application.enabled:
    add(
        "application_context",
        "Show the active application and window title",
        lambda: application.context(application_service),
    )
    add(
        "application_shortcuts",
        "Show shortcuts for the active application",
        lambda: application.shortcuts(application_service),
    )
    add(
        "application_clipboard",
        "Read and classify the clipboard",
        lambda: application.clipboard(application_service),
        permission=CONFIRM,
        timeout_s=15.0,
        preview=lambda arguments: "Read the clipboard",
    )
    add(
        "application_explain",
        "Explain the error currently on the clipboard",
        lambda: application.explain(application_service),
        permission=CONFIRM,
        timeout_s=15.0,
        preview=lambda arguments: "Explain the copied error",
    )
    add(
        "application_browser",
        "Manage browser tabs with a validated shortcut",
        lambda action: application.browser(application_service, action),
        {"action": str},
        required=("action",),
        permission=CONFIRM,
        preview=lambda args: f"Use browser action {args.get('action', '')}",
    )
    add(
        "application_shortcut",
        "Send a shortcut for the active application",
        lambda action: application.shortcut(application_service, action),
        {"action": str},
        required=("action",),
        permission=CONFIRM,
        preview=lambda args: f"Send shortcut {args.get('action', '')}",
    )
    add(
        "application_page",
        "Read and summarize the active browser page",
        lambda: application.page(application_service),
        permission=CONFIRM,
        timeout_s=30.0,
        preview=lambda arguments: "Summarize the active browser page",
    )
    add(
        "application_screenshot",
        "Capture the screen and OCR its text",
        lambda: application.screenshot(application_service),
        permission=CONFIRM,
        timeout_s=60.0,
        preview=lambda arguments: "Capture and OCR the screen",
    )

  return registry
