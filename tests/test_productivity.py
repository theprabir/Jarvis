"""Feature set 4.11 M1-M2 tests: productivity to-dos and planning.

The suite is offline: timers are polled manually and the assistant uses a
client that fails if a deterministic to-do request ever reaches the model.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

from core.assistant import Assistant
from core.config import ConfigError, ProductivityConfig, load_settings
from core.intents import IntentRouter
from core.productivity import (
    ProductivityError,
    ProductivityService,
    format_plan,
    format_todo,
    parse_todo_input,
    parse_when,
)
from core.timers import TimerService
from tools import build_registry


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

  def test_parse_time_of_day_phrases(self):
    cases = {
        "tomorrow morning": datetime(2026, 11, 11, 9, 0),
        "tomorrow afternoon": datetime(2026, 11, 11, 15, 0),
        "tomorrow evening": datetime(2026, 11, 11, 18, 0),
        "Friday at noon": datetime(2026, 11, 13, 12, 0),
        "tomorrow night at 9": datetime(2026, 11, 11, 21, 0),
        "tomorrow morning at 5": datetime(2026, 11, 11, 5, 0),
        "tomorrow at 5": datetime(2026, 11, 11, 17, 0),
        "tomorrow at 17": datetime(2026, 11, 11, 17, 0),
    }
    for text, expected in cases.items():
      self.assertEqual(parse_when(text, self.NOW), expected, text)

  def test_parse_relative_and_calendar_phrases(self):
    self.assertEqual(
        parse_when("in a week", self.NOW),
        self.NOW + timedelta(days=7),
    )
    self.assertEqual(
        parse_when("next week", self.NOW), datetime(2026, 11, 17, 9, 0)
    )
    self.assertEqual(
        parse_when("two weeks from today", self.NOW),
        datetime(2026, 11, 24, 9, 0),
    )
    self.assertEqual(
        parse_when("in 2 days at 5pm", self.NOW),
        datetime(2026, 11, 12, 17, 0),
    )
    self.assertEqual(
        parse_when("in half an hour", self.NOW),
        datetime(2026, 11, 10, 12, 30),
    )
    self.assertEqual(
        parse_when("in two months", self.NOW),
        datetime(2027, 1, 10, 9, 0),
    )
    self.assertEqual(
        parse_when("November 20", self.NOW),
        datetime(2026, 11, 20, 9, 0),
    )
    self.assertEqual(
        parse_when("20 November", self.NOW),
        datetime(2026, 11, 20, 9, 0),
    )
    self.assertEqual(
        parse_when("the 20th", self.NOW),
        datetime(2026, 11, 20, 9, 0),
    )
    self.assertEqual(
        parse_when("on the 3rd", self.NOW),
        datetime(2026, 12, 3, 9, 0),
    )
    self.assertIsNone(parse_when("November 31", self.NOW))
    self.assertIsNone(parse_when("February 30", self.NOW))

  def test_parse_end_of_period_phrases(self):
    self.assertEqual(
        parse_when("end of day", self.NOW),
        datetime(2026, 11, 10, 17, 0),
    )
    self.assertEqual(
        parse_when("eod", self.NOW),
        datetime(2026, 11, 10, 17, 0),
    )
    self.assertEqual(
        parse_when("end of week", self.NOW),
        datetime(2026, 11, 13, 17, 0),
    )
    self.assertEqual(
        parse_when("end of month", self.NOW),
        datetime(2026, 11, 30, 17, 0),
    )
    saturday = datetime(2026, 11, 14, 12, 0)
    self.assertEqual(
        parse_when("this weekend", saturday),
        datetime(2026, 11, 21, 9, 0),
    )
    last_day = datetime(2026, 11, 30, 18, 0)
    self.assertEqual(
        parse_when("end of month", last_day),
        datetime(2026, 12, 31, 17, 0),
    )

  def test_parse_exact_time_is_not_rolled_forward(self):
    self.assertEqual(
        parse_when("noon", self.NOW), datetime(2026, 11, 10, 12, 0)
    )
    self.assertEqual(
        parse_when("tonight", self.NOW), datetime(2026, 11, 10, 20, 0)
    )

  def test_past_same_day_dates_roll_to_their_next_occurrence(self):
    friday = datetime(2026, 11, 13, 18, 0)
    self.assertEqual(
        parse_when("Friday at 5pm", friday),
        datetime(2026, 11, 20, 17, 0),
    )
    self.assertEqual(
        parse_when("November 20 at 5pm", datetime(2026, 11, 20, 18, 0)),
        datetime(2027, 11, 20, 17, 0),
    )
    self.assertEqual(
        parse_when("the 20th at 5pm", datetime(2026, 11, 20, 18, 0)),
        datetime(2026, 12, 20, 17, 0),
    )
    parsed = parse_todo_input("call mom Friday at 5pm", friday)
    self.assertEqual(parsed["title"], "call mom")
    self.assertEqual(parsed["due_at"], datetime(2026, 11, 20, 17, 0))

  def test_parse_implicit_relative_duration_due(self):
    parsed = parse_todo_input("call mom in 2 hours", self.NOW)
    self.assertEqual(parsed["title"], "call mom")
    self.assertEqual(parsed["due_at"], datetime(2026, 11, 10, 14, 0))
    parsed = parse_todo_input("call mom in half an hour", self.NOW)
    self.assertEqual(parsed["title"], "call mom")
    self.assertEqual(parsed["due_at"], datetime(2026, 11, 10, 12, 30))

  def test_parse_implicit_natural_due_and_clean_title(self):
    cases = {
        "call mom tomorrow morning": (
            "call mom",
            datetime(2026, 11, 11, 9, 0),
        ),
        "submit report by next Friday": (
            "submit report",
            datetime(2026, 11, 13, 9, 0),
        ),
        "book tickets the 20th": (
            "book tickets",
            datetime(2026, 11, 20, 9, 0),
        ),
        "plan trip next month": (
            "plan trip",
            datetime(2026, 12, 10, 9, 0),
        ),
        "call mom on Friday": (
            "call mom",
            datetime(2026, 11, 13, 9, 0),
        ),
        "call mom eod": (
            "call mom",
            datetime(2026, 11, 10, 17, 0),
        ),
    }
    for text, (title, due) in cases.items():
      parsed = parse_todo_input(text, self.NOW)
      self.assertEqual(parsed["title"], title, text)
      self.assertEqual(parsed["due_at"], due, text)

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

  def test_daily_plan_groups_orders_and_excludes_completed_items(self):
    today = date.today()
    overdue = self.service.add(
        "old report",
        due_at=datetime.combine(today - timedelta(days=1), time(9, 0)),
    )
    morning = self.service.add(
        "morning call",
        due_at=datetime.combine(today, time(10, 0)),
        priority="high",
    )
    evening = self.service.add(
        "evening review",
        due_at=datetime.combine(today, time(18, 0)),
    )
    future = self.service.add(
        "future task",
        due_at=datetime.combine(today + timedelta(days=2), time(9, 0)),
    )
    unscheduled_low = self.service.add("low idea", priority="low")
    unscheduled_high = self.service.add("high idea", priority="high")
    done = self.service.add("already done")
    self.service.complete(done.id)

    plan = self.service.daily_plan(today)
    self.assertEqual([item.id for item in plan.overdue], [overdue.id])
    self.assertEqual(
        [item.id for item in plan.days[0].items], [morning.id, evening.id]
    )
    self.assertEqual([item.id for item in plan.later], [future.id])
    self.assertEqual(
        [item.id for item in plan.unscheduled],
        [unscheduled_high.id, unscheduled_low.id],
    )
    self.assertEqual(plan.total_open, 6)
    self.assertNotIn(done.id, [item.id for item in plan.unscheduled])

  def test_weekly_plan_covers_seven_days_and_renders_sections(self):
    today = date.today()
    self.service.add(
        "later this week",
        due_at=datetime.combine(today + timedelta(days=3), time(9, 0)),
    )
    self.service.add(
        "after the week",
        due_at=datetime.combine(today + timedelta(days=9), time(9, 0)),
    )
    self.service.add("unscheduled")

    plan = self.service.weekly_plan(today)
    self.assertEqual(len(plan.days), 7)
    self.assertEqual(plan.end, today + timedelta(days=6))
    self.assertEqual(plan.days[3].items[0].title, "later this week")
    self.assertEqual(plan.later[0].title, "after the week")

    text = format_plan(plan, now=datetime.combine(today, time(8, 0)))
    self.assertIn("Weekly plan", text)
    self.assertIn("Today -", text)
    self.assertIn("later this week", text)
    self.assertIn("Unscheduled", text)
    self.assertIn("Later", text)
    self.assertIn("3 open items", text)

  def test_plan_dates_accept_phrases_and_reject_unknown_text(self):
    tomorrow = self.service.daily_plan("tomorrow")
    self.assertEqual(tomorrow.start, date.today() + timedelta(days=1))
    with self.assertRaises(ProductivityError):
      self.service.daily_plan("someday")


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
        "plan my day": "plan_day",
        "plan tomorrow": "plan_day",
        "what's my plan today": "plan_day",
        "plan my week": "plan_week",
        "weekly plan": "plan_week",
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
    self.assertEqual(self.router.route("/plan").name, "plan")
    self.assertEqual(
        self.router.route("plan a task to organize downloads").name,
        "task_plan",
    )


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

  def test_daily_and_weekly_plans_from_commands_and_natural_language(self):
    assistant = self.make()
    assistant.reply("/todo add call mom due today at 17")
    daily = assistant.reply("/plan")
    self.assertIn("Daily plan", daily)
    self.assertIn("call mom", daily)
    self.assertIn("Daily plan", assistant.reply("plan my day"))
    self.assertIn("Daily plan", assistant.reply("/plan tomorrow"))

    weekly = assistant.reply("/plan week")
    self.assertIn("Weekly plan", weekly)
    self.assertIn("call mom", weekly)
    self.assertIn("Weekly plan", assistant.reply("plan my week"))

  def test_natural_delete_requires_confirmation_and_uses_tool(self):
    assistant = self.make(tools=True)
    assistant.reply("add a to-do to buy milk")
    pending = assistant.reply("remove todo 1")
    self.assertIn("Delete to-do #1", pending)
    self.assertIn("confirm", pending.lower())
    self.assertEqual(assistant._pending_tool[0], "todo_delete")
    self.assertEqual(assistant._pending_tool[1], {"id": 1})
    self.assertIsNotNone(assistant._get_productivity().get(1))
    done = assistant.reply("confirm")
    self.assertIn("removed", done.lower())
    self.assertIsNone(assistant._get_productivity().get(1))

  def test_natural_delete_can_be_cancelled(self):
    assistant = self.make(tools=True)
    assistant.reply("add a to-do to buy milk")
    self.assertIn("confirm", assistant.reply("remove todo 1").lower())
    self.assertIn("cancelled", assistant.reply("no").lower())
    self.assertIsNotNone(assistant._get_productivity().get(1))

  def test_natural_delete_works_without_tools_after_confirmation(self):
    assistant = self.make(tools=False)
    assistant.reply("add a to-do to buy milk")
    self.assertIn("confirm", assistant.reply("remove todo 1").lower())
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_productivity().get(1))

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
    self.assertEqual(
        assistant.reply("/plan"), "Productivity is disabled, sir."
    )

  def test_tool_registry_uses_the_assistant_service(self):
    assistant = self.make(tools=True)
    answer = assistant.reply("add a to-do to buy milk")
    self.assertIn("buy milk", answer)
    result = assistant.registry.execute("todo_list")
    self.assertTrue(result.ok, result.text)
    self.assertEqual(result.data["count"], 1)
    self.assertEqual(result.data["items"][0]["title"], "buy milk")
    self.assertIn("todo_add (safe)", assistant.reply("/tools"))


class ProductivityToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    settings = load_settings()
    self.settings = dataclasses.replace(
        settings,
        productivity=dataclasses.replace(
            settings.productivity, db=str(self.base / "todos.db")
        ),
        tools=dataclasses.replace(settings.tools, root=str(self.base)),
    )
    self.service = ProductivityService(self.settings.productivity)
    self.addCleanup(self.service.close)
    self.registry = build_registry(
        self.settings, productivity_service=self.service
    )

  def test_tools_are_registered_with_permissions(self):
    names = set(self.registry.names())
    for name in {
        "todo_add",
        "todo_list",
        "todo_show",
        "todo_update",
        "todo_complete",
        "todo_snooze",
        "todo_delete",
        "plan_day",
        "plan_week",
    }:
      self.assertIn(name, names)
    self.assertTrue(self.registry.requires_confirmation("todo_delete"))
    self.assertFalse(self.registry.requires_confirmation("todo_add"))
    self.assertFalse(self.registry.requires_confirmation("plan_day"))

  def test_plan_tools_return_structured_agendas(self):
    added = self.registry.execute(
        "todo_add",
        {"title": "call mom", "due": "tomorrow at 5"},
    )
    self.assertTrue(added.ok, added.text)

    daily = self.registry.execute("plan_day", {"day": "tomorrow"})
    self.assertTrue(daily.ok, daily.text)
    self.assertEqual(daily.data["days"][0]["items"][0]["title"], "call mom")
    self.assertEqual(len(daily.data["days"]), 1)

    weekly = self.registry.execute("plan_week")
    self.assertTrue(weekly.ok, weekly.text)
    self.assertEqual(len(weekly.data["days"]), 7)
    self.assertIn("call mom", weekly.text)

    invalid = self.registry.execute("plan_day", {"day": "someday"})
    self.assertFalse(invalid.ok)
    self.assertIn("couldn't understand that plan date", invalid.text)

  def test_add_list_show_update_complete_and_delete(self):
    added = self.registry.execute(
        "todo_add",
        {
            "title": "call mom",
            "due": "tomorrow at 5",
            "priority": "high",
        },
    )
    self.assertTrue(added.ok, added.text)
    item_id = added.data["id"]
    self.assertEqual(self.service.get(item_id).title, "call mom")
    self.assertEqual(added.data["due_at"][-5:], "17:00")

    listed = self.registry.execute("todo_list", {"status": "open"})
    self.assertTrue(listed.ok)
    self.assertEqual(listed.data["count"], 1)
    self.assertEqual(listed.data["items"][0]["title"], "call mom")

    shown = self.registry.execute("todo_show", {"id": item_id})
    self.assertTrue(shown.ok)
    self.assertIn("call mom", shown.text)

    updated = self.registry.execute(
        "todo_update", {"id": item_id, "title": "call mother"}
    )
    self.assertTrue(updated.ok)
    self.assertEqual(self.service.get(item_id).title, "call mother")

    cleared = self.registry.execute(
        "todo_update",
        {"id": item_id, "clear_due": True, "clear_remind": True},
    )
    self.assertTrue(cleared.ok, cleared.text)
    self.assertIsNone(self.service.get(item_id).due_at)
    self.assertIsNone(self.service.get(item_id).remind_at)

    completed = self.registry.execute("todo_complete", {"id": item_id})
    self.assertTrue(completed.ok)
    self.assertEqual(completed.data["todo"]["status"], "done")

    deleted = self.registry.execute("todo_delete", {"id": item_id})
    self.assertTrue(deleted.ok)
    self.assertIsNone(self.service.get(item_id))

  def test_tool_validation_and_productivity_errors(self):
    missing = self.registry.execute("todo_add", {})
    self.assertFalse(missing.ok)
    self.assertIn("missing argument", missing.text)

    bad_type = self.registry.execute("todo_show", {"id": "one"})
    self.assertFalse(bad_type.ok)
    self.assertIn("must be int", bad_type.text)

    bad_due = self.registry.execute(
        "todo_add", {"title": "call mom", "due": "someday"}
    )
    self.assertFalse(bad_due.ok)
    self.assertIn("couldn't understand the due time", bad_due.text)

    bad_status = self.registry.execute("todo_list", {"status": "later"})
    self.assertFalse(bad_status.ok)
    self.assertIn("status must be", bad_status.text)

    bad_priority = self.registry.execute(
        "todo_add", {"title": "call mom", "priority": "urgent-ish"}
    )
    self.assertFalse(bad_priority.ok)
    self.assertIn("priority must be", bad_priority.text)

  def test_recurring_completion_and_snooze(self):
    added = self.registry.execute(
        "todo_add",
        {
            "title": "water plants",
            "due": "tomorrow",
            "recurrence": "daily",
        },
    )
    self.assertTrue(added.ok, added.text)
    completed = self.registry.execute(
        "todo_complete", {"id": added.data["id"]}
    )
    self.assertTrue(completed.ok)
    self.assertIsNotNone(completed.data["next"])
    self.assertEqual(completed.data["next"]["recurrence"], "daily")

    snoozed = self.registry.execute(
        "todo_snooze", {"id": completed.data["next"]["id"], "minutes": 5}
    )
    self.assertTrue(snoozed.ok)
    self.assertTrue(snoozed.data["remind_at"])

  def test_provider_is_lazy_and_returns_the_same_service(self):
    calls = []

    def provider():
      calls.append(True)
      return self.service

    registry = build_registry(self.settings, productivity_service=provider)
    self.assertEqual(calls, [])
    result = registry.execute("todo_list")
    self.assertTrue(result.ok)
    self.assertEqual(calls, [True])


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
