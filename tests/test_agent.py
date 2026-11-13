"""Feature set 4.15 tests: constrained autonomous task agent.

The goal planner is deterministic and only selects registered tools. These
tests use temporary folders and the real sandboxed registry; no model,
microphone or desktop action is required.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.agent import GoalAgent
from core.assistant import Assistant
from core.config import load_settings
from core.intents import IntentRouter
from tools import build_registry


class FakeClient:
  def __init__(self):
    self.calls = []

  def chat(self, messages, max_tokens=None, temperature=None):
    self.calls.append({"messages": messages, "max_tokens": max_tokens})
    return SimpleNamespace(
        content="Model answer.",
        total_ms=1.0,
        completion_tokens=1,
        generated_tokens_per_second=1.0,
        finish_reason="stop",
    )

  def close(self):
    pass


class GoalAgentTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    self.downloads = self.root / "Downloads"
    self.downloads.mkdir()
    (self.downloads / "report.pdf").write_text("report", encoding="utf-8")
    (self.downloads / "photo.jpg").write_text("photo", encoding="utf-8")
    base = load_settings()
    self.settings = dataclasses.replace(
        base,
        tools=dataclasses.replace(base.tools, root=str(self.root)),
    )
    self.registry = build_registry(self.settings)
    self.agent = GoalAgent(
        self.registry,
        self.settings.tools.allowed_apps,
        self.settings.tools.allowed_commands,
        self.settings.tasks.max_steps,
    )

  def test_system_goal_selects_safe_tools(self):
    plan = self.agent.plan("check my system")
    self.assertEqual(plan.kind, "system_check")
    self.assertEqual(
        [step.tool for step in plan.steps],
        ["system_info", "processes"],
    )
    self.assertTrue(plan.steps[1].parallel)
    self.assertEqual(plan.context["role"], "main")
    self.assertIn("system health", plan.summary)

  def test_inventory_and_search_goals(self):
    inventory = self.agent.plan("take inventory of my downloads")
    self.assertEqual(inventory.kind, "inventory")
    self.assertEqual(inventory.steps[0].tool, "list_files")
    self.assertEqual(inventory.steps[0].arguments["path"], "Downloads")
    self.assertEqual(inventory.context["role"], "file")

    search = self.agent.plan("find report.pdf in my downloads")
    self.assertEqual(search.kind, "search")
    self.assertEqual(search.steps[0].tool, "search_files")
    self.assertEqual(search.steps[0].arguments["name"], "report.pdf")
    self.assertEqual(search.steps[0].arguments["path"], "Downloads")

  def test_folder_goal_is_verified(self):
    plan = self.agent.plan("create a folder named drafts")
    self.assertEqual(plan.kind, "folder")
    self.assertEqual(len(plan.steps), 2)
    self.assertEqual(plan.steps[1].tool, "list_files")
    self.assertEqual(plan.steps[1].verify, {"contains": ["drafts/"]})
    self.assertEqual(plan.steps[1].depends_on, ("create_folder",))

  def test_existing_file_actions_are_delegated(self):
    plan = self.agent.plan(
        "create a folder named drafts and in that folder write hello"
        " to file notes.txt"
    )
    self.assertEqual(plan.kind, "file_plan")
    self.assertEqual(
        [step.tool for step in plan.steps],
        ["create_folder", "write_file"],
    )
    self.assertEqual(plan.steps[1].depends_on, ("agent_file_01",))

  def test_compound_goal_chains_stages(self):
    plan = self.agent.plan(
        "check my system then take inventory of Downloads"
    )
    self.assertEqual(plan.kind, "compound")
    self.assertEqual(len(plan.steps), 3)
    self.assertEqual(plan.context["stages"], 2)
    inventory = plan.steps[-1]
    self.assertEqual(inventory.tool, "list_files")
    self.assertEqual(
        set(inventory.depends_on),
        {"stage1_system_info", "stage1_top_processes"},
    )
    self.assertEqual(plan.context["roles"], ["main", "file"])
    self.assertIn("system health", plan.summary)
    self.assertIn("inventory of Downloads", plan.summary)

  def test_compound_file_and_folder_goal_chains_verification(self):
    plan = self.agent.plan(
        "create a folder named drafts then take inventory of Downloads"
    )
    self.assertEqual(plan.kind, "compound")
    self.assertEqual(
        [step.key for step in plan.steps],
        ["stage1_create_folder", "stage1_verify_folder", "stage2_inventory"],
    )
    self.assertEqual(
        plan.steps[-1].depends_on,
        ("stage1_verify_folder",),
    )

  def test_organize_goal_uses_existing_planner(self):
    plan = self.agent.plan("organize my downloads folder")
    self.assertEqual(plan.kind, "organize")
    self.assertEqual(plan.context["files"], 2)
    self.assertGreater(len(plan.steps), 3)

  def test_unsupported_goal_returns_none(self):
    self.assertIsNone(self.agent.plan("make me a sandwich"))


class AgentAssistantTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    self.downloads = self.root / "Downloads"
    self.downloads.mkdir()
    (self.downloads / "a.txt").write_text("a", encoding="utf-8")
    base = load_settings()
    self.settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(
            base.conversation,
            enabled=False,
            history_db=str(self.root / "history.db"),
        ),
        memory=dataclasses.replace(base.memory, enabled=False),
        tools=dataclasses.replace(base.tools, root=str(self.root)),
        tasks=dataclasses.replace(
            base.tasks,
            db=str(self.root / "tasks.db"),
            retry_delay_s=0.0,
            parallel_workers=1,
        ),
    )

  def make(self):
    client = FakeClient()
    assistant = Assistant(self.settings, client=client)
    self.addCleanup(assistant.close)
    return assistant, client

  def test_agent_goal_is_planned_and_runs_after_confirmation(self):
    assistant, client = self.make()
    prompt = assistant.reply(
        "as an agent, check my system then take inventory of Downloads"
    )
    self.assertIn("Task #1", prompt)
    self.assertIn("system health", prompt)
    self.assertIn("inventory of Downloads", prompt)
    done = assistant.reply("yes")
    self.assertIn("completed", done.lower())
    detail = assistant.reply("/tasks show 1")
    self.assertIn("3/3", detail)
    self.assertIn("Agent role: main", detail)
    self.assertEqual(client.calls, [])

  def test_tasks_agent_command(self):
    assistant, _client = self.make()
    prompt = assistant.reply("/tasks agent find a.txt in Downloads")
    self.assertIn("find a.txt under Downloads", prompt)
    done = assistant.reply("confirm")
    self.assertIn("find a.txt under Downloads", done)
    self.assertIn("completed", done.lower())

  def test_unsupported_goal_is_not_stored(self):
    assistant, client = self.make()
    reply = assistant.reply("as an agent, make me a sandwich")
    self.assertIn("I can plan", reply)
    self.assertIn("no tasks", assistant.reply("/tasks").lower())
    self.assertEqual(client.calls, [])

  def test_agent_intent(self):
    intent = IntentRouter().route("as an agent, check my system")
    self.assertEqual(intent.name, "task_plan")
    self.assertEqual(intent.entities["request"], "check my system")


if __name__ == "__main__":
  unittest.main()
