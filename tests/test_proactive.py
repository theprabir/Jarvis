"""Feature set 4.19 tests: deterministic briefings and proactive checks."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from core.assistant import Assistant
from core.config import load_settings
from core.intents import IntentRouter
from core.proactive import ProactiveService, ProactiveState
from core.timers import TimerService


NOW = datetime(2026, 11, 13, 8, 0)


class FakeProductivity:
  def __init__(self):
    self.items = [
        SimpleNamespace(
            id=1, title="Write report", due_at=NOW + timedelta(hours=2), status="open"
        ),
        SimpleNamespace(
            id=2, title="Plan trip", due_at=None, status="open"
        ),
        SimpleNamespace(
            id=3, title="Pay bill", due_at=NOW - timedelta(days=1), status="open"
        ),
        SimpleNamespace(
            id=4, title="Far future", due_at=NOW + timedelta(days=10), status="open"
        ),
    ]

  def daily_plan(self, day):
    return SimpleNamespace(
        days=[SimpleNamespace(items=[self.items[0]])],
        overdue=[self.items[2]],
        unscheduled=[self.items[1]],
        later=[self.items[3]],
    )

  def list(self, status="open"):
    return self.items


class FakeCalendar:
  def agenda(self, day):
    start = NOW + timedelta(hours=1)
    return SimpleNamespace(
        events=[
            SimpleNamespace(
                id=1,
                title="Standup",
                start_at=start,
                duration_minutes=60,
                end_at=start + timedelta(hours=1),
            ),
            SimpleNamespace(
                id=2,
                title="Review",
                start_at=start + timedelta(minutes=30),
                duration_minutes=30,
                end_at=start + timedelta(minutes=60),
            ),
        ]
    )

  def upcoming(self, now=None, limit=20):
    return list(self.agenda(now.date()).events)


class FakeTasks:
  def list(self, limit=10):
    return [
        SimpleNamespace(
            id=7,
            goal="Organize downloads",
            status="running",
            steps=[1, 2, 3],
            done_count=lambda: 1,
            current_step="move",
        ),
        SimpleNamespace(
            id=8,
            goal="Finished task",
            status="completed",
            steps=[],
            done_count=lambda: 0,
            current_step="",
        ),
    ]


class FakeGoals:
  def list(self, status=None):
    return [
        SimpleNamespace(
            id=1,
            title="Read 12 books",
            status="active",
            target_date=date(2026, 11, 15),
            progress_percent=25,
        )
    ]


class FakeResearch:
  def history(self, limit=5):
    return [
        {
            "title": "AI news",
            "created": "2026-11-13 07:00",
            "report": "A new local model was released.",
            "sources": [1, 2],
        }
    ]


class FakeKnowledge:
  def due(self, limit=10):
    return [1, 2]


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


class ProactiveServiceTests(unittest.TestCase):
  def setUp(self):
    self.config = dataclasses.replace(load_settings().proactive)
    self.service = ProactiveService(
        self.config,
        productivity=FakeProductivity(),
        calendar=FakeCalendar(),
        tasks=FakeTasks(),
        goals=FakeGoals(),
        research=FakeResearch(),
        knowledge=FakeKnowledge(),
        system=lambda: {
            "disk_free_gb": 2.0,
            "disk_percent": 95.0,
            "memory_percent": 91.0,
            "cpu_percent": 40.0,
        },
        news=lambda query="": [
            {
                "title": "News A",
                "snippet": "Something happened",
                "url": "https://example.test/news",
            }
        ],
    )

  def test_morning_briefing_combines_local_sources(self):
    text = self.service.morning(NOW)
    self.assertIn("Good morning", text)
    self.assertIn("Today's tasks", text)
    self.assertIn("Pay bill", text)
    self.assertIn("Standup", text)
    self.assertIn("Read 12 books", text)
    self.assertIn("Organize downloads", text)
    self.assertIn("Research digest", text)
    self.assertIn("Low disk space", text)
    self.assertIn("Suggested next actions", text)
    self.assertEqual(text.count("Plan trip"), 1)

  def test_deadlines_and_conflicts(self):
    deadlines = self.service.deadlines(NOW)
    self.assertIn("Overdue", deadlines)
    self.assertIn("Write report", deadlines)
    self.assertNotIn("Far future", deadlines)
    conflicts = self.service.conflicts(NOW)
    self.assertIn("Standup", conflicts)
    self.assertIn("Review", conflicts)

  def test_unfinished_tasks_and_projects(self):
    tasks = self.service.unfinished(NOW)
    self.assertIn("Organize downloads", tasks)
    self.assertNotIn("Finished task", tasks)
    projects = self.service.projects()
    self.assertIn("Read 12 books", projects)
    self.assertIn("Organize downloads", projects)

  def test_system_alerts_use_thresholds(self):
    alerts = self.service.system_alerts()
    self.assertIn("Low disk space", alerts)
    self.assertIn("High memory use", alerts)
    self.assertNotIn("High CPU use", alerts)
    normal = self.service.system_alerts(
        {"disk_free_gb": 100.0, "disk_percent": 20.0,
         "memory_percent": 30.0, "cpu_percent": 10.0}
    )
    self.assertIn("within the configured limits", normal)

  def test_research_news_and_recommendations(self):
    research = self.service.research_summary()
    self.assertIn("AI news", research)
    news = self.service.news("AI")
    self.assertIn("News summary for AI", news)
    self.assertIn("example.test", news)
    recommendations = self.service.recommendations(NOW)
    self.assertIn("overdue", recommendations)
    self.assertIn("flashcard", recommendations)

  def test_acknowledgement_deduplicates_alerts(self):
    first = self.service.deadlines(NOW)
    self.assertIn("Pay bill", first)
    second = self.service.deadlines(NOW)
    self.assertIn("acknowledged", second)
    everything = self.service.alerts(NOW, include_all=True)
    self.assertIn("Pay bill", everything)
    reset = self.service.reset_acknowledgements()
    self.assertGreater(reset, 0)
    self.assertIn("Pay bill", self.service.deadlines(NOW))

  def test_acknowledgement_state_persists(self):
    path = self.root = Path(tempfile.mkdtemp(prefix="jarvis-proactive-"))
    state = ProactiveState(path / "state.db")
    service = ProactiveService(
        self.config,
        productivity=FakeProductivity(),
        state=state,
    )
    self.assertIn("Pay bill", service.deadlines(NOW))
    service.close()
    state = ProactiveState(path / "state.db")
    service = ProactiveService(
        self.config,
        productivity=FakeProductivity(),
        state=state,
    )
    self.assertIn("acknowledged", service.deadlines(NOW))
    service.close()
    import shutil

    shutil.rmtree(path, ignore_errors=True)

  def test_alerts_are_bounded_and_typed(self):
    text = self.service.alerts(NOW)
    self.assertIn("Proactive alerts", text)
    self.assertIn("Calendar conflicts", text)
    self.assertIn("System alerts", text)

  def test_natural_proactive_intents(self):
    router = IntentRouter()
    cases = (
        ("good morning", "morning"),
        ("morning briefing", "morning"),
        ("daily briefing", "daily"),
        ("what's due soon", "deadlines"),
        ("unfinished tasks", "tasks"),
        ("calendar conflicts", "conflicts"),
        ("research summary", "research"),
        ("news summary", "news"),
        ("system alerts", "system"),
        ("project progress", "projects"),
        ("what should I do next", "recommend"),
        ("proactive alerts", "alerts"),
    )
    for text, action in cases:
      with self.subTest(text=text):
        intent = router.route(text)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.name, "proactive")
        self.assertEqual(intent.entities["action"], action)


class ProactiveAssistantTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    root = Path(self.tmp.name)
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(
            base.conversation,
            enabled=False,
            history_db=str(root / "history.db"),
        ),
        memory=dataclasses.replace(base.memory, enabled=False),
        web=dataclasses.replace(base.web, enabled=False),
        documents=dataclasses.replace(base.documents, enabled=False),
        tools=dataclasses.replace(base.tools, root=str(root)),
        tasks=dataclasses.replace(base.tasks, enabled=False),
        knowledge=dataclasses.replace(base.knowledge, enabled=False),
        productivity=dataclasses.replace(
            base.productivity, db=str(root / "productivity.db")
        ),
        calendar=dataclasses.replace(
            base.calendar, db=str(root / "calendar.db")
        ),
        goals=dataclasses.replace(base.goals, enabled=False),
        proactive=dataclasses.replace(base.proactive),
    )
    self.assistant = Assistant(settings, client=FakeClient())
    self.addCleanup(self.assistant.close)

  def test_real_timer_silent_delivery_and_reschedule(self):
    timers = TimerService(autostart=False)
    self.addCleanup(timers.close)
    messages = []
    self.assistant._notifier = messages.append
    self.assistant._get_timers = lambda: timers
    self.assistant._briefing_timer = None
    self.assistant._schedule_briefing()
    entries = timers.list()
    self.assertEqual(len(entries), 1)
    entry = entries[0]
    self.assertTrue(entry.silent)
    delivered = timers.poll(now=entry.due_at)
    self.assertEqual(delivered, [])
    self.assertTrue(messages)
    self.assertIn("Good morning", messages[0])
    self.assertEqual(len(timers.list()), 1)
    self.assertTrue(timers.list()[0].silent)

  def test_scheduled_briefing_callback_and_reschedule(self):
    class FakeTimers:
      def __init__(self):
        self.entries = []

      def add_timer(self, seconds, label="", on_done=None, silent=False):
        entry = SimpleNamespace(
            id=len(self.entries) + 1,
            seconds=seconds,
            label=label,
            on_done=on_done,
            silent=silent,
        )
        self.entries.append(entry)
        return entry

    timers = FakeTimers()
    messages = []
    self.assistant._notifier = messages.append
    self.assistant._get_timers = lambda: timers
    self.assistant._briefing_timer = None
    self.assistant._schedule_briefing()
    self.assertEqual(len(timers.entries), 1)
    entry = timers.entries[0]
    self.assertTrue(entry.silent)
    self.assertGreater(entry.seconds, 0)
    entry.on_done(entry)
    self.assertTrue(messages)
    self.assertIn("Good morning", messages[0])
    self.assertEqual(len(timers.entries), 2)

  def test_assistant_briefing_and_natural_commands(self):
    self.assertIn("/proactive", self.assistant.reply("/help"))
    briefing = self.assistant.reply("/briefing")
    self.assertIn("Good morning", briefing)
    natural = self.assistant.reply("what should I do next")
    self.assertIn("Suggested next actions", natural)
    news = self.assistant.reply("/proactive news")
    self.assertIn("No news items", news)
    acknowledged = self.assistant.reply("/proactive ack")
    self.assertIn("Acknowledged", acknowledged)
    reset = self.assistant.reply("/proactive reset")
    self.assertIn("Cleared", reset)
    projects = self.assistant.reply("/proactive projects")
    self.assertTrue(
        "no active goals" in projects.lower() or "project" in projects.lower()
    )


if __name__ == "__main__":
  unittest.main()
