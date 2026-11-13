"""Feature set 4.6 tests: explicit memories, search, commands and prompt use.

Temporary SQLite files and a fake model keep the suite offline and fast.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.assistant import Assistant
from core.config import ConfigError, load_settings
from core.intents import IntentRouter
from core.memory import MemoryStore, MemoryStoreError, classify


class FakeClient:
  def __init__(self, replies=("Noted.",)):
    self.replies = list(replies)
    self.calls = []

  def chat(self, messages, max_tokens=None, temperature=None):
    self.calls.append({"messages": messages, "max_tokens": max_tokens})
    content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
    return SimpleNamespace(
        content=content,
        total_ms=10.0,
        completion_tokens=4,
        generated_tokens_per_second=5.0,
        finish_reason="stop",
    )


def make_store(tmp):
  return MemoryStore(Path(tmp) / "memory.db")


def make_assistant(tmp, replies=("Noted.",), memory=True):
  base = load_settings()
  settings = dataclasses.replace(
      base,
      conversation=dataclasses.replace(base.conversation, enabled=False),
      memory=dataclasses.replace(
          base.memory,
          enabled=memory,
          db=str(Path(tmp) / "assistant.db"),
      ),
  )
  client = FakeClient(list(replies))
  assistant = Assistant(settings, client=client)
  return assistant, client


class StoreTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.store = make_store(self.tmp.name)
    self.addCleanup(self.store.close)

  def test_add_get_list_and_count(self):
    memory_id, created = self.store.add("preference", "my name is Alex")
    self.assertTrue(created)
    self.assertEqual(self.store.count(), 1)
    item = self.store.get(memory_id)
    self.assertEqual(item["category"], "preference")
    self.assertEqual(item["content"], "my name is Alex")
    self.assertFalse(item["temporary"])
    self.assertEqual(len(self.store.list()), 1)

  def test_duplicate_persistent_memory_is_kept_once(self):
    first, created = self.store.add("fact", "the project is JARVIS")
    second, again = self.store.add("fact", "The project is JARVIS")
    self.assertTrue(created)
    self.assertFalse(again)
    self.assertEqual(first, second)
    self.assertEqual(self.store.count(), 1)

  def test_unknown_category_falls_back_to_fact(self):
    memory_id, _ = self.store.add("nonsense", "something")
    self.assertEqual(self.store.get(memory_id)["category"], "fact")

  def test_search_finds_content_and_reports_nothing(self):
    self.store.add("preference", "my name is Alex")
    self.store.add("fact", "the project is called JARVIS")
    found = self.store.search("alex")
    self.assertEqual([item["content"] for item in found],
                     ["my name is Alex"])
    self.assertEqual(self.store.search("banana"), [])

  def test_update_and_delete(self):
    memory_id, _ = self.store.add("fact", "the office is in Kolkata")
    self.assertTrue(self.store.update(memory_id, content="the office is in Pune"))
    self.assertIn("Pune", self.store.get(memory_id)["content"])
    self.assertTrue(self.store.update(memory_id, category="path"))
    self.assertEqual(self.store.get(memory_id)["category"], "path")
    self.assertTrue(self.store.delete(memory_id))
    self.assertIsNone(self.store.get(memory_id))
    self.assertFalse(self.store.delete(memory_id))

  def test_temporary_memories_do_not_survive_a_reopen(self):
    self.store.add("fact", "the meeting is at five", temporary=True)
    self.assertEqual(self.store.count(), 1)
    self.store.close()
    reopened = make_store(self.tmp.name)
    self.addCleanup(reopened.close)
    self.assertEqual(reopened.count(), 0)

  def test_expired_memories_are_hidden(self):
    self.store.add(
        "task", "old task", expires_at="2000-01-01T00:00:00+00:00"
    )
    self.assertEqual(self.store.list(), [])
    self.assertEqual(self.store.search("task"), [])

  def test_prompt_block_puts_preferences_first_and_stays_bounded(self):
    self.store.add("fact", "the project is JARVIS")
    self.store.add("preference", "my name is Alex")
    block = self.store.prompt_block(limit=6, max_chars=200)
    self.assertTrue(block.startswith("Known about the user:"))
    self.assertLess(block.index("my name is Alex"),
                    block.index("the project is JARVIS"))
    self.assertLessEqual(len(block), 220)

  def test_usage_is_counted_and_ranked(self):
    self.store.note_usage("app", "notepad")
    self.store.note_usage("app", "notepad")
    self.store.note_usage("app", "calc")
    rows = self.store.frequent("app")
    self.assertEqual(rows[0]["value"], "notepad")
    self.assertEqual(rows[0]["uses"], 2)
    self.assertEqual(len(rows), 2)

  def test_clear_can_target_temporary_memories(self):
    self.store.add("fact", "kept")
    self.store.add("fact", "short lived", temporary=True)
    self.assertEqual(self.store.clear(temporary_only=True), 1)
    self.assertEqual(self.store.count(), 1)

  def test_empty_memory_is_rejected(self):
    with self.assertRaises(MemoryStoreError):
      self.store.add("fact", "   ")


class ClassifyTests(unittest.TestCase):
  def test_categories_are_inferred(self):
    cases = {
        "my name is Alex": "preference",
        "I prefer dark mode": "preference",
        "C:/Users/ADMIN/Desktop": "path",
        "my projects folder is C:/dev": "folder",
        "https://example.com": "website",
        "finish the report task": "task",
        "the capital of Japan is Tokyo": "fact",
    }
    for content, expected in cases.items():
      self.assertEqual(classify(content), expected, content)


class ConfigTests(unittest.TestCase):
  def test_project_config_has_memory(self):
    memory = load_settings().memory
    self.assertTrue(memory.enabled)
    self.assertEqual(memory.inject_items, 6)
    self.assertTrue(memory.resolved_db.is_absolute())

  def test_inject_items_is_validated(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[memory]\ninject_items = 0\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


class IntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_remembering_phrases(self):
    intent = self.router.route("remember my name is Alex")
    self.assertEqual(intent.name, "remember_memory")
    self.assertEqual(intent.entities["category"], "preference")
    self.assertFalse(intent.entities["temporary"])

  def test_temporary_remembering(self):
    intent = self.router.route("remember for now that the meeting is at five")
    self.assertEqual(intent.name, "remember_memory")
    self.assertTrue(intent.entities["temporary"])
    self.assertIn("meeting", intent.entities["content"])

  def test_remember_at_the_end_of_a_sentence(self):
    intent = self.router.route("my name is Alex, remember that")
    self.assertEqual(intent.name, "remember_memory")
    self.assertIn("alex", intent.entities["content"].lower())

  def test_memory_questions(self):
    intent = self.router.route("what do you remember about alex")
    self.assertEqual(intent.name, "search_memory")
    self.assertEqual(intent.entities["query"], "alex")
    self.assertEqual(
        self.router.route("what do you remember").name, "list_memory"
    )
    self.assertEqual(
        self.router.route("show your memories").name, "list_memory"
    )
    intent = self.router.route("forget memory about alex")
    self.assertEqual(intent.name, "delete_memory")
    self.assertEqual(intent.entities["query"], "alex")

  def test_file_search_is_not_memory_search(self):
    self.assertEqual(
        self.router.route("search for notes in drafts").name, "search_files"
    )


class AssistantMemoryTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)

  def test_remember_and_recall_from_a_sentence(self):
    assistant, _client = make_assistant(self.tmp.name)
    self.addCleanup(assistant.close)
    saved = assistant.reply("remember my name is Alex")
    self.assertIn("Saved to memory", saved)
    listing = assistant.reply("what do you remember")
    self.assertIn("[1]", listing)
    self.assertIn("my name is Alex", listing)
    found = assistant.reply("what do you remember about alex")
    self.assertIn("my name is Alex", found)

  def test_temporary_memory_is_labelled(self):
    assistant, _client = make_assistant(self.tmp.name)
    self.addCleanup(assistant.close)
    assistant.reply("remember for now that the meeting is at five")
    listing = assistant.reply("what do you remember")
    self.assertIn("temporary", listing)

  def test_memories_reach_the_trailing_context(self):
    assistant, client = make_assistant(self.tmp.name)
    self.addCleanup(assistant.close)
    assistant.reply("remember my name is Alex")
    assistant.reply("hello")
    messages = client.calls[-1]["messages"]
    self.assertNotIn("Known about the user", messages[0]["content"])
    self.assertEqual(messages[-2]["role"], "system")
    self.assertIn("Known about the user", messages[-2]["content"])
    self.assertIn("my name is Alex", messages[-2]["content"])
    self.assertEqual(messages[-1], {"role": "user", "content": "hello"})

  def test_delete_by_description(self):
    assistant, _client = make_assistant(self.tmp.name)
    self.addCleanup(assistant.close)
    assistant.reply("remember the office is in Kolkata")
    reply = assistant.reply("forget memory about kolkata")
    self.assertIn("Forgotten", reply)
    self.assertEqual(assistant._memory.count(), 0)

  def test_memory_commands(self):
    assistant, _client = make_assistant(self.tmp.name)
    self.addCleanup(assistant.close)
    self.assertIn(
        "Saved", assistant.reply("/memory add preference I like tea")
    )
    listing = assistant.reply("/memory list")
    self.assertIn("I like tea", listing)
    self.assertIn("preference", listing)
    memory_id = assistant._memory.list()[0]["id"]
    self.assertIn(
        "Updated", assistant.reply(f"/memory edit {memory_id} I like coffee")
    )
    self.assertIn("I like coffee", assistant.reply("/memory search coffee"))
    self.assertIn(
        "Forgotten", assistant.reply(f"/memory delete {memory_id}")
    )
    self.assertEqual(assistant._memory.count(), 0)

  def test_clear_requires_confirmation(self):
    assistant, _client = make_assistant(self.tmp.name)
    self.addCleanup(assistant.close)
    assistant.reply("remember the office is in Kolkata")
    warning = assistant.reply("/memory clear")
    self.assertIn("deletes every saved memory", warning)
    self.assertEqual(assistant._memory.count(), 1)
    done = assistant.reply("/memory clear confirm")
    self.assertIn("Deleted", done)
    self.assertEqual(assistant._memory.count(), 0)

  def test_disabled_memory_says_so(self):
    assistant, _client = make_assistant(self.tmp.name, memory=False)
    self.addCleanup(assistant.close)
    reply = assistant.reply("what do you remember")
    self.assertIn("Memory is disabled", reply)


if __name__ == "__main__":
  unittest.main()
