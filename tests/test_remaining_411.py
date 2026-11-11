"""Tests for the remaining 4.11 Pomodoro, clipboard and summary features."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.assistant import Assistant
from core.clipboard_history import ClipboardError, ClipboardHistoryService
from core.config import ClipboardConfig, ConfigError, PomodoroConfig, load_settings
from core.intents import IntentRouter
from core.pomodoro import PomodoroError, PomodoroService
from core.summaries import SummaryError, SummaryService, format_meeting_summary
from core.timers import TimerService


class NoModelClient:
    def chat(self, *args, **kwargs):
        raise AssertionError("the model must not be called")


class PomodoroTests(unittest.TestCase):
    def setUp(self):
        self.timers = TimerService(autostart=False)
        self.addCleanup(self.timers.close)
        self.service = PomodoroService(
            self.timers, PomodoroConfig(work_minutes=25, break_minutes=5)
        )

    def test_work_then_break_then_work(self):
        status = self.service.start()
        self.assertTrue(status.running)
        self.assertEqual(status.phase, "work")
        entry = self.timers.get(status.remaining_s and self.timers.list()[0].id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.label, "Pomodoro work")

        self.timers.poll(datetime.now() + timedelta(minutes=26))
        self.assertEqual(self.service.status().phase, "break")
        self.assertEqual(self.service.status().completed_work, 1)
        self.assertEqual(self.timers.list()[0].label, "Pomodoro break")

        self.timers.poll(datetime.now() + timedelta(minutes=40))
        self.assertEqual(self.service.status().phase, "work")
        self.assertEqual(self.timers.list()[0].label, "Pomodoro work")

    def test_stop_skip_and_validation(self):
        with self.assertRaises(PomodoroError):
            self.service.skip()
        self.service.start()
        self.assertEqual(self.service.skip().phase, "break")
        self.service.stop()
        self.assertFalse(self.service.status().running)
        with self.assertRaises(PomodoroError):
            self.service.start(0)
        with self.assertRaises(PomodoroError):
            self.service.start(181)

    def test_intents_and_commands(self):
        router = IntentRouter()
        self.assertEqual(
            router.route("start a pomodoro").name, "pomodoro_start"
        )
        self.assertEqual(
            router.route("pomodoro status").name, "pomodoro_status"
        )
        self.assertEqual(
            router.route("stop the pomodoro").name, "pomodoro_stop"
        )
        self.assertEqual(router.route("/pomodoro").name, "pomodoro")
        self.assertEqual(router.route("/pomo").name, "pomo")


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = ClipboardHistoryService(
            ClipboardConfig(db=str(Path(self.tmp.name) / "clipboard.db"))
        )
        self.addCleanup(self.service.close)

    def test_crud_search_and_clear(self):
        first = self.service.add("Invoice number 42", source="clipboard")
        second = self.service.add("Lunch with Sam", source="command")
        self.assertEqual(len(self.service.list()), 2)
        self.assertEqual(self.service.search("invoice")[0].id, first.id)
        self.assertEqual(self.service.get(second.id).content, "Lunch with Sam")
        self.service.delete(first.id)
        self.assertEqual(len(self.service.list()), 1)
        self.assertEqual(self.service.clear(), 1)
        self.assertEqual(self.service.list(), [])

    def test_bounds_and_validation(self):
        with self.assertRaises(ClipboardError):
            self.service.add("   ")
        with self.assertRaises(ClipboardError):
            self.service.search("")
        small = ClipboardHistoryService(
            ClipboardConfig(
                db=str(Path(self.tmp.name) / "small.db"),
                max_items=2,
                max_chars=5,
            )
        )
        self.addCleanup(small.close)
        small.add("12345")
        with self.assertRaises(ClipboardError):
            small.add("123456")
        small.add("a")
        small.add("b")
        self.assertEqual([entry.content for entry in small.list()], ["b", "a"])

    def test_history_intents(self):
        router = IntentRouter()
        self.assertEqual(
            router.route("show clipboard history").name, "clipboard_list"
        )
        self.assertEqual(
            router.route("search clipboard for invoice").name,
            "clipboard_search",
        )
        self.assertEqual(
            router.route("show clipboard entry 2").name, "clipboard_show"
        )
        self.assertEqual(
            router.route("remove clipboard entry 2").name,
            "clipboard_delete",
        )
        self.assertEqual(
            router.route("clear clipboard history").name, "clipboard_clear"
        )
        self.assertEqual(router.route("/clipboard").name, "clipboard")


class SummaryTests(unittest.TestCase):
    def test_meeting_summary(self):
        summary = format_meeting_summary(
            "Q3 review",
            "Revenue grew strongly. The launch date is unchanged. "
            "We agreed to review the plan next Friday.",
        )
        self.assertIn("Q3 review", summary)
        self.assertIn("Revenue", summary)
        with self.assertRaises(SummaryError):
            format_meeting_summary("Empty", "")

    def test_daily_summary_without_services(self):
        text = SummaryService().daily()
        self.assertIn("Daily summary for", text)

    def test_summary_intents_and_commands(self):
        router = IntentRouter()
        self.assertEqual(router.route("summarize my day").name, "summary_daily")
        self.assertEqual(
            router.route("summarize meeting Q3 | notes").name,
            "summary_meeting",
        )
        self.assertEqual(router.route("/summary").name, "summary")
        self.assertEqual(router.route("/summaries").name, "summaries")


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def make(self):
        settings = load_settings()
        settings = dataclasses.replace(
            settings,
            conversation=dataclasses.replace(settings.conversation, enabled=False),
            memory=dataclasses.replace(
                settings.memory, enabled=False, db=str(self.base / "memory.db")
            ),
            documents=dataclasses.replace(settings.documents, enabled=False),
            tools=dataclasses.replace(settings.tools, enabled=False),
            weather=dataclasses.replace(settings.weather, enabled=False),
            notes=dataclasses.replace(settings.notes, enabled=False),
            habits=dataclasses.replace(settings.habits, enabled=False),
            goals=dataclasses.replace(settings.goals, enabled=False),
            time=dataclasses.replace(settings.time, enabled=False),
            calendar=dataclasses.replace(settings.calendar, enabled=False),
            email=dataclasses.replace(settings.email, enabled=False),
            clipboard=dataclasses.replace(
                settings.clipboard, db=str(self.base / "clipboard.db")
            ),
        )
        assistant = Assistant(settings, client=NoModelClient())
        self.addCleanup(assistant.close)
        return assistant

    def test_clipboard_commands_and_natural_history(self):
        assistant = self.make()
        service = assistant._get_clipboard_history()
        service.add("Invoice number 42", source="clipboard")
        self.assertIn("Invoice", assistant.reply("show clipboard history"))
        self.assertIn(
            "Invoice", assistant.reply("search clipboard for invoice")
        )
        self.assertIn("Invoice", assistant.reply("/clipboard show 1"))
        self.assertIn(
            "confirm", assistant.reply("remove clipboard entry 1").lower()
        )
        self.assertIn("deleted", assistant.reply("yes").lower())
        self.assertEqual(assistant.reply("/clipboard"), "Clipboard commands: list, search QUERY, show ID, remove ID, clear.")

    def test_summaries_and_pomodoro_commands(self):
        assistant = self.make()
        self.assertIn("Daily summary", assistant.reply("/summary"))
        self.assertIn(
            "Q3", assistant.reply("/summary meeting Q3 | Revenue grew. Costs fell.")
        )
        self.assertIn("Pomodoro work started", assistant.reply("/pomodoro start 25"))
        self.assertIn("Pomodoro work", assistant.reply("/pomodoro status"))
        self.assertIn("stopped", assistant.reply("/pomodoro stop").lower())


class ConfigTests(unittest.TestCase):
    def test_invalid_pomodoro_and_clipboard_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.toml"
            path.write_text("[pomodoro]\nwork_minutes = 0\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)
            path.write_text("[clipboard]\nmax_items = 0\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
