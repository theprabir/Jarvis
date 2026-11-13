"""Feature set 4.20 tests: reliability, telemetry and self-monitoring."""

from __future__ import annotations

import dataclasses
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.assistant import Assistant
from core.config import load_settings
from core.intents import IntentRouter
from core.reliability import ReliabilityError, ReliabilityService


class FakeClient:
  def chat(self, messages, max_tokens=None, temperature=None):
    return SimpleNamespace(
        content="Model answer.",
        total_ms=1.0,
        completion_tokens=1,
        generated_tokens_per_second=1.0,
        finish_reason="stop",
    )

  def close(self):
    pass


class FakeTasks:
  def list(self, limit=100):
    return [
        SimpleNamespace(
            status="completed",
            steps=[SimpleNamespace(status="completed"),
                   SimpleNamespace(status="completed")],
        ),
        SimpleNamespace(
            status="running",
            steps=[SimpleNamespace(status="completed"),
                   SimpleNamespace(status="pending")],
        ),
    ]


class ReliabilityServiceTests(unittest.TestCase):
  def setUp(self):
    self.root = Path(tempfile.mkdtemp(prefix="jarvis-reliability-"))
    self.addCleanup(shutil.rmtree, self.root, True)
    base = load_settings()
    self.config = dataclasses.replace(
        base.reliability,
        db=str(self.root / "reliability.db"),
        config_path=str(self.root / "settings.toml"),
        offline=False,
        privacy=False,
        explain=False,
    )
    self.config_path = Path(self.config.config_path)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "config" / "settings.toml",
        self.config_path,
    )
    self.service = ReliabilityService(
        self.config,
        task_provider=FakeTasks(),
        capabilities=lambda: {"groups": {}, "tools": ["write_file"]},
    )
    self.addCleanup(self.service.close)

  def test_requests_latency_misunderstandings_and_tool_metrics(self):
    self.service.record_request(
        "make it faster", intent="", role="main", status="ok", latency_ms=12,
        misunderstood=True,
    )
    self.service.record_request(
        "what time is it", intent="time", role="main", status="ok",
        latency_ms=4,
    )
    self.service.record_request(
        "broken", intent="time", role="main", status="error", latency_ms=30,
        error="boom",
    )
    self.service.record_tool("write_file", True, 8)
    self.service.record_tool("write_file", False, 22, "denied")
    stats = self.service.stats()
    self.assertEqual(stats["requests"]["total"], 3)
    self.assertEqual(stats["requests"]["failed"], 1)
    self.assertEqual(stats["requests"]["misunderstood"], 1)
    self.assertEqual(stats["requests"]["average_latency_ms"], 15.3)
    self.assertEqual(stats["tools"][0]["name"], "write_file")
    self.assertEqual(stats["tools"][0]["failed"], 1)
    self.assertEqual(stats["tasks"]["completed_steps"], 3)
    self.assertEqual(stats["tasks"]["total_steps"], 4)
    self.assertEqual(stats["tasks"]["step_rate"], 75.0)
    self.assertEqual(stats["misunderstood"][0]["text"], "make it faster")
    self.assertIn("rss_mb", stats["memory"])

  def test_telemetry_is_bounded(self):
    config = dataclasses.replace(self.config, max_requests=3, max_tool_events=2)
    service = ReliabilityService(config)
    self.addCleanup(service.close)
    for index in range(6):
      service.record_request(f"request {index}", latency_ms=index)
      service.record_tool("tool", True, index)
    self.assertEqual(service.stats()["requests"]["total"], 3)
    self.assertEqual(sum(row["total"] for row in service.stats()["tools"]), 2)

  def test_requests_persist_and_privacy_can_omit_text(self):
    self.service.record_request("private words", latency_ms=2)
    self.assertEqual(self.service.recent_requests(1)[0]["text"], "private words")
    self.service.close()
    reopened = ReliabilityService(self.config)
    self.addCleanup(reopened.close)
    self.assertEqual(reopened.recent_requests(1)[0]["text"], "private words")

  def test_modes_are_persisted(self):
    self.assertFalse(self.service.mode("offline"))
    self.service.set_mode("offline", True)
    self.service.set_mode("privacy", True)
    self.assertTrue(self.service.mode("offline"))
    self.service.close()
    reopened = ReliabilityService(self.config)
    self.addCleanup(reopened.close)
    self.assertTrue(reopened.mode("offline"))
    self.assertTrue(reopened.mode("privacy"))
    self.assertFalse(reopened.mode("explain"))
    with self.assertRaises(ReliabilityError):
      reopened.set_mode("unknown", True)

  def test_configuration_snapshots_and_rollback_require_confirmation(self):
    first = self.service.snapshot_config("baseline")
    original = self.config_path.read_text(encoding="utf-8")
    self.config_path.write_text(original + "\n# changed\n", encoding="utf-8")
    with self.assertRaises(ReliabilityError):
      self.service.rollback_config(first["id"])
    restored = self.service.rollback_config(first["id"], confirm=True)
    self.assertEqual(restored["id"], first["id"])
    self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)
    self.assertGreaterEqual(len(self.service.config_versions()), 2)

  def test_plugin_registration_is_explicit_and_bounded(self):
    calls = []
    plugin = self.service.register_plugin(
        "sample_plugin", lambda: calls.append(True), "sample", "safe"
    )
    self.assertEqual(plugin.name, "sample_plugin")
    self.assertEqual(self.service.plugins()[0].description, "sample")
    with self.assertRaises(ReliabilityError):
      self.service.register_plugin("sample_plugin", lambda: None)
    with self.assertRaises(ReliabilityError):
      self.service.register_plugin("Bad Name", lambda: None)
    with self.assertRaises(ReliabilityError):
      self.service.register_plugin("other_plugin", "not callable")
    with self.assertRaises(ReliabilityError):
      self.service.register_plugin("other_plugin", lambda: None, permission="root")

  def test_reversible_file_action_and_unsafe_paths(self):
    from tools.base import ToolResult

    folder = self.root / "made"
    folder.mkdir()
    result = ToolResult(True, "made", {"path": str(folder)})
    self.service.record_undo("create_folder", {"path": str(folder)}, result)
    self.assertEqual(self.service.undo_last(), f"remove folder {folder}")
    self.assertFalse(folder.exists())
    with self.assertRaises(ReliabilityError):
      self.service.undo_last()

  def test_capability_provider_is_available(self):
    self.assertEqual(self.service.capabilities()["tools"], ["write_file"])


class ReliabilityAssistantTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    root = Path(self.tmp.name)
    base = load_settings()
    self.config_path = root / "settings.toml"
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "config" / "settings.toml",
        self.config_path,
    )
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(
            base.conversation, enabled=False, history_db=str(root / "history.db")
        ),
        memory=dataclasses.replace(base.memory, enabled=False),
        web=dataclasses.replace(base.web, enabled=False),
        weather=dataclasses.replace(base.weather, enabled=False),
        documents=dataclasses.replace(base.documents, enabled=False),
        tools=dataclasses.replace(base.tools, root=str(root)),
        tasks=dataclasses.replace(base.tasks, enabled=False),
        knowledge=dataclasses.replace(base.knowledge, enabled=False),
        productivity=dataclasses.replace(
            base.productivity, db=str(root / "productivity.db")
        ),
        calendar=dataclasses.replace(base.calendar, db=str(root / "calendar.db")),
        goals=dataclasses.replace(base.goals, enabled=False),
        proactive=dataclasses.replace(base.proactive),
        reliability=dataclasses.replace(
            base.reliability,
            db=str(root / "reliability.db"),
            config_path=str(self.config_path),
            offline=False,
            privacy=False,
            explain=False,
        ),
    )
    self.assistant = Assistant(settings, client=FakeClient())
    self.addCleanup(self.assistant.close)

  def test_capability_commands_and_natural_intent(self):
    self.assertIn("/reliability", self.assistant.reply("/help"))
    text = self.assistant.reply("/capabilities")
    self.assertIn("computer tools", text)
    self.assertIn("assistant", text)
    natural = self.assistant.reply("what can you do")
    self.assertIn("capabilities", natural)
    self.assertEqual(IntentRouter().route("what can you do").name, "capabilities")

  def test_status_records_requests_and_tool_failures(self):
    self.assistant.reply("hello")
    self.assistant.registry.execute("missing_tool", {})
    status = self.assistant.reply("/reliability status")
    self.assertIn("requests:", status)
    self.assertIn("failed", status)
    tools = self.assistant.reply("/reliability tools")
    self.assertIn("missing_tool", tools)
    errors = self.assistant.reply("/reliability errors")
    self.assertIn("tool:missing_tool", errors)

  def test_offline_and_privacy_modes(self):
    offline = self.assistant.reply("/offline on")
    self.assertIn("Offline mode is on", offline)
    web = self.assistant.reply("/web local question")
    self.assertIn("Offline mode is on", web)
    self.assistant.reply("/offline off")
    self.assertEqual(self.assistant.reply("/offline status"), "Offline mode is off, sir.")

    self.assistant.reply("/privacy on")
    self.assistant.reply("private conversation text")
    rows = self.assistant._get_reliability().recent_requests(1)
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["text"], "")
    self.assistant.reply("/privacy off")

  def test_explain_mode_adds_a_local_explanation(self):
    self.assistant.reply("/explain on")
    answer = self.assistant.reply("hello")
    self.assertIn("[explain]", answer)
    self.assertIn("intent=", answer)
    self.assistant.reply("/explain off")

  def test_undo_command_reverts_created_folder(self):
    result = self.assistant.registry.execute(
        "create_folder", {"path": "undo-command-folder"}
    )
    self.assertTrue(result.ok, result.error)
    folder = Path(self.assistant._settings.tools.root) / "undo-command-folder"
    self.assertTrue(folder.exists())
    answer = self.assistant.reply("/undo")
    self.assertIn("Undone", answer)
    self.assertFalse(folder.exists())

  def test_undo_command_reverts_a_relative_rename(self):
    root = Path(self.assistant._settings.tools.root)
    (root / "rename-source").mkdir()
    result = self.assistant.registry.execute(
        "rename_path",
        {"source": "rename-source", "target": "rename-target"},
    )
    self.assertTrue(result.ok, result.error)
    self.assertTrue((root / "rename-target").exists())
    self.assertIn("Undone", self.assistant.reply("/undo"))
    self.assertTrue((root / "rename-source").exists())
    self.assertFalse((root / "rename-target").exists())

  def test_reliability_config_commands(self):
    listed = self.assistant.reply("/reliability config list")
    self.assertIn("Configuration versions", listed)
    saved = self.assistant.reply("/reliability config save test")
    self.assertIn("saved", saved)
    self.assertIn("#", saved)

  def test_database_is_created_without_global_side_effects(self):
    self.assistant.reply("/reliability status")
    path = Path(self.assistant._settings.reliability.db)
    self.assertTrue(path.exists())
    db = sqlite3.connect(path)
    try:
      names = {
          row[0]
          for row in db.execute(
              "SELECT name FROM sqlite_master WHERE type='table'"
          )
      }
    finally:
      db.close()
    self.assertIn("reliability_requests", names)
    self.assertIn("reliability_config_versions", names)


if __name__ == "__main__":
  unittest.main()
