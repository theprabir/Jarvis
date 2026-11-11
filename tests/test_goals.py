"""Feature set 4.11 M5 tests: goals, progress and target dates."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from core.assistant import Assistant
from core.config import ConfigError, GoalsConfig, load_settings
from core.goals import (
    GoalError,
    GoalService,
    format_goal,
    format_goal_detail,
    parse_goal_input,
)
from core.intents import IntentRouter
from tools import build_registry


class ParseTests(unittest.TestCase):
  def test_target_unit_and_date(self):
    parsed = parse_goal_input("read 12 books by December 31")
    self.assertEqual(parsed["title"], "read")
    self.assertEqual(parsed["target_value"], 12.0)
    self.assertEqual(parsed["unit"], "books")
    self.assertTrue(parsed["target_date"].endswith("-12-31"))

    parsed = parse_goal_input("save target 500 dollars due 31 January 2027")
    self.assertEqual(parsed["title"], "save")
    self.assertEqual(parsed["target_value"], 500.0)
    self.assertEqual(parsed["unit"], "dollars")
    self.assertEqual(parsed["target_date"], "2027-01-31")

  def test_open_ended_goal_has_no_target(self):
    parsed = parse_goal_input("run a marathon by June 30")
    self.assertEqual(parsed["title"], "run a marathon")
    self.assertIsNone(parsed["target_value"])
    self.assertTrue(parsed["target_date"].endswith("-06-30"))

  def test_empty_input(self):
    self.assertEqual(parse_goal_input("")["title"], "")


class ServiceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    self.service = GoalService(GoalsConfig(db=str(self.base / "goals.db")))
    self.addCleanup(self.service.close)

  def test_crud_progress_and_dates(self):
    due = date.today() + timedelta(days=30)
    goal = self.service.add(
        "Read 12 books", target_value=12, unit="books", target_date=due
    )
    self.assertEqual(goal.current_value, 0)
    self.assertEqual(goal.progress_percent, 0)
    self.assertIn("0/12 books", format_goal(goal))
    self.assertIn("Target date", format_goal_detail(goal))

    goal = self.service.progress(goal.id, 3)
    self.assertEqual(goal.current_value, 3)
    self.assertEqual(goal.progress_percent, 25)
    goal = self.service.progress(goal.id, 2, add=True)
    self.assertEqual(goal.current_value, 5)

    goal = self.service.update(goal.id, title="Read more books")
    self.assertEqual(goal.title, "Read more books")
    goal = self.service.update(goal.id, target_date="")
    self.assertIsNone(goal.target_date)

    done = self.service.complete(goal.id)
    self.assertEqual(done.status, "completed")
    self.assertIn("completed", format_goal(done))
    reopened = self.service.reopen(goal.id)
    self.assertEqual(reopened.status, "active")
    self.assertEqual(reopened.completed_at, "")

    removed = self.service.delete(goal.id)
    self.assertEqual(removed.id, goal.id)
    self.assertIsNone(self.service.get(goal.id))

  def test_open_ended_goal_and_validation(self):
    goal = self.service.add("Run a marathon")
    self.assertIsNone(goal.target_value)
    self.assertIsNone(goal.progress_percent)
    self.assertIn("no numeric target", format_goal_detail(goal))

    with self.assertRaises(GoalError):
      self.service.add("   ")
    with self.assertRaises(GoalError):
      self.service.add("Bad target", target_value=0)
    with self.assertRaises(GoalError):
      self.service.add("Bad date", target_date="someday")
    with self.assertRaises(GoalError):
      self.service.progress(999, 1)
    with self.assertRaises(GoalError):
      self.service.progress(goal.id, -1)

  def test_limit_and_list_order(self):
    service = GoalService(
        GoalsConfig(db=str(self.base / "limited.db"), max_goals=2)
    )
    self.addCleanup(service.close)
    first = service.add(
        "Later", target_value=1, target_date=date.today() + timedelta(days=5)
    )
    service.complete(first.id)
    second = service.add("Now")
    goals = service.list(status=None)
    self.assertEqual([goal.id for goal in goals], [second.id, first.id])
    with self.assertRaises(GoalError):
      service.add("Third")


class IntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_goal_intents(self):
    cases = {
        "add a goal to read 12 books by December 31": "goal_add",
        "set a goal to save 500 dollars": "goal_add",
        "list my goals": "goal_list",
        "what are my goals": "goal_list",
        "show goal 2": "goal_show",
        "goal 2 progress 5": "goal_progress",
        "update goal 2 to 5": "goal_progress",
        "add 2 to goal 2": "goal_progress",
        "complete goal 2": "goal_complete",
        "goal 2 done": "goal_complete",
        "reopen goal 2": "goal_reopen",
        "remove goal 2": "goal_delete",
    }
    for text, name in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, name, text)

  def test_entities_and_commands(self):
    intent = self.router.route("add a goal to read 12 books by December 31")
    self.assertEqual(intent.entities["title"], "read")
    self.assertEqual(intent.entities["target_value"], 12.0)
    self.assertEqual(intent.entities["unit"], "books")
    self.assertEqual(self.router.route("/goals").name, "goals")
    self.assertEqual(self.router.route("/goal").name, "goal")


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
            base.goals,
            enabled=enabled,
            db=str(self.base / "goals.db"),
        ),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    return assistant

  def test_commands_and_natural_language(self):
    assistant = self.make()
    answer = assistant.reply("add a goal to read 12 books by December 31")
    self.assertIn("Goal #1", answer)
    self.assertIn("12 books", answer)
    self.assertIn("read", assistant.reply("/goal list"))
    self.assertIn("0/12", assistant.reply("show goal 1"))
    self.assertIn("5/12", assistant.reply("/goal progress 1 5"))
    self.assertIn("7/12", assistant.reply("/goal increment 1 2"))
    self.assertIn("completed", assistant.reply("/goal complete 1"))
    self.assertIn("completed", assistant.reply("/goal list all"))
    self.assertIn("reopened", assistant.reply("/goal reopen 1"))
    self.assertIn("updated", assistant.reply("/goal edit 1 target 20"))
    self.assertIn("No target date", assistant.reply("/goal edit 1 clear due"))
    self.assertIn("removed", assistant.reply("/goal remove 1"))
    self.assertEqual(
        assistant.reply("/goal list"), "You have no active goals, sir."
    )

  def test_natural_delete_requires_confirmation_and_uses_tool(self):
    assistant = self.make(tools=True)
    assistant.reply("add a goal to save 500 dollars")
    pending = assistant.reply("remove goal 1")
    self.assertIn("Delete goal #1", pending)
    self.assertIn("confirm", pending.lower())
    self.assertEqual(assistant._pending_tool[0], "goal_delete")
    self.assertIsNotNone(assistant._get_goals().get(1))
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_goals().get(1))

  def test_natural_delete_can_be_cancelled(self):
    assistant = self.make(tools=True)
    assistant.reply("add a goal to save 500 dollars")
    self.assertIn("confirm", assistant.reply("delete goal 1").lower())
    self.assertIn("cancelled", assistant.reply("no").lower())
    self.assertIsNotNone(assistant._get_goals().get(1))

  def test_natural_delete_works_without_tools(self):
    assistant = self.make(tools=False)
    assistant.reply("add a goal to save 500 dollars")
    self.assertIn("confirm", assistant.reply("remove goal 1").lower())
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_goals().get(1))

  def test_disabled_goals(self):
    assistant = self.make(enabled=False)
    self.assertEqual(
        assistant.reply("/goals"), "Goal tracking is disabled, sir."
    )
    self.assertEqual(
        assistant.reply("add a goal to read"),
        "Goal tracking is disabled, sir.",
    )


class GoalToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    settings = load_settings()
    self.settings = dataclasses.replace(
        settings,
        goals=dataclasses.replace(
            settings.goals, db=str(self.base / "goals.db")
        ),
        tools=dataclasses.replace(settings.tools, root=str(self.base)),
    )
    self.service = GoalService(self.settings.goals)
    self.addCleanup(self.service.close)
    self.registry = build_registry(
        self.settings, goals_service=self.service
    )

  def test_tools_are_registered_with_permissions(self):
    names = set(self.registry.names())
    for name in {
        "goal_add",
        "goal_list",
        "goal_show",
        "goal_progress",
        "goal_complete",
        "goal_reopen",
        "goal_update",
        "goal_delete",
    }:
      self.assertIn(name, names)
    self.assertTrue(self.registry.requires_confirmation("goal_delete"))
    self.assertFalse(self.registry.requires_confirmation("goal_progress"))

  def test_add_list_progress_complete_reopen_update_and_delete_tools(self):
    added = self.registry.execute(
        "goal_add",
        {
            "title": "Read",
            "target": "12",
            "unit": "books",
            "due": "December 31",
        },
    )
    self.assertTrue(added.ok, added.text)
    goal_id = added.data["id"]
    self.assertEqual(added.data["target_value"], 12.0)
    self.assertTrue(added.data["target_date"].endswith("-12-31"))

    listed = self.registry.execute("goal_list")
    self.assertTrue(listed.ok, listed.text)
    self.assertEqual(listed.data["count"], 1)

    shown = self.registry.execute("goal_show", {"id": goal_id})
    self.assertTrue(shown.ok, shown.text)
    self.assertIn("Progress", shown.text)

    progress = self.registry.execute(
        "goal_progress", {"id": goal_id, "value": "5"}
    )
    self.assertTrue(progress.ok, progress.text)
    self.assertEqual(progress.data["current_value"], 5.0)
    self.assertEqual(progress.data["progress_percent"], 42)

    complete = self.registry.execute("goal_complete", {"id": goal_id})
    self.assertTrue(complete.ok, complete.text)
    self.assertEqual(complete.data["status"], "completed")

    reopened = self.registry.execute("goal_reopen", {"id": goal_id})
    self.assertTrue(reopened.ok, reopened.text)
    self.assertEqual(reopened.data["status"], "active")

    updated = self.registry.execute(
        "goal_update", {"id": goal_id, "target": "20"}
    )
    self.assertTrue(updated.ok, updated.text)
    self.assertEqual(updated.data["target_value"], 20.0)

    deleted = self.registry.execute("goal_delete", {"id": goal_id})
    self.assertTrue(deleted.ok, deleted.text)
    self.assertIsNone(self.service.get(goal_id))

  def test_tool_validation(self):
    missing = self.registry.execute("goal_add", {})
    self.assertFalse(missing.ok)
    self.assertIn("missing argument", missing.text)

    bad_type = self.registry.execute("goal_show", {"id": "one"})
    self.assertFalse(bad_type.ok)
    self.assertIn("must be int", bad_type.text)

    empty = self.registry.execute("goal_add", {"title": "   "})
    self.assertFalse(empty.ok)
    self.assertIn("needs a title", empty.text)

    bad_target = self.registry.execute(
        "goal_add", {"title": "Read", "target": "zero"}
    )
    self.assertFalse(bad_target.ok)
    self.assertIn("must be a number", bad_target.text)


class ConfigTests(unittest.TestCase):
  def test_invalid_goal_limit_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[goals]\nmax_goals = 0\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


class NoModelClient:
  """Fail if deterministic goal request reaches the model."""

  def chat(self, *args, **kwargs):
    raise AssertionError("the model must not be called")


if __name__ == "__main__":
  unittest.main()
