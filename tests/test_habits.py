"""Feature set 4.11 M4 tests: daily habits, streaks and reminders."""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from core.assistant import Assistant
from core.config import ConfigError, HabitsConfig, load_settings
from core.habits import (
    HabitError,
    HabitService,
    format_habit,
    format_habit_detail,
    parse_habit_input,
)
from core.intents import IntentRouter
from core.timers import TimerService
from tools import build_registry


class ParseTests(unittest.TestCase):
  def test_daily_habit_and_reminder_phrases(self):
    parsed = parse_habit_input("read every day at 9pm")
    self.assertEqual(parsed["title"], "read")
    self.assertEqual(parsed["reminder_time"], "21:00")

    parsed = parse_habit_input(
        "exercise daily with a reminder at 07:30"
    )
    self.assertEqual(parsed["title"], "exercise")
    self.assertEqual(parsed["reminder_time"], "07:30")

    parsed = parse_habit_input("drink water every morning")
    self.assertEqual(parsed["title"], "drink water")
    self.assertEqual(parsed["reminder_time"], "09:00")

    parsed = parse_habit_input("stretch")
    self.assertEqual(parsed["title"], "stretch")
    self.assertEqual(parsed["reminder_time"], "")

  def test_titles_are_cleaned(self):
    self.assertEqual(
        parse_habit_input("to meditate as a habit")["title"], "meditate"
    )
    self.assertEqual(
        parse_habit_input("called journal daily")["title"], "journal"
    )
    self.assertEqual(parse_habit_input("")["title"], "")


class ServiceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    self.timers = TimerService(self.base / "timers.db", autostart=False)
    self.addCleanup(self.timers.close)
    self.service = HabitService(
        HabitsConfig(db=str(self.base / "habits.db")),
        timers=self.timers,
    )
    self.addCleanup(self.service.close)

  def test_crud_and_check_in(self):
    summary = self.service.add("Read", reminder_time="9pm")
    self.assertEqual(summary.habit.reminder_time, "21:00")
    self.assertEqual(summary.streak, 0)
    self.assertFalse(summary.done_today)
    self.assertIn("reminder 21:00 daily", format_habit(summary))
    self.assertIn("Habit #", format_habit_detail(summary))

    summary = self.service.check_in(summary.id)
    self.assertTrue(summary.done_today)
    self.assertEqual(summary.streak, 1)
    self.assertIn("done today", format_habit(summary))

    summary = self.service.undo(summary.id)
    self.assertFalse(summary.done_today)
    self.assertEqual(summary.streak, 0)

    summary = self.service.update(summary.id, title="Read a chapter")
    self.assertEqual(summary.title, "Read a chapter")
    self.assertEqual(self.service.list()[0].id, summary.id)
    removed = self.service.delete(summary.id)
    self.assertEqual(removed.title, "Read a chapter")
    self.assertEqual(self.service.list(), [])

  def test_streaks_count_consecutive_days_and_best(self):
    habit = self.service.add("Walk")
    today = date.today()
    for offset in (4, 3, 1, 0):
      self.service._store.check_in(habit.id, today - timedelta(days=offset))
    summary = self.service.summary(habit.id)
    self.assertEqual(summary.streak, 2)
    self.assertEqual(summary.best_streak, 2)
    self.assertEqual(summary.total_checkins, 4)

    # A gap breaks the current streak but keeps the best run.
    self.service.undo(habit.id)
    summary = self.service.summary(habit.id, today - timedelta(days=2))
    self.assertEqual(summary.streak, 2)

  def test_reminder_is_daily_and_survives_reopen(self):
    habit = self.service.add("Stretch", reminder_time="08:00")
    pending = self.timers.list()
    self.assertEqual(len(pending), 1)
    self.assertEqual(pending[0].source, "habit")
    self.assertEqual(pending[0].source_id, habit.id)
    self.assertEqual(pending[0].repeat, "daily")

    due = pending[0].due_at
    self.assertEqual(
        self.timers.poll(due + timedelta(seconds=1)),
        ["Habit: Stretch."],
    )
    remaining = self.timers.list()
    self.assertEqual(len(remaining), 1)
    self.assertEqual(remaining[0].id, pending[0].id)
    self.assertGreater(remaining[0].due_at, due)

    self.timers.close()
    self.service.close()
    timers = TimerService(self.base / "timers.db", autostart=False)
    self.addCleanup(timers.close)
    service = HabitService(
        HabitsConfig(db=str(self.base / "habits.db")),
        timers=timers,
    )
    self.addCleanup(service.close)
    self.assertEqual(len(timers.list()), 1)
    self.assertEqual(service.list()[0].id, habit.id)

  def test_edit_and_delete_keep_one_reminder(self):
    habit = self.service.add("Read", reminder_time="9pm")
    self.service.update(habit.id, reminder_time="7am")
    self.assertEqual(len(self.timers.list()), 1)
    self.assertEqual(self.timers.list()[0].label, "Read")
    self.service.update(habit.id, title="Read more")
    self.assertEqual(self.timers.list()[0].label, "Read more")
    self.service.update(habit.id, reminder_time="")
    self.assertEqual(self.timers.list(), [])
    self.service.update(habit.id, reminder_time="8pm")
    self.service.delete(habit.id)
    self.assertEqual(self.timers.list(), [])

  def test_validation_and_limit(self):
    with self.assertRaises(HabitError):
      self.service.add("   ")
    with self.assertRaises(HabitError):
      self.service.add("x" * 201)
    with self.assertRaises(HabitError):
      self.service.add("Read", reminder_time="not a time")
    with self.assertRaises(HabitError):
      self.service.summary(999)
    service = HabitService(
        HabitsConfig(db=str(self.base / "limited.db"), max_habits=1)
    )
    self.addCleanup(service.close)
    service.add("One")
    with self.assertRaises(HabitError):
      service.add("Two")

  def test_refresh_does_not_duplicate_existing_reminders(self):
    self.service.add("Read", reminder_time="9pm")
    self.service.refresh_reminders()
    self.assertEqual(len(self.timers.list()), 1)


class TimerTests(unittest.TestCase):
  def test_old_table_gains_repeat_column(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "old.db"
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
      try:
        columns = {
            row[1]
            for row in timers._db.execute(
                "PRAGMA table_info(reminders)"
            ).fetchall()
        }
        self.assertIn("repeat", columns)
        self.assertEqual(timers.list()[0].repeat, "")
      finally:
        timers.close()

  def test_stale_daily_reminder_rolls_forward_without_flooding(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "timers.db"
      timers = TimerService(path, autostart=False)
      now = datetime.now().replace(microsecond=0)
      try:
        timers.add_reminder(
            now + timedelta(minutes=1),
            "read",
            source="habit",
            source_id=3,
            repeat="daily",
        )
      finally:
        timers.close()
      db = sqlite3.connect(str(path))
      past = (now - timedelta(days=2)).isoformat(timespec="seconds")
      db.execute("UPDATE reminders SET due_at = ?", (past,))
      db.commit()
      db.close()

      timers = TimerService(path, autostart=False)
      try:
        self.assertGreater(timers.list()[0].due_at, now)
        self.assertEqual(timers.poll(), [])
      finally:
        timers.close()


class IntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_add_list_show_stats_and_edit_intents(self):
    cases = {
        "add a habit to read every day at 9pm": "habit_add",
        "start a habit to exercise daily": "habit_add",
        "track journal as a habit": "habit_add",
        "list my habits": "habit_list",
        "what are my habits": "habit_list",
        "show habit 2": "habit_show",
        "habit stats": "habit_stats",
        "show my habit streaks": "habit_stats",
        "habit 2 done": "habit_done",
        "complete habit 2": "habit_done",
        "undo habit 2": "habit_undo",
        "remove habit 2": "habit_delete",
    }
    for text, name in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, name, text)

  def test_habit_entities_and_commands(self):
    intent = self.router.route("add a habit to read every day at 9pm")
    self.assertEqual(intent.entities["title"], "read")
    self.assertEqual(intent.entities["reminder_time"], "21:00")
    self.assertEqual(self.router.route("/habits").name, "habits")
    self.assertEqual(self.router.route("/habit").name, "habit")


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
            base.habits,
            enabled=enabled,
            db=str(self.base / "habits.db"),
        ),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    return assistant

  def test_commands_and_natural_language(self):
    assistant = self.make()
    self.assertIn(
        "Habit #1",
        assistant.reply("add a habit to read every day at 9pm"),
    )
    self.assertIn("read", assistant.reply("/habit list"))
    self.assertIn("21:00", assistant.reply("show habit 1"))
    self.assertIn("checked in", assistant.reply("habit 1 done"))
    self.assertIn("already done", assistant.reply("complete habit 1"))
    self.assertIn("undone", assistant.reply("/habit undo 1"))
    self.assertIn("checked in", assistant.reply("/habit done 1"))
    self.assertIn("1 done today", assistant.reply("/habit stats"))
    self.assertIn("updated", assistant.reply("/habit edit 1 name Read daily"))
    self.assertIn("No reminder", assistant.reply("/habit edit 1 clear reminder"))
    self.assertIn("removed", assistant.reply("/habit remove 1"))
    self.assertEqual(assistant.reply("/habit list"), "You have no habits, sir.")

  def test_natural_delete_requires_confirmation_and_uses_tool(self):
    assistant = self.make(tools=True)
    assistant.reply("add a habit to stretch")
    pending = assistant.reply("remove habit 1")
    self.assertIn("Delete habit #1", pending)
    self.assertIn("confirm", pending.lower())
    self.assertEqual(assistant._pending_tool[0], "habit_delete")
    self.assertIsNotNone(assistant._get_habits().get(1))
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_habits().get(1))

  def test_natural_delete_can_be_cancelled(self):
    assistant = self.make(tools=True)
    assistant.reply("add a habit to stretch")
    self.assertIn("confirm", assistant.reply("delete habit 1").lower())
    self.assertIn("cancelled", assistant.reply("no").lower())
    self.assertIsNotNone(assistant._get_habits().get(1))

  def test_natural_delete_works_without_tools(self):
    assistant = self.make(tools=False)
    assistant.reply("add a habit to stretch")
    self.assertIn("confirm", assistant.reply("remove habit 1").lower())
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_habits().get(1))

  def test_disabled_habits(self):
    assistant = self.make(enabled=False)
    self.assertEqual(
        assistant.reply("/habits"), "Habit tracking is disabled, sir."
    )
    self.assertEqual(
        assistant.reply("add a habit to read"),
        "Habit tracking is disabled, sir.",
    )


class HabitToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    settings = load_settings()
    self.settings = dataclasses.replace(
        settings,
        habits=dataclasses.replace(
            settings.habits, db=str(self.base / "habits.db")
        ),
        tools=dataclasses.replace(settings.tools, root=str(self.base)),
    )
    self.timers = TimerService(self.base / "timers.db", autostart=False)
    self.addCleanup(self.timers.close)
    self.service = HabitService(self.settings.habits, timers=self.timers)
    self.addCleanup(self.service.close)
    self.registry = build_registry(
        self.settings, habits_service=self.service
    )

  def test_tools_are_registered_with_permissions(self):
    names = set(self.registry.names())
    for name in {
        "habit_add",
        "habit_list",
        "habit_show",
        "habit_complete",
        "habit_undo",
        "habit_update",
        "habit_delete",
    }:
      self.assertIn(name, names)
    self.assertTrue(self.registry.requires_confirmation("habit_delete"))
    self.assertFalse(self.registry.requires_confirmation("habit_complete"))

  def test_add_list_complete_undo_update_and_delete_tools(self):
    added = self.registry.execute(
        "habit_add", {"title": "Read", "remind": "9pm"}
    )
    self.assertTrue(added.ok, added.text)
    habit_id = added.data["id"]
    self.assertEqual(added.data["reminder_time"], "21:00")

    listed = self.registry.execute("habit_list")
    self.assertTrue(listed.ok, listed.text)
    self.assertEqual(listed.data["count"], 1)

    shown = self.registry.execute("habit_show", {"id": habit_id})
    self.assertTrue(shown.ok, shown.text)
    self.assertIn("Current streak", shown.text)

    done = self.registry.execute("habit_complete", {"id": habit_id})
    self.assertTrue(done.ok, done.text)
    self.assertEqual(done.data["streak"], 1)
    self.assertTrue(done.data["done_today"])

    undone = self.registry.execute("habit_undo", {"id": habit_id})
    self.assertTrue(undone.ok, undone.text)
    self.assertFalse(undone.data["done_today"])

    updated = self.registry.execute(
        "habit_update", {"id": habit_id, "title": "Read more"}
    )
    self.assertTrue(updated.ok, updated.text)
    self.assertEqual(updated.data["title"], "Read more")

    deleted = self.registry.execute("habit_delete", {"id": habit_id})
    self.assertTrue(deleted.ok, deleted.text)
    self.assertIsNone(self.service.get(habit_id))
    self.assertEqual(self.timers.list(), [])

  def test_tool_validation(self):
    missing = self.registry.execute("habit_add", {})
    self.assertFalse(missing.ok)
    self.assertIn("missing argument", missing.text)

    bad_type = self.registry.execute("habit_show", {"id": "one"})
    self.assertFalse(bad_type.ok)
    self.assertIn("must be int", bad_type.text)

    empty = self.registry.execute("habit_add", {"title": "   "})
    self.assertFalse(empty.ok)
    self.assertIn("needs a name", empty.text)

    bad_time = self.registry.execute(
        "habit_add", {"title": "Read", "remind": "soon"}
    )
    self.assertFalse(bad_time.ok)
    self.assertIn("clock time", bad_time.text)


class ConfigTests(unittest.TestCase):
  def test_invalid_habit_limit_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[habits]\nmax_habits = 0\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


class NoModelClient:
  """Fail if a deterministic habit request reaches the model."""

  def chat(self, *args, **kwargs):
    raise AssertionError("the model must not be called")


if __name__ == "__main__":
  unittest.main()
