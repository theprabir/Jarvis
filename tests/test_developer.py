"""Phase 4.12 D1 tests: source explanation and project inspection."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from core.assistant import Assistant
from core.config import DeveloperConfig, load_settings
from core.developer import DeveloperError, DeveloperService
from core.intents import IntentRouter
from tools import build_registry


class NoModelClient:
  def chat(self, *args, **kwargs):
    raise AssertionError("the model must not be called")


class DeveloperTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    (self.root / "pkg").mkdir()
    (self.root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (self.root / "pkg" / "app.py").write_text(
        '"""Small demo module."""\n'
        "import os\n\n"
        "class Greeter:\n"
        "    def greet(self):\n"
        "        return os.name\n\n"
        "def main():\n"
        "    return Greeter().greet()\n",
        encoding="utf-8",
    )
    (self.root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (self.root / "requirements.txt").write_text("", encoding="utf-8")
    (self.root / "tests").mkdir()
    (self.root / "tests" / "test_app.py").write_text(
        "def test_main():\n    assert True\n", encoding="utf-8"
    )
    (self.root / "tests" / "test_ok.py").write_text(
        "import unittest\n\n"
        "class TestOk(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    (self.root / "script.py").write_text(
        'print("script-ok")\n', encoding="utf-8"
    )
    self.settings = load_settings()
    self.tools_config = dataclasses.replace(
        self.settings.tools, root=str(self.root)
    )
    self.service = DeveloperService(
        DeveloperConfig(max_files=100, max_depth=5), self.tools_config
    )

  def test_explains_python_source(self):
    text, data = self.service.explain_source("pkg/app.py")
    self.assertIn("Python", text)
    self.assertIn("Class Greeter", text)
    self.assertIn("main", text)
    self.assertIn("os", data["imports"])
    self.assertEqual(data["functions"], ["main"])

  def test_inspects_project(self):
    text, data = self.service.inspect_project(".")
    self.assertIn("Project inspection", text)
    self.assertIn("pkg/", text)
    self.assertIn("README.md", text)
    self.assertEqual(data["python_files"], 5)
    self.assertGreaterEqual(data["test_files"], 1)

  def test_refuses_paths_outside_root(self):
    with self.assertRaises(DeveloperError):
      self.service.explain_source("../outside.py")
    with self.assertRaises(DeveloperError):
      self.service.inspect_project("..")

  def test_intents_and_tools(self):
    router = IntentRouter()
    self.assertEqual(
        router.route("explain source pkg/app.py").name, "developer_explain"
    )
    self.assertEqual(
        router.route("inspect project").name, "developer_inspect"
    )
    self.assertEqual(router.route("/dev").name, "dev")
    self.assertEqual(router.route("/developer").name, "developer")
    self.assertEqual(
        router.route("search code for main").name, "developer_search"
    )
    self.assertEqual(
        router.route("find class Greeter").name, "developer_find"
    )
    self.assertEqual(
        router.route("explain project architecture").name,
        "developer_architecture",
    )
    self.assertEqual(router.route("run tests").name, "developer_tests")
    self.assertEqual(
        router.route("debug error KeyError: token").name, "developer_debug"
    )

    registry = build_registry(
        self.settings, developer_service=self.service
    )
    self.assertIn("developer_explain", registry.names())
    self.assertIn("developer_inspect", registry.names())
    for name in (
        "developer_search",
        "developer_find",
        "developer_architecture",
        "developer_snippet",
        "developer_debug",
        "developer_terminal",
        "developer_failures",
        "developer_format",
        "developer_docs",
        "developer_readme",
        "developer_tests",
        "developer_git_status",
        "developer_git_log",
        "developer_git_changes",
        "developer_commit_message",
        "developer_search_docs",
        "developer_template",
        "developer_run_script",
    ):
      self.assertIn(name, registry.names())
    result = registry.execute("developer_explain", {"path": "pkg/app.py"})
    self.assertTrue(result.ok, result.text)
    self.assertIn("Class Greeter", result.text)
    result = registry.execute("developer_inspect", {"path": "."})
    self.assertTrue(result.ok, result.text)
    self.assertIn("Project inspection", result.text)

  def test_search_find_and_architecture(self):
    text, data = self.service.search_code("Greeter", ".")
    self.assertIn("pkg/app.py", text)
    self.assertTrue(data["results"])
    text, data = self.service.find_symbol("Greeter", ".")
    self.assertIn("class Greeter", text)
    self.assertEqual(data["results"][0]["line"], 4)
    text, data = self.service.explain_architecture(".")
    self.assertIn("Project architecture", text)
    self.assertIn("pkg/app.py", data["modules"])

  def test_snippets_and_diagnostics(self):
    text, data = self.service.generate_snippet("python", "add numbers")
    self.assertIn("def add_numbers", data["code"])
    text, data = self.service.debug_error("KeyError: 'token'")
    self.assertIn("dictionary key", text)
    self.assertEqual(data["exception"], "KeyError")
    text, data = self.service.interpret_test_failure(
        "Ran 2 tests\nFAILED (failures=1)\nFAIL: test_x (tests.test_x)\n"
        "AssertionError: nope"
    )
    self.assertTrue(data["failed"])
    self.assertIn("test_x", text)
    self.assertIn(
        "dictionary key",
        self.service.explain_terminal_error("KeyError: 'token'")[0],
    )

  def test_docs_readme_and_format(self):
    text, data = self.service.generate_documentation("pkg", "API.md", write=True)
    self.assertIn("API documentation", text)
    self.assertTrue((self.root / "pkg" / "API.md").is_file())
    self.assertEqual(data["symbols"], 2)
    text, data = self.service.generate_readme(".", write=True)
    self.assertTrue((self.root / "README.generated.md").is_file())
    self.assertTrue(data["written"])
    (self.root / "format_me.py").write_text("x = 1  \n", encoding="utf-8")
    text, data = self.service.format_source("format_me.py", write=True)
    self.assertTrue(data["changed"])
    self.assertEqual((self.root / "format_me.py").read_text(), "x = 1\n")

  def test_tests_script_and_template(self):
    text, data = self.service.run_tests(".")
    self.assertEqual(data["returncode"], 0, text)
    text, data = self.service.run_script("script.py")
    self.assertEqual(data["returncode"], 0, text)
    self.assertIn("script-ok", text)
    text, data = self.service.create_template("python-cli", "demo", "templates")
    self.assertIn("Created", text)
    self.assertTrue((self.root / "templates" / "demo" / "main.py").is_file())

  def test_documentation_search_and_git(self):
    (self.root / "docs.md").write_text("# API\nUse the Greeter class.\n")
    text, data = self.service.search_documentation("Greeter")
    self.assertIn("docs.md", text)
    self.assertTrue(data["results"])

  def test_assistant_commands(self):
    settings = dataclasses.replace(
        self.settings,
        conversation=dataclasses.replace(
            self.settings.conversation, enabled=False
        ),
        memory=dataclasses.replace(
            self.settings.memory,
            enabled=False,
            db=str(self.root / "memory.db"),
        ),
        documents=dataclasses.replace(self.settings.documents, enabled=False),
        tools=dataclasses.replace(
            self.settings.tools, enabled=False, root=str(self.root)
        ),
        weather=dataclasses.replace(self.settings.weather, enabled=False),
        notes=dataclasses.replace(self.settings.notes, enabled=False),
        habits=dataclasses.replace(self.settings.habits, enabled=False),
        goals=dataclasses.replace(self.settings.goals, enabled=False),
        time=dataclasses.replace(self.settings.time, enabled=False),
        calendar=dataclasses.replace(self.settings.calendar, enabled=False),
        email=dataclasses.replace(self.settings.email, enabled=False),
        clipboard=dataclasses.replace(self.settings.clipboard, enabled=False),
        pomodoro=dataclasses.replace(self.settings.pomodoro, enabled=False),
        developer=DeveloperConfig(
            max_files=100, max_depth=5, max_read_bytes=100_000
        ),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    self.assertIn("Class Greeter", assistant.reply("/dev explain pkg/app.py"))
    self.assertIn("Project inspection", assistant.reply("/dev inspect ."))
    self.assertIn("pkg/app.py", assistant.reply("/dev search Greeter"))
    self.assertIn("class Greeter", assistant.reply("/dev find Greeter"))
    self.assertIn("Generated python snippet", assistant.reply(
        "/dev snippet python | add numbers"
    ))
    self.assertIn("dictionary key", assistant.reply("/dev debug KeyError: token"))
    self.assertIn(
        "Class Greeter",
        assistant.reply("explain source pkg/app.py"),
    )


if __name__ == "__main__":
  unittest.main()
