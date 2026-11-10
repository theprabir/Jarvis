"""Feature set 4.4 tests: tool registry, files, apps, intents and confirmations.

Hardware-facing tools (clipboard, screenshot, notifications) are exercised
during manual verification; here they are limited to validation and quoting so
the suite never touches the user's desktop or clipboard.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.assistant import Assistant
from core.config import ConfigError, ToolsConfig, load_settings
from core.intents import Intent, IntentRouter, plan_file_actions
from tools import build_registry
from tools.apps import select_close_targets, validate_url
from tools.base import Tool, ToolError, ToolRegistry, ToolResult
from tools.desktop import _quote


class FakeClient:
  def __init__(self, replies=("Model answer.",), error=None):
    self.replies = list(replies)
    self.error = error
    self.calls = []

  def chat(self, messages, max_tokens=None, temperature=None):
    self.calls.append({"messages": messages, "max_tokens": max_tokens})
    if self.error is not None:
      raise self.error
    content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
    return SimpleNamespace(
        content=content,
        total_ms=100.0,
        completion_tokens=4,
        generated_tokens_per_second=5.0,
    )


def make_settings(root, **overrides):
  options = {"root": str(root)}
  options.update(overrides)
  return dataclasses.replace(
      load_settings(), tools=ToolsConfig(**options)
  )


class RegistryTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.registry = build_registry(make_settings(self.tmp.name))

  def test_unknown_tool(self):
    result = self.registry.execute("launch_rocket")
    self.assertFalse(result.ok)
    self.assertIn("don't have a tool", result.text)

  def test_missing_argument(self):
    result = self.registry.execute("read_file", {})
    self.assertFalse(result.ok)
    self.assertIn("missing argument", result.text)

  def test_unknown_argument(self):
    result = self.registry.execute("read_file", {"path": "a.txt", "force": "1"})
    self.assertFalse(result.ok)
    self.assertIn("unknown argument", result.text)

  def test_wrong_type(self):
    result = self.registry.execute("processes", {"limit": "many"})
    self.assertFalse(result.ok)
    self.assertIn("must be int", result.text)

  def test_bool_is_not_an_int(self):
    result = self.registry.execute("processes", {"limit": True})
    self.assertFalse(result.ok)
    self.assertIn("must be an integer", result.text)

  def test_tool_error_is_reported(self):
    result = self.registry.execute("run_command", {"command": "rm"})
    self.assertFalse(result.ok)
    self.assertIn("not an allowed command", result.text)

  def test_invalid_result_from_a_tool(self):
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="bad",
            description="Returns the wrong type",
            arguments={},
            handler=lambda: "oops",
        )
    )
    result = registry.execute("bad")
    self.assertFalse(result.ok)
    self.assertIn("invalid result", result.text)

  def test_exception_from_a_tool_is_contained(self):
    def explode():
      raise RuntimeError("boom")

    registry = ToolRegistry()
    registry.register(
        Tool(name="boom", description="Explodes", arguments={}, handler=explode)
    )
    result = registry.execute("boom")
    self.assertFalse(result.ok)
    self.assertIn("failed unexpectedly", result.text)

  def test_permission_flags(self):
    self.assertTrue(self.registry.requires_confirmation("write_file"))
    self.assertTrue(self.registry.requires_confirmation("delete_path"))
    self.assertTrue(self.registry.requires_confirmation("run_command"))
    self.assertTrue(self.registry.requires_confirmation("clipboard_read"))
    self.assertTrue(self.registry.requires_confirmation("close_all_apps"))
    self.assertFalse(self.registry.requires_confirmation("read_file"))
    self.assertFalse(self.registry.requires_confirmation("system_info"))

  def test_registry_covers_every_tool_intent(self):
    from core.intents import TOOL_INTENTS

    # Document tools are registered only when a document service is supplied;
    # this bare registry still covers every non-document tool intent.
    self.assertEqual(
        set(self.registry.names()),
        set(TOOL_INTENTS) - {"document_convert"},
    )


class FileToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    self.registry = build_registry(make_settings(self.root))

  def test_write_then_read(self):
    written = self.registry.execute(
        "write_file", {"path": "notes.txt", "content": "hello"}
    )
    self.assertTrue(written.ok, written.text)
    self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"),
                     "hello")
    read = self.registry.execute("read_file", {"path": "notes.txt"})
    self.assertTrue(read.ok)
    self.assertIn("notes.txt has 1 line", read.text)
    self.assertIn("It starts: hello", read.text)

  def test_write_creates_parent_folders(self):
    result = self.registry.execute(
        "write_file", {"path": "a/b/c.txt", "content": "deep"}
    )
    self.assertTrue(result.ok, result.text)
    self.assertTrue((self.root / "a" / "b" / "c.txt").is_file())

  def test_read_size_limit(self):
    registry = build_registry(make_settings(self.root, max_read_bytes=1000))
    (self.root / "big.txt").write_text("x" * 2000, encoding="utf-8")
    result = registry.execute("read_file", {"path": "big.txt"})
    self.assertFalse(result.ok)
    self.assertIn("larger than", result.text)

  def test_list_files(self):
    (self.root / "one.txt").write_text("1", encoding="utf-8")
    (self.root / "two").mkdir()
    result = self.registry.execute("list_files")
    self.assertTrue(result.ok)
    self.assertIn("one.txt", result.text)
    self.assertIn("two/", result.text)

  def test_search_files(self):
    (self.root / "alpha.txt").write_text("a", encoding="utf-8")
    (self.root / "beta.md").write_text("b", encoding="utf-8")
    result = self.registry.execute("search_files", {"name": "*.txt"})
    self.assertTrue(result.ok)
    self.assertIn("alpha.txt", result.text)
    self.assertNotIn("beta.md", result.text)

  def test_search_skips_noisy_folders(self):
    (self.root / ".git").mkdir()
    (self.root / ".git" / "secret.py").write_text("x", encoding="utf-8")
    result = self.registry.execute("search_files", {"name": "secret.py"})
    self.assertTrue(result.ok)
    self.assertEqual(result.data["matches"], [])

  def test_rename_copy_move_delete(self):
    (self.root / "a.txt").write_text("a", encoding="utf-8")
    self.assertTrue(
        self.registry.execute(
            "rename_path", {"source": "a.txt", "target": "b.txt"}
        ).ok
    )
    self.assertTrue(
        self.registry.execute(
            "copy_path", {"source": "b.txt", "target": "c.txt"}
        ).ok
    )
    self.assertTrue(
        self.registry.execute(
            "move_path", {"source": "c.txt", "target": "d.txt"}
        ).ok
    )
    self.assertTrue((self.root / "b.txt").is_file())
    self.assertTrue((self.root / "d.txt").is_file())
    self.assertTrue(
        self.registry.execute("delete_path", {"path": "d.txt"}).ok
    )
    self.assertFalse((self.root / "d.txt").exists())

  def test_delete_folder(self):
    folder = self.root / "reports"
    folder.mkdir()
    (folder / "x.txt").write_text("x", encoding="utf-8")
    self.assertTrue(
        self.registry.execute("delete_path", {"path": "reports"}).ok
    )
    self.assertFalse(folder.exists())

  def test_traversal_is_refused(self):
    outside = self.root.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    self.addCleanup(lambda: outside.unlink(missing_ok=True))
    result = self.registry.execute("read_file", {"path": "../outside-secret.txt"})
    self.assertFalse(result.ok)
    self.assertIn("outside the allowed folder", result.text)

  def test_deleting_the_root_is_refused(self):
    result = self.registry.execute("delete_path", {"path": "."})
    self.assertFalse(result.ok)
    self.assertIn("will not delete", result.text)

  def test_missing_source_is_reported(self):
    result = self.registry.execute(
        "copy_path", {"source": "nope.txt", "target": "other.txt"}
    )
    self.assertFalse(result.ok)
    self.assertIn("does not exist", result.text)


class AppToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.registry = build_registry(make_settings(self.tmp.name))

  def test_url_validation(self):
    self.assertEqual(validate_url("example.com"), "https://example.com")
    self.assertEqual(
        validate_url("https://example.com/a?b=1"),
        "https://example.com/a?b=1",
    )
    for bad in ("", "not a url", "javascript:alert(1)", "ftp://example.com",
                "file:///c:/windows"):
      with self.assertRaises(ToolError, msg=bad):
        validate_url(bad)

  def test_open_url_uses_the_injected_opener(self):
    from tools.apps import open_url

    opened = []
    settings = make_settings(self.tmp.name)
    result = open_url(
        settings.tools, "example.com", opener=lambda url: opened.append(url)
    )
    self.assertTrue(result.ok)
    self.assertEqual(opened, ["https://example.com"])

  def test_disallowed_application(self):
    result = self.registry.execute(
        "open_app", {"application": "definitely-not-allowed"}
    )
    self.assertFalse(result.ok)
    self.assertIn("allowed application list", result.text)

  def test_disallowed_close(self):
    result = self.registry.execute(
        "close_app", {"application": "definitely-not-allowed"}
    )
    self.assertFalse(result.ok)
    self.assertIn("allowed application list", result.text)

  def test_command_arguments_reject_shell_characters(self):
    result = self.registry.execute(
        "run_command",
        {"command": "ping", "arguments": "127.0.0.1 & whoami"},
    )
    self.assertFalse(result.ok)
    self.assertIn("shell characters", result.text)

  def test_allowed_command_runs_without_a_shell(self):
    result = self.registry.execute("run_command", {"command": "hostname"})
    self.assertTrue(result.ok, result.text)
    self.assertTrue(result.data["output"])
    self.assertEqual(result.data["exit_code"], 0)


class DesktopToolTests(unittest.TestCase):
  def test_powershell_quoting(self):
    self.assertEqual(_quote("it's fine"), "'it''s fine'")
    self.assertEqual(_quote("plain"), "'plain'")

  def test_window_listing(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    registry = build_registry(make_settings(tmp.name))
    result = registry.execute("windows_list")
    self.assertTrue(result.ok, result.text)
    self.assertIsInstance(result.data["windows"], list)

  def test_window_action_needs_a_match(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    registry = build_registry(make_settings(tmp.name))
    result = registry.execute(
        "window_action",
        {"title": "zzz-no-such-window-zzz", "action": "focus"},
    )
    self.assertFalse(result.ok)
    self.assertIn("No window matching", result.text)


class IntentToolTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter(
        allowed_apps=("notepad",), allowed_commands=("ipconfig", "hostname")
    )

  def route(self, text):
    intent = self.router.route(text)
    self.assertIsNotNone(intent, text)
    return intent

  def test_open_application(self):
    intent = self.route("open notepad")
    self.assertEqual(intent.name, "open_app")
    self.assertEqual(intent.entities["application"], "notepad")

  def test_open_website(self):
    intent = self.route("open https://example.com")
    self.assertEqual(intent.name, "open_url")
    self.assertEqual(intent.entities["url"], "https://example.com")

  def test_open_file_path(self):
    intent = self.route("open notes.txt")
    self.assertEqual(intent.name, "open_path")
    self.assertEqual(intent.entities["path"], "notes.txt")

  def test_system_intents(self):
    cases = {
        "what is my cpu usage": "cpu",
        "how much ram is free": "memory",
        "how much disk space is left": "disk",
        "what is the battery level": "battery",
        "system status": "all",
    }
    for text, kind in cases.items():
      intent = self.route(text)
      self.assertEqual(intent.name, "system_info", text)
      self.assertEqual(intent.entities["kind"], kind, text)

  def test_processes_and_screenshot(self):
    self.assertEqual(self.route("list processes").name, "processes")
    self.assertEqual(self.route("take a screenshot").name, "screenshot")

  def test_notification(self):
    intent = self.route("notify me the build finished")
    self.assertEqual(intent.name, "notify")
    self.assertEqual(intent.entities["message"], "the build finished")

  def test_clipboard(self):
    copy = self.route("copy hello world to clipboard")
    self.assertEqual(copy.name, "clipboard_copy")
    self.assertEqual(copy.entities["text"], "hello world")
    self.assertEqual(self.route("what's on my clipboard").name, "clipboard_read")

  def test_file_intents(self):
    self.assertEqual(
        self.route("read file notes.txt").entities["path"], "notes.txt"
    )
    write = self.route("write hello to file notes.txt")
    self.assertEqual(write.name, "write_file")
    self.assertEqual(write.entities["content"], "hello")
    self.assertEqual(write.entities["path"], "notes.txt")
    self.assertEqual(
        self.route("delete file notes.txt").name, "delete_path"
    )
    search = self.route("search for *.py in src")
    self.assertEqual(search.entities["name"], "*.py")
    self.assertEqual(search.entities["path"], "src")
    self.assertEqual(
        self.route("list files in data").entities["path"], "data"
    )
    self.assertEqual(self.route("create folder reports").name, "create_folder")
    self.assertEqual(self.route("rename a.txt to b.txt").name, "rename_path")
    self.assertEqual(self.route("copy a.txt to b.txt").name, "copy_path")
    self.assertEqual(self.route("move a.txt to b.txt").name, "move_path")

  def test_compound_request_uses_the_first_action(self):
    intent = self.route(
        "create a folder named prabir's docs on the desktop & in that"
        " folder create two files"
    )
    self.assertEqual(intent.name, "create_folder")
    self.assertEqual(intent.entities["path"], "~/Desktop/prabir's docs")

  def test_folder_inside_a_parent_folder(self):
    intent = self.route("create a folder named reports in drafts")
    self.assertEqual(intent.name, "create_folder")
    self.assertEqual(intent.entities["path"], "drafts/reports")

  def test_plan_runs_several_file_actions(self):
    plan = plan_file_actions(
        "create a folder named drafts and in that folder write hello to"
        " file notes.txt and copy notes.txt to backup.txt"
    )
    self.assertEqual(
        [name for name, _arguments, _clause in plan],
        ["create_folder", "write_file", "copy_path"],
    )
    self.assertEqual(plan[0][1], {"path": "drafts"})
    self.assertEqual(
        plan[1][1], {"path": "drafts/notes.txt", "content": "hello"}
    )
    self.assertEqual(
        plan[2][1],
        {"source": "drafts/notes.txt", "target": "drafts/backup.txt"},
    )

  def test_plan_keeps_a_folder_prefix_with_its_action(self):
    plan = plan_file_actions(
        "create a folder named docs & in that folder, create two files"
        ' - "readme.md" & "main.py"'
    )
    self.assertEqual(
        [name for name, _arguments, _clause in plan],
        ["create_folder", "write_file", "write_file"],
    )
    self.assertEqual(plan[1][1]["path"], "docs/readme.md")
    self.assertEqual(plan[2][1]["path"], "docs/main.py")

  def test_second_folder_is_a_sibling_by_default(self):
    plan = plan_file_actions(
        "create a folder named a and create a folder named b"
    )
    self.assertEqual([step[1]["path"] for step in plan], ["a", "b"])

  def test_plan_expands_a_file_list(self):
    plan = plan_file_actions('create two files - "readme.md" & "main.py"')
    self.assertEqual(
        [step[1]["path"] for step in plan], ["readme.md", "main.py"]
    )

  def test_plan_stops_at_the_first_unclear_step(self):
    plan = plan_file_actions(
        "create a folder named drafts and write hello to file notes.txt"
        " and make me a sandwich"
    )
    self.assertEqual(len(plan), 2)
    # Later relative paths stay inside the folder just created.
    self.assertEqual(plan[1][1]["path"], "drafts/notes.txt")

  def test_single_action_is_not_a_plan(self):
    self.assertIsNone(plan_file_actions("create a folder named drafts"))

  def test_command_and_windows(self):
    run = self.route("run ipconfig")
    self.assertEqual(run.name, "run_command")
    self.assertEqual(run.entities["command"], "ipconfig")
    self.assertEqual(self.route("close notepad").name, "close_app")
    for text in (
        "close all apps",
        "close all programs",
        "close everything",
        "close all",
        "quit all applications",
        "shut down all windows",
    ):
      self.assertEqual(self.route(text).name, "close_all_apps", text)
    window = self.route("minimize notepad")
    self.assertEqual(window.name, "window_action")
    self.assertEqual(window.entities["action"], "minimize")
    self.assertEqual(self.route("what windows are open").name, "windows_list")

  def test_unrecognized_requests_fall_back(self):
    for text in (
        "open the pod bay doors",
        "explain how file permissions work",
        "read me a story",
        "close the deal",
        "run the numbers",
        "explain what a CPU cache does",
        "what is a processor",
    ):
      self.assertIsNone(self.router.route(text), text)


class AssistantToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)

  def make(self, replies=("Model answer.",), **overrides):
    options = {"root": str(self.root)}
    options.update(overrides)
    settings = dataclasses.replace(
        load_settings(), tools=ToolsConfig(**options)
    )
    client = FakeClient(replies)
    assistant = Assistant(settings, client=client)
    self.addCleanup(assistant.close)
    return assistant, client

  def test_system_info_is_answered_locally(self):
    assistant, client = self.make()
    reply = assistant.reply("what is my cpu usage")
    self.assertTrue(reply.startswith("CPU usage is"), reply)
    self.assertEqual(client.calls, [])

  def test_destructive_action_requires_confirmation(self):
    assistant, client = self.make()
    prompt = assistant.reply("write hello to file notes.txt")
    self.assertIn("confirm to continue", prompt)
    self.assertFalse((self.root / "notes.txt").exists())

    done = assistant.reply("yes")
    self.assertIn("Saved 5 characters", done)
    self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"),
                     "hello")
    self.assertEqual(client.calls, [])
    self.assertIn("Saved 5 characters", assistant.reply("/history"))

  def test_confirmation_is_short_and_names_the_folder(self):
    assistant, _ = self.make()
    prompt = assistant.reply(
        "create a folder named prabir's docs on the desktop & in that folder"
        " create two files"
    )
    self.assertIn('Create the folder "~/Desktop/prabir\'s docs"', prompt)
    self.assertIn("confirm to continue", prompt)
    self.assertLess(len(prompt), 110)

  def test_plan_asks_once_then_runs_every_step(self):
    assistant, client = self.make()
    prompt = assistant.reply(
        "create a folder named drafts and in that folder write hello to"
        " file notes.txt"
    )
    self.assertIn("confirm to continue", prompt)
    self.assertLess(len(prompt), 130)
    self.assertFalse((self.root / "drafts").exists())

    result = assistant.reply("yes")
    self.assertIn("Created the folder drafts", result)
    self.assertIn("Saved 5 characters", result)
    self.assertEqual(
        (self.root / "drafts" / "notes.txt").read_text(encoding="utf-8"),
        "hello",
    )
    self.assertEqual(client.calls, [])

  def test_plan_stops_at_the_first_failure(self):
    assistant, _ = self.make()
    prompt = assistant.reply(
        "delete file missing.txt and create a folder named drafts"
    )
    self.assertIn("confirm to continue", prompt)
    result = assistant.reply("yes")
    self.assertIn("Step 1 failed", result)
    self.assertFalse((self.root / "drafts").exists())

  def test_plan_can_be_cancelled(self):
    assistant, _ = self.make()
    assistant.reply("create a folder named a and create a folder named b")
    self.assertEqual(assistant.reply("no"), "Cancelled, sir.")
    self.assertFalse((self.root / "a").exists())

  def test_cancel_leaves_files_untouched(self):
    assistant, _ = self.make()
    assistant.reply("write hello to file notes.txt")
    assistant.reply("/confirm")
    prompt = assistant.reply("delete file notes.txt")
    self.assertIn("delete", prompt.lower())
    self.assertEqual(assistant.reply("no"), "Cancelled, sir.")
    self.assertTrue((self.root / "notes.txt").is_file())

  def test_confirmed_command_runs(self):
    assistant, _ = self.make()
    prompt = assistant.reply("run hostname")
    self.assertIn("confirm to continue", prompt)
    result = assistant.reply("/confirm")
    self.assertNotIn("confirm to continue", result)
    self.assertTrue(result)

  def test_read_file_uses_the_reader_tool(self):
    (self.root / "notes.txt").write_text("hello", encoding="utf-8")
    assistant, client = self.make()
    reply = assistant.reply("read file notes.txt")
    self.assertIn("notes.txt has 1 line", reply)
    self.assertEqual(client.calls, [])

  def test_tools_command_lists_permissions(self):
    assistant, _ = self.make()
    listing = assistant.reply("/tools")
    self.assertIn("write_file (confirm)", listing)
    self.assertIn("read_file (safe)", listing)

  def test_disabled_tools_fall_back_to_the_model(self):
    assistant, client = self.make(enabled=False)
    self.assertEqual(assistant.reply("what is my cpu usage"), "Model answer.")
    self.assertEqual(len(client.calls), 1)
    self.assertIn("disabled", assistant.reply("/tools"))

  def test_close_all_requires_confirmation(self):
    assistant, client = self.make()
    prompt = assistant.reply("close all apps")
    self.assertIn("confirm to continue", prompt)
    self.assertEqual(assistant.reply("/cancel"), "Cancelled, sir.")
    self.assertEqual(client.calls, [])

  def test_new_clears_a_pending_action(self):
    assistant, _ = self.make()
    assistant.reply("write hello to file notes.txt")
    assistant.reply("/new")
    self.assertIn("nothing waiting", assistant.reply("/confirm"))
    self.assertFalse((self.root / "notes.txt").exists())


class ClosePolicyTests(unittest.TestCase):
  WINDOWS = [
      {"hwnd": 1, "title": "Program Manager", "pid": 10},
      {"hwnd": 2, "title": "playlist - Notepad", "pid": 11},
      {"hwnd": 3, "title": "Inbox - Chrome", "pid": 12},
      {"hwnd": 4, "title": "New tab - Chrome", "pid": 12},
      {"hwnd": 5, "title": "jarvis - cmd", "pid": 13},
      {"hwnd": 6, "title": "Settings", "pid": 14},
      {"hwnd": 7, "title": "JARVIS", "pid": 15},
  ]
  NAMES = {
      10: "explorer",
      11: "notepad",
      12: "chrome",
      13: "cmd",
      14: "systemsettings",
      15: "python",
  }

  def test_only_open_apps_are_targeted(self):
    targets = select_close_targets(self.WINDOWS, self.NAMES, own_pids={15})
    self.assertEqual(
        {target["process"] for target in targets}, {"notepad", "chrome"}
    )

  def test_system_and_shell_processes_are_protected(self):
    targets = select_close_targets(self.WINDOWS, self.NAMES, own_pids=set())
    names = {target["process"] for target in targets}
    self.assertNotIn("explorer", names)
    self.assertNotIn("cmd", names)
    self.assertNotIn("systemsettings", names)
    self.assertNotIn("python", names)

  def test_one_window_per_application(self):
    targets = select_close_targets(self.WINDOWS, self.NAMES, own_pids=set())
    self.assertEqual(
        len([target for target in targets if target["process"] == "chrome"]),
        1,
    )

  def test_own_processes_are_never_targeted(self):
    targets = select_close_targets(self.WINDOWS, self.NAMES, own_pids={11, 12})
    self.assertEqual(targets, [])


class ToolsConfigTests(unittest.TestCase):
  def test_project_config_has_tools(self):
    tools = load_settings().tools
    self.assertTrue(tools.enabled)
    self.assertIn("notepad", tools.allowed_apps)
    self.assertTrue(tools.resolved_root.is_absolute())

  def test_invalid_search_limit_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[tools]\nsearch_limit = 0\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_invalid_app_list_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text('[tools]\nallowed_apps = [1, 2]\n', encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


if __name__ == "__main__":
  unittest.main()
