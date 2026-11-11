"""Feature set 4.11 M6 tests: time tracking and daily totals."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from core.assistant import Assistant
from core.config import ConfigError, TimeConfig, load_settings
from core.intents import IntentRouter
from core.time_tracking import TimeError, TimeService, format_report
from tools import build_registry


class ServiceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    self.service = TimeService(TimeConfig(db=str(self.base / "time.db")))
    self.addCleanup(self.service.close)

  def test_start_stop_and_daily_totals(self):
    start = datetime(2026, 11, 11, 10, 0)
    entry = self.service.start("writing", now=start)
    self.assertTrue(entry.active)
    self.assertEqual(self.service.active().label, "writing")
    with self.assertRaises(TimeError):
      self.service.start("other", now=start)

    stopped = self.service.stop(now=datetime(2026, 11, 11, 11, 30))
    self.assertFalse(stopped.active)
    report = self.service.report(
        date(2026, 11, 11), now=datetime(2026, 11, 11, 11, 30)
    )
    self.assertEqual(report.total_seconds, 5400)
    self.assertEqual(report.totals[0].label, "writing")
    self.assertIn("writing - 1h 30m", format_report(report))
    self.assertIsNone(self.service.active())
    with self.assertRaises(TimeError):
      self.service.stop(now=datetime(2026, 11, 11, 12, 0))

  def test_midnight_is_split_between_days(self):
    self.service.start("night work", now=datetime(2026, 11, 10, 23, 30))
    stop = datetime(2026, 11, 11, 0, 30)
    self.service.stop(now=stop)
    first = self.service.report(date(2026, 11, 10), now=stop)
    second = self.service.report(date(2026, 11, 11), now=stop)
    self.assertEqual(first.total_seconds, 1800)
    self.assertEqual(second.total_seconds, 1800)

  def test_active_entry_contributes_without_stopping(self):
    self.service.start("reading", now=datetime(2026, 11, 11, 9, 0))
    report = self.service.report(
        date(2026, 11, 11), now=datetime(2026, 11, 11, 10, 0)
    )
    self.assertEqual(report.total_seconds, 3600)
    self.assertIn("1h 00m", format_report(report))

  def test_list_delete_and_week(self):
    first = self.service.start("writing", now=datetime(2026, 11, 9, 9, 0))
    self.service.stop(now=datetime(2026, 11, 9, 10, 0))
    second = self.service.start("reading", now=datetime(2026, 11, 10, 9, 0))
    self.service.stop(now=datetime(2026, 11, 10, 9, 30))
    self.assertEqual([entry.id for entry in self.service.list()], [second.id, first.id])
    reports = self.service.week("2026-11-09", now=datetime(2026, 11, 11, 12, 0))
    self.assertEqual(len(reports), 7)
    self.assertEqual(sum(item.total_seconds for item in reports), 5400)
    removed = self.service.delete(first.id)
    self.assertEqual(removed.id, first.id)
    self.assertIsNone(self.service.get(first.id))

  def test_validation_and_limit(self):
    with self.assertRaises(TimeError):
      self.service.start("   ")
    self.service.start("one", now=datetime(2026, 11, 11, 9, 0))
    self.service.stop(now=datetime(2026, 11, 11, 10, 0))
    limited = TimeService(
        TimeConfig(db=str(self.base / "limited.db"), max_entries=1)
    )
    self.addCleanup(limited.close)
    limited.start("first", now=datetime(2026, 11, 11, 9, 0))
    with self.assertRaises(TimeError):
      limited.start("second", now=datetime(2026, 11, 11, 10, 0))
    with self.assertRaises(TimeError):
      self.service.report("someday")


class IntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_time_tracking_intents(self):
    cases = {
        "start tracking writing": "time_start",
        "start time tracking writing": "time_start",
        "stop tracking": "time_stop",
        "what am i tracking": "time_status",
        "how much time did i spend today": "time_report",
        "show my time totals": "time_report",
        "time week": "time_week",
        "list my time entries": "time_list",
        "remove time entry 2": "time_delete",
    }
    for text, name in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, name, text)

  def test_start_entities_and_commands(self):
    intent = self.router.route("start tracking writing")
    self.assertEqual(intent.entities["label"], "writing")
    self.assertEqual(self.router.route("/time").name, "time")


class AssistantTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)

  def make(self, enabled=True, tools=False):
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(base.conversation, enabled=False),
        memory=dataclasses.replace(
            base.memory, enabled=False, db=str(self.base / "memory.db")
        ),
        documents=dataclasses.replace(base.documents, enabled=False),
        tools=dataclasses.replace(
            base.tools, enabled=tools, root=str(self.base)
        ),
        weather=dataclasses.replace(base.weather, enabled=False),
        notes=dataclasses.replace(
            base.notes, enabled=False, db=str(self.base / "notes.db")
        ),
        habits=dataclasses.replace(
            base.habits, enabled=False, db=str(self.base / "habits.db")
        ),
        goals=dataclasses.replace(
            base.goals, enabled=False, db=str(self.base / "goals.db")
        ),
        time=dataclasses.replace(
            base.time,
            enabled=enabled,
            db=str(self.base / "time.db"),
        ),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    return assistant

  def test_commands_and_natural_language(self):
    assistant = self.make()
    self.assertIn("Started tracking writing", assistant.reply("start tracking writing"))
    self.assertIn("Currently tracking", assistant.reply("what am i tracking"))
    self.assertIn("Stopped tracking writing", assistant.reply("stop tracking"))
    self.assertIn("writing", assistant.reply("/time today"))
    self.assertIn("writing", assistant.reply("/time list"))
    self.assertIn("Total", assistant.reply("/time week"))
    self.assertIn("writing", assistant.reply("how much time did i spend today"))

  def test_natural_delete_uses_tool_and_can_be_cancelled(self):
    assistant = self.make(tools=True)
    assistant.reply("start tracking writing")
    assistant.reply("stop tracking")
    pending = assistant.reply("remove time entry 1")
    self.assertIn("Delete time entry #1", pending)
    self.assertIn("confirm", pending.lower())
    self.assertEqual(assistant._pending_tool[0], "time_delete")
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_time_tracking().get(1))

  def test_natural_delete_without_tools(self):
    assistant = self.make(tools=False)
    assistant.reply("start tracking writing")
    assistant.reply("stop tracking")
    self.assertIn("confirm", assistant.reply("remove time entry 1").lower())
    self.assertIn("removed", assistant.reply("yes").lower())

  def test_disabled_time_tracking(self):
    assistant = self.make(enabled=False)
    self.assertEqual(
        assistant.reply("/time"), "Time tracking is disabled, sir."
    )
    self.assertEqual(
        assistant.reply("start tracking writing"),
        "Time tracking is disabled, sir.",
    )


class TimeToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    settings = load_settings()
    self.settings = dataclasses.replace(
        settings,
        time=dataclasses.replace(
            settings.time, db=str(self.base / "time.db")
        ),
        tools=dataclasses.replace(settings.tools, root=str(self.base)),
    )
    self.service = TimeService(self.settings.time)
    self.addCleanup(self.service.close)
    self.registry = build_registry(
        self.settings, time_service=self.service
    )

  def test_tools_are_registered_with_permissions(self):
    names = set(self.registry.names())
    for name in {
        "time_start",
        "time_stop",
        "time_status",
        "time_report",
        "time_week",
        "time_list",
        "time_delete",
    }:
      self.assertIn(name, names)
    self.assertTrue(self.registry.requires_confirmation("time_delete"))
    self.assertFalse(self.registry.requires_confirmation("time_start"))

  def test_start_status_stop_report_and_delete_tools(self):
    started = self.registry.execute("time_start", {"label": "writing"})
    self.assertTrue(started.ok, started.text)
    entry_id = started.data["id"]

    status = self.registry.execute("time_status")
    self.assertTrue(status.ok, status.text)
    self.assertEqual(status.data["id"], entry_id)

    stopped = self.registry.execute("time_stop")
    self.assertTrue(stopped.ok, stopped.text)
    self.assertFalse(stopped.data["active"])

    report = self.registry.execute("time_report", {"day": "today"})
    self.assertTrue(report.ok, report.text)
    self.assertIn("writing", report.text)
    self.assertGreater(report.data["total_seconds"], 0)

    listed = self.registry.execute("time_list")
    self.assertTrue(listed.ok, listed.text)
    self.assertEqual(listed.data["count"], 1)

    week = self.registry.execute("time_week")
    self.assertTrue(week.ok, week.text)
    self.assertEqual(len(week.data["days"]), 7)

    deleted = self.registry.execute("time_delete", {"id": entry_id})
    self.assertTrue(deleted.ok, deleted.text)
    self.assertIsNone(self.service.get(entry_id))

  def test_tool_validation(self):
    missing = self.registry.execute("time_start", {})
    self.assertFalse(missing.ok)
    self.assertIn("missing argument", missing.text)

    bad_type = self.registry.execute("time_delete", {"id": "one"})
    self.assertFalse(bad_type.ok)
    self.assertIn("must be int", bad_type.text)

    empty = self.registry.execute("time_start", {"label": "   "})
    self.assertFalse(empty.ok)
    self.assertIn("needs a label", empty.text)


class ConfigTests(unittest.TestCase):
  def test_invalid_time_limit_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[time]\nmax_entries = 0\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


class NoModelClient:
  """Fail if a deterministic time request reaches the model."""

  def chat(self, *args, **kwargs):
    raise AssertionError("the model must not be called")


if __name__ == "__main__":
  unittest.main()
