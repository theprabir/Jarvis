"""Feature set 4.11 M7 tests: calendar events and daily agendas."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.assistant import Assistant
from core.calendar import (
    CalendarError,
    CalendarService,
    format_agenda,
    parse_event_input,
)
from core.config import CalendarConfig, ConfigError, load_settings
from core.intents import IntentRouter
from tools import build_registry


class ParseTests(unittest.TestCase):
  def test_title_start_duration_and_details(self):
    parsed = parse_event_input(
        "lunch tomorrow at noon for 30 minutes | with Sam"
    )
    self.assertEqual(parsed["title"], "lunch")
    self.assertEqual(parsed["duration_minutes"], 30)
    self.assertEqual(parsed["details"], "with Sam")
    self.assertEqual(parsed["start_at"].hour, 12)

  def test_default_duration_and_empty_input(self):
    parsed = parse_event_input("standup tomorrow at 9am")
    self.assertEqual(parsed["title"], "standup")
    self.assertEqual(parsed["duration_minutes"], 60)
    self.assertEqual(parsed["start_at"].hour, 9)
    self.assertEqual(parse_event_input("")["title"], "")


class ServiceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    self.service = CalendarService(
        CalendarConfig(db=str(self.base / "calendar.db"))
    )
    self.addCleanup(self.service.close)

  def test_create_agenda_update_delete(self):
    start = datetime.now().replace(microsecond=0) + timedelta(days=1, hours=2)
    start = start.replace(hour=10, minute=0, second=0)
    event = self.service.add(
        "Dentist", start, duration_minutes=45, details="Bring card"
    )
    self.assertEqual(event.duration_minutes, 45)
    self.assertEqual(event.end_at, start + timedelta(minutes=45))
    agenda = self.service.agenda(start.date())
    self.assertEqual([item.id for item in agenda.events], [event.id])
    self.assertIn("Dentist", format_agenda(agenda))
    self.assertIn("10:00-10:45", format_agenda(agenda))

    event = self.service.update(event.id, title="Dentist checkup")
    self.assertEqual(event.title, "Dentist checkup")
    event = self.service.update(event.id, duration_minutes=60)
    self.assertEqual(event.duration_minutes, 60)

    upcoming = self.service.upcoming()
    self.assertEqual(upcoming[0].id, event.id)
    removed = self.service.delete(event.id)
    self.assertEqual(removed.id, event.id)
    self.assertIsNone(self.service.get(event.id))

  def test_validation_and_limit(self):
    with self.assertRaises(CalendarError):
      self.service.add("   ", datetime.now())
    with self.assertRaises(CalendarError):
      self.service.add("Bad", "not a datetime")
    with self.assertRaises(CalendarError):
      self.service.add("Bad duration", datetime.now(), duration_minutes=0)
    limited = CalendarService(
        CalendarConfig(db=str(self.base / "limited.db"), max_events=1)
    )
    self.addCleanup(limited.close)
    limited.add("One", datetime.now() + timedelta(hours=1))
    with self.assertRaises(CalendarError):
      limited.add("Two", datetime.now() + timedelta(hours=2))

  def test_agenda_date_phrases(self):
    tomorrow = self.service.agenda("tomorrow")
    self.assertEqual(
        tomorrow.day, datetime.now().date() + timedelta(days=1)
    )
    with self.assertRaises(CalendarError):
      self.service.agenda("someday")


class IntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_calendar_intents(self):
    cases = {
        "add a calendar event for lunch tomorrow at noon": "calendar_add",
        "schedule event for standup tomorrow at 9am": "calendar_add",
        "what's on my calendar today": "calendar_agenda",
        "what is on my agenda tomorrow": "calendar_agenda",
        "show calendar event 2": "calendar_show",
        "remove calendar event 2": "calendar_delete",
    }
    for text, name in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, name, text)

  def test_entities_and_commands(self):
    intent = self.router.route(
        "add a calendar event for lunch tomorrow at noon"
    )
    self.assertEqual(intent.entities["title"], "lunch")
    self.assertIn("start_at", intent.entities)
    self.assertEqual(intent.entities["duration_minutes"], 60)
    self.assertEqual(self.router.route("/calendar").name, "calendar")
    self.assertEqual(self.router.route("/cal").name, "cal")


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
            base.time, enabled=False, db=str(self.base / "time.db")
        ),
        calendar=dataclasses.replace(
            base.calendar,
            enabled=enabled,
            db=str(self.base / "calendar.db"),
        ),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    return assistant

  def test_commands_and_natural_language(self):
    assistant = self.make()
    answer = assistant.reply(
        "add a calendar event for lunch tomorrow at noon"
    )
    self.assertIn("Event #1", answer)
    self.assertIn("12:00", answer)
    self.assertIn("lunch", assistant.reply("what's on my calendar tomorrow"))
    self.assertIn("lunch", assistant.reply("/calendar agenda tomorrow"))
    self.assertIn("lunch", assistant.reply("/calendar list"))
    self.assertIn("lunch", assistant.reply("show calendar event 1"))
    self.assertIn(
        "updated", assistant.reply("/calendar edit 1 title Lunch with Sam")
    )
    self.assertIn("removed", assistant.reply("/calendar remove 1"))
    self.assertEqual(
        assistant.reply("what's on my calendar today"),
        "Your calendar is clear for "
        + datetime.now().strftime("%A %d %B %Y")
        + ".",
    )

  def test_natural_delete_requires_confirmation_and_uses_tool(self):
    assistant = self.make(tools=True)
    assistant.reply("add a calendar event for dentist tomorrow at 3pm")
    pending = assistant.reply("remove calendar event 1")
    self.assertIn("Delete event #1", pending)
    self.assertIn("confirm", pending.lower())
    self.assertEqual(assistant._pending_tool[0], "calendar_delete")
    self.assertIsNotNone(assistant._get_calendar().get(1))
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_calendar().get(1))

  def test_natural_delete_can_be_cancelled(self):
    assistant = self.make(tools=True)
    assistant.reply("add a calendar event for dentist tomorrow at 3pm")
    self.assertIn("confirm", assistant.reply("delete event 1").lower())
    self.assertIn("cancelled", assistant.reply("no").lower())
    self.assertIsNotNone(assistant._get_calendar().get(1))

  def test_natural_delete_works_without_tools(self):
    assistant = self.make(tools=False)
    assistant.reply("add a calendar event for dentist tomorrow at 3pm")
    self.assertIn("confirm", assistant.reply("remove event 1").lower())
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_calendar().get(1))

  def test_disabled_calendar(self):
    assistant = self.make(enabled=False)
    self.assertEqual(
        assistant.reply("/calendar"), "Calendar is disabled, sir."
    )
    self.assertEqual(
        assistant.reply("what's on my calendar today"),
        "Calendar is disabled, sir.",
    )


class CalendarToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    settings = load_settings()
    self.settings = dataclasses.replace(
        settings,
        calendar=dataclasses.replace(
            settings.calendar, db=str(self.base / "calendar.db")
        ),
        tools=dataclasses.replace(settings.tools, root=str(self.base)),
    )
    self.service = CalendarService(self.settings.calendar)
    self.addCleanup(self.service.close)
    self.registry = build_registry(
        self.settings, calendar_service=self.service
    )

  def test_tools_are_registered_with_permissions(self):
    names = set(self.registry.names())
    for name in {
        "calendar_add",
        "calendar_agenda",
        "calendar_list",
        "calendar_show",
        "calendar_update",
        "calendar_delete",
    }:
      self.assertIn(name, names)
    self.assertTrue(self.registry.requires_confirmation("calendar_delete"))
    self.assertFalse(self.registry.requires_confirmation("calendar_add"))

  def test_add_agenda_show_update_and_delete_tools(self):
    added = self.registry.execute(
        "calendar_add",
        {
            "title": "Dentist",
            "start": "tomorrow at 3pm",
            "duration": 45,
            "details": "Bring card",
        },
    )
    self.assertTrue(added.ok, added.text)
    event_id = added.data["id"]
    self.assertEqual(added.data["duration_minutes"], 45)

    agenda = self.registry.execute("calendar_agenda", {"day": "tomorrow"})
    self.assertTrue(agenda.ok, agenda.text)
    self.assertEqual(agenda.data["count"], 1)

    shown = self.registry.execute("calendar_show", {"id": event_id})
    self.assertTrue(shown.ok, shown.text)
    self.assertIn("Dentist", shown.text)

    updated = self.registry.execute(
        "calendar_update", {"id": event_id, "title": "Dentist checkup"}
    )
    self.assertTrue(updated.ok, updated.text)
    self.assertEqual(updated.data["title"], "Dentist checkup")

    listed = self.registry.execute("calendar_list")
    self.assertTrue(listed.ok, listed.text)
    self.assertEqual(listed.data["count"], 1)

    deleted = self.registry.execute("calendar_delete", {"id": event_id})
    self.assertTrue(deleted.ok, deleted.text)
    self.assertIsNone(self.service.get(event_id))

  def test_tool_validation(self):
    missing = self.registry.execute("calendar_add", {"title": "Dentist"})
    self.assertFalse(missing.ok)
    self.assertIn("missing argument", missing.text)

    bad_type = self.registry.execute("calendar_show", {"id": "one"})
    self.assertFalse(bad_type.ok)
    self.assertIn("must be int", bad_type.text)

    empty = self.registry.execute(
        "calendar_add", {"title": "   ", "start": "tomorrow at 3pm"}
    )
    self.assertFalse(empty.ok)
    self.assertIn("needs a title", empty.text)


class ConfigTests(unittest.TestCase):
  def test_invalid_calendar_limit_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[calendar]\nmax_events = 0\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


class NoModelClient:
  """Fail if a deterministic calendar request reaches the model."""

  def chat(self, *args, **kwargs):
    raise AssertionError("the model must not be called")


if __name__ == "__main__":
  unittest.main()
