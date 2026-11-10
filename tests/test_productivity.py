"""Feature set 4.11 M1 tests: productivity to-dos and deadline reminders.

The suite is offline: timers are polled manually and the assistant uses a
client that fails if a deterministic to-do request ever reaches the model.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.assistant import Assistant
from core.config import ConfigError, ProductivityConfig, load_settings
from core.intents import IntentRouter
from core.productivity import (
    ProductivityError,
    ProductivityService,
    format_todo,
    parse_todo_input,
    parse_when,
)
from core.timers import TimerService


class ParseTests(unittest.TestCase):
  NOW = datetime(2026, 11, 10, 12, 0)

  def test_parse_add_fields(self):
    parsed = parse_todo_input(
        "to call mom tomorrow at 5pm priority high", self.NOW
    )
    self.assertEqual(parsed["title"], "call mom")
    self.assertEqual(parsed["priority"], "high")
    self.assertEqual(parsed["due_at"], datetime(2026, 11, 11, 17, 0))
    self.assertIsNone(parsed["remind_at"])

  def test_parse_due_and_recurrence(self):
    parsed = parse_todo_input(
        "pay rent due Friday repeat monthly", self.NOW
    )
    self.assertEqual(parsed["title"], "pay rent")
    self.assertEqual(parsed["recurrence"], "monthly")
    self.assertEqual(parsed["due_at"].hour, 9)

  def test_parse_relative_reminder(self):
    parsed = parse_todo_input("stretch remind me in 20 minutes", self.NOW)
    self.assertEqual(parsed["title"], "stretch")
    self.assertEqual(
        parsed["remind_at"], self.NOW + timedelta(minutes=20)
    )

  def test_parse_when(self):
    self.assertEqual(
        parse_when("tomorrow at 9", self.NOW),
        datetime(2026, 11, 11, 9, 0),
    )
    self.assertEqual(
        parse_when("tomorrow", self.NOW),
        datetime(2026, 11, 11, 9, 0),
    )
    self.assertEqual(
        parse_when("tonight", self.NOW),
        datetime(2026, 11, 10, 20, 0),
    )
    self.assertEqual(
        parse_when("in 2 hours", self.NOW),
        datetime(2026, 11, 10, 14, 0),
    )
    self.assertIsNone(parse_when("sometime soon", self.NOW))

  def test_empty_input_is_not_a_todo(self):
    self.assertEqual(parse_todo_input("", self.NOW)["title"], "")


class ServiceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    self.timers = TimerService(self.base / "timers.db", autostart=False)
    self.addCleanup(self.timers.close)
    self.service = ProductivityService(
        ProductivityConfig(db=str(self.base / "productivity.db")),
        timers=self.timers,
    )
    self.addCleanup(self.service.close)

  def test_crud_and_persistence(self):
    due = datetime.now().replace(microsecond=0) + timedelta(days=1)
    item = self.service.add(
        "write the report",
        details="Use the latest figures",
        priority="high",
        due_at=due,
    )
    self.assertEqual(item.status, "open")
    self.assertEqual(item.priority, "high")
    self.assertIn("write the report", format_todo(item))
    self.assertEqual(self.service.get(item.id).id, item.id)

    item = self.service.update(item.id, title="write final report")
    self.assertEqual(item.title, "write final report")
    self.assertEqual(self.service.list()[0].title, "write final report")

    done, next_item = self.service.complete(item.id)
    self.assertEqual(done.status, "done")
    self.assertIsNone(next_item)
    self.assertEqual(self.service.list(), [])
    self.assertEqual(self.service.list(status=None)[0].status, "done")

  def test_reminder_is_scheduled_and_cancelled(self):
    due = datetime.now().replace(microsecond=0) + timedelta(hours=1)
    item = self.service.add("call the dentist", due_at=due)
    pending = self.timers.list()
    self.assertEqual(len(pending), 1)
    self.assertEqual(pending[0].source, "todo")
    self.assertEqual(pending[0].source_id, item.id)

    self.service.complete(item.id)
    self.assertEqual(self.timers.list(), [])

  def test_reminder_fires_with_todo_prefix(self):
    due = datetime.now().replace(microsecond=0) + timedelta(hours=1)
    self.service.add("take medicine", due_at=due)
    messages = self.timers.poll(due + timedelta(seconds=1))
    self.assertEqual(messages, ["To-do: take medicine."])
    self.assertEqual(self.timers.poll(due + timedelta(seconds=2)), [])

  def test_snooze_reschedules(self):
    due = datetime.now().replace(microsecond=0) + timedelta(hours=2)
    item = self.service.add("review invoices", due_at=due)
    item = self.service.snooze(item.id, 15)
    self.assertAlmostEqual(
        (item.remind_at - datetime.now()).total_seconds(),
        15 * 60,
        delta=5,
    )
    self.assertEqual(len(self.timers.list()), 1)

  def test_recurring_completion_creates_next_occurrence(self):
    due = datetime.now().replace(second=0, microsecond=0) + timedelta(
        days=1
    )
    item = self.service.add(
        "water plants", due_at=due, recurrence="daily"
    )
    done, next_item = self.service.complete(item.id)
    self.assertEqual(done.status, "done")
    self.assertIsNotNone(next_item)
    self.assertEqual(next_item.title, "water plants")
    self.assertEqual(next_item.due_at, due + timedelta(days=1))
    self.assertEqual(next_item.parent_id, item.id)
    self.assertEqual(next_item.status, "open")
    self.assertEqual(len(self.service.list()), 1)

  def test_recurrence_requires_due_date(self):
    with self.assertRaises(ProductivityError):
      self.service.add("daily stretch", recurrence="daily")

  def test_reminder_survives_restart(self):
    due = datetime.now().replace(microsecond=0) + timedelta(hours=1)
    item = self.service.add("renew the licence", due_at=due)
    self.service.close()
    self.timers.close()

    timers = TimerService(self.base / "timers.db", autostart=False)
    self.addCleanup(timers.close)
    service = ProductivityService(
        ProductivityConfig(db=str(self.base / "productivity.db")),
        timers=timers,
    )
    self.addCleanup(service.close)
    self.assertEqual(service.get(item.id).title, "renew the licence")
    self.assertEqual(timers.list()[0].source, "todo")
    self.assertEqual(
        timers.poll(due + timedelta(seconds=1)),
        ["To-do: renew the licence."],
    )

  def test_missed_todo_reminder_is_reported(self):
    due = datetime.now().replace(microsecond=0) + timedelta(hours=1)
    item = self.service.add("overdue reminder", due_at=due)
    self.service.close()
    self.timers.close()

    past = (datetime.now() - timedelta(minutes=5)).isoformat(
        timespec="seconds"
    )
    db = sqlite3.connect(str(self.base / "timers.db"))
    db.execute(
        "UPDATE reminders SET due_at = ? WHERE source = 'todo'"
        " AND source_id = ?",
        (past, item.id),
    )
    db.commit()
    db.close()

    timers = TimerService(self.base / "timers.db", autostart=False)
    self.addCleanup(timers.close)
    self.assertEqual(
        timers.poll(), ["To-do: overdue reminder."]
    )


class MigrationTests(unittest.TestCase):
  def test_old_reminder_table_gets_source_columns(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "old.db"
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE reminders (id INTEGER PRIMARY KEY, text TEXT,"
        " due_at TEXT, created_at TEXT, fired INTEGER)"
    )
    due = (datetime.now() + timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    db.execute(
        "INSERT INTO reminders (text, due_at, created_at, fired)"
        " VALUES ('old reminder', ?, ?, 0)",
        (due, datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    db.close()

    timers = TimerService(path, autostart=False)
    self.addCleanup(timers.close)
    columns = {
        row[1]
        for row in timers._db.execute(
            "PRAGMA table_info(reminders)"
        ).fetchall()
    }
    self.assertIn("source", columns)
    self.assertIn("source_id", columns)
    self.assertEqual(timers.list()[0].label, "old reminder")


class IntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_todo_intents(self):
    cases = {
        "add a to-do to buy milk": "todo_add",
        "todo: call the bank": "todo_add",
        "list my to-dos": "todo_list",
        "what's on my todo list": "todo_list",
        "show todo 2": "todo_show",
        "mark todo 2 done": "todo_done",
        "complete todo 2": "todo_done",
        "remove todo 2": "todo_delete",
        "snooze todo 2 for 10 minutes": "todo_snooze",
    }
    for text, name in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, name, text)

  def test_todo_fields_are_extracted(self):
    intent = self.router.route(
        "add a to-do to call mom tomorrow at 5pm priority high"
    )
    self.assertEqual(intent.entities["title"], "call mom")
    self.assertEqual(intent.entities["priority"], "high")
    self.assertTrue(intent.entities["due_at"].startswith("2026-"))

  def test_task_controls_remain_separate(self):
    intent = self.router.route("list tasks")
    self.assertEqual(intent.name, "task_list")
    self.assertEqual(self.router.route("/todo").name, "todo")


class AssistantTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)

  def make(self, enabled=True):
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(base.conversation, enabled=False),
        memory=dataclasses.replace(
            base.memory, enabled=False, db=str(self.base / "memory.db")
        ),
        documents=dataclasses.replace(base.documents, enabled=False),
        tools=dataclasses.replace(base.tools, enabled=False),
        weather=dataclasses.replace(base.weather, enabled=False),
        productivity=dataclasses.replace(
            base.productivity,
            enabled=enabled,
            db=str(self.base / "productivity.db"),
        ),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    return assistant

  def test_commands_and_natural_language(self):
    assistant = self.make()
    answer = assistant.reply(
        "/todo add call mom due tomorrow priority high"
    )
    self.assertIn("To-do #1", answer)
    self.assertIn("call mom", answer)
    self.assertIn("call mom", assistant.reply("/todo list"))
    self.assertIn("completed", assistant.reply("mark todo 1 done"))
    self.assertIn("done", assistant.reply("/todo all"))

  def test_natural_add_and_show(self):
    assistant = self.make()
    self.assertIn("buy milk", assistant.reply("add a to-do to buy milk"))
    detail = assistant.reply("show todo 1")
    self.assertIn("buy milk", detail)
    self.assertIn("Status: open", detail)

  def test_snooze_command(self):
    assistant = self.make()
    assistant.reply("/todo add call the bank due tomorrow")
    answer = assistant.reply("/todo snooze 1 10 minutes")
    self.assertIn("snoozed", answer.lower())

  def test_recurring_task_rolls_forward(self):
    assistant = self.make()
    assistant.reply(
        "/todo add water plants due tomorrow repeat daily"
    )
    answer = assistant.reply("/todo done 1")
    self.assertIn("Next occurrence", answer)
    self.assertIn("water plants", assistant.reply("/todo list"))

  def test_disabled_productivity(self):
    assistant = self.make(enabled=False)
    self.assertEqual(
        assistant.reply("/todo list"), "Productivity is disabled, sir."
    )


class NoModelClient:
  """Fail loudly if a deterministic request reaches the model."""

  def __init__(self):
    self.calls = 0

  def chat(self, *args, **kwargs):
    self.calls += 1
    raise AssertionError("the model must not be called")


class ConfigTests(unittest.TestCase):
  def test_invalid_default_priority_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text(
          "[productivity]\ndefault_priority = \"urgent\"\n",
          encoding="utf-8",
      )
      with self.assertRaises(ConfigError):
        load_settings(path)


if __name__ == "__main__":
  unittest.main()
