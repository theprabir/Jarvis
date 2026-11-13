"""Feature set 4.16 tests: logical agent roles on one local model."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.assistant import Assistant
from core.config import load_settings
from core.intents import Intent
from core.roles import ROLES, RoleRouter


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


class RoleRouterTests(unittest.TestCase):
  def setUp(self):
    self.router = RoleRouter()

  def test_all_roles_are_logical_configurations(self):
    self.assertEqual(
        set(ROLES),
        {
            "main",
            "research",
            "coding",
            "productivity",
            "file",
            "memory",
            "verification",
        },
    )
    self.assertEqual(self.router.get("coding").label, "Coding agent")
    self.assertIn("Role: Coding agent.", self.router.get("coding").system_prompt)

  def test_explicit_role_phrases(self):
    cases = (
        ("use the research agent to compare sources", "research"),
        ("ask the coding agent to refactor a parser", "coding"),
        ("as a file agent, inventory Downloads", "file"),
        ("verification agent: check the result", "verification"),
        ("/role is not part of this parser", None),
    )
    for text, expected in cases:
      with self.subTest(text=text):
        selection = self.router.parse_explicit(text)
        if expected is None:
          self.assertIsNone(selection)
        else:
          self.assertEqual(selection.role.name, expected)
          self.assertTrue(selection.explicit)
          self.assertTrue(selection.request)

  def test_keyword_selection(self):
    cases = (
        ("help me debug this Python traceback", "coding"),
        ("organize my downloads folder", "file"),
        ("plan my day and priorities", "productivity"),
        ("what do you remember about my preferences", "memory"),
        ("verify the result of the task", "verification"),
        ("research the latest news with sources", "research"),
        ("tell me a short story", "main"),
    )
    for text, expected in cases:
      with self.subTest(text=text):
        self.assertEqual(self.router.select(text).role.name, expected)

  def test_intent_mapping(self):
    cases = (
        (Intent("web_search", 1.0), "research"),
        (Intent("developer_debug", 1.0), "coding"),
        (Intent("todo_add", 1.0), "productivity"),
        (Intent("move_path", 1.0), "file"),
        (Intent("remember_memory", 1.0), "memory"),
        (Intent("conversation", 1.0), "main"),
    )
    for intent, expected in cases:
      with self.subTest(intent=intent.name):
        self.assertEqual(self.router.select("...", intent).role.name, expected)

  def test_role_help_and_detail(self):
    help_text = self.router.help_text()
    for name in ROLES:
      self.assertIn(name, help_text)
    detail = self.router.get("memory").detail()
    self.assertIn("Memory agent", detail)
    self.assertIn("use the memory agent", detail)


class AssistantRoleTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
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
    self.client = FakeClient()
    self.assistant = Assistant(self.settings, client=self.client)
    self.addCleanup(self.assistant.close)

  def test_roles_command_lists_every_role(self):
    reply = self.assistant.reply("/roles")
    for name in ROLES:
      self.assertIn(name, reply)
    self.assertEqual(self.client.calls, [])

  def test_role_detail_and_unknown_role(self):
    self.assertIn("Coding agent", self.assistant.reply("/role coding"))
    self.assertIn("Unknown role", self.assistant.reply("/role nope"))

  def test_explicit_role_adds_its_prompt_to_the_model_turn(self):
    reply = self.assistant.reply(
        "use the coding agent to discuss tradeoffs in parser design"
    )
    self.assertEqual(reply, "Model answer.")
    messages = self.client.calls[-1]["messages"]
    prompts = [
        message["content"]
        for message in messages
        if message["role"] == "system"
    ]
    self.assertIn(ROLES["coding"].system_prompt, prompts)
    self.assertEqual(self.assistant._last_role, "coding")

  def test_role_command_can_force_a_role(self):
    reply = self.assistant.reply(
        "/role verification discuss whether the reported result is reliable"
    )
    self.assertEqual(reply, "Model answer.")
    prompts = [
        message["content"]
        for message in self.client.calls[-1]["messages"]
        if message["role"] == "system"
    ]
    self.assertIn(ROLES["verification"].system_prompt, prompts)
    self.assertEqual(self.assistant._last_role, "verification")

  def test_main_turn_does_not_add_a_specialist_prompt(self):
    self.assistant.reply("Tell me a short story.")
    prompts = [
        message["content"]
        for message in self.client.calls[-1]["messages"]
        if message["role"] == "system"
    ]
    for role in ROLES.values():
      if role.name != "main":
        self.assertNotIn(role.system_prompt, prompts)


if __name__ == "__main__":
  unittest.main()
