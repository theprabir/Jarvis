"""Feature set 4.8 tests: automatic memory, episodes and ranking.

Everything is offline and deterministic: a fake model supplies replies while
the learner extracts candidates from the user text, exactly as in a real turn.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core.assistant import Assistant
from core.config import load_settings
from core.learning import (
    ShortTermMemory,
    episode_summary,
    episode_topics,
    extract_candidates,
    importance_for,
    subject_for,
)
from core.memory import MemoryStore


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


def make_assistant(
    tmp, auto_learn=True, replies=("Noted.",), conversation_enabled=False
):
  base = load_settings()
  settings = dataclasses.replace(
      base,
      conversation=dataclasses.replace(
          base.conversation,
          enabled=conversation_enabled,
          history_db=str(Path(tmp) / "history.db"),
      ),
      memory=dataclasses.replace(
          base.memory,
          enabled=True,
          auto_learn=auto_learn,
          db=str(Path(tmp) / "assistant.db"),
      ),
  )
  client = FakeClient(list(replies))
  assistant = Assistant(settings, client=client)
  return assistant, client


class ExtractionTests(unittest.TestCase):
  def test_stable_statements_become_candidates(self):
    cases = {
        "My name is Alex.": ("name", "preference", 0.95),
        "I prefer dark mode.": (None, "preference", 0.85),
        "My favorite color is blue.": ("favorite:color", "preference", 0.9),
        "I live in Kolkata.": ("residence", "preference", 0.9),
        "I work at Acme Corp.": ("employer", "preference", 0.9),
        "I work as an engineer.": ("job", "preference", 0.9),
        "I am 34 years old.": ("age", "preference", 0.95),
        "My projects folder is C:/dev.": (
            "my:projects folder", "folder", 0.9
        ),
    }
    for text, (subject, category, confidence) in cases.items():
      candidate = extract_candidates(text)[0]
      self.assertEqual(candidate.subject, subject, text)
      self.assertEqual(candidate.category, category, text)
      self.assertAlmostEqual(candidate.confidence, confidence)
      self.assertNotIn("\n", candidate.content)

  def test_questions_commands_and_small_talk_are_ignored(self):
    for text in (
        "Do you like tea?",
        "What is my name?",
        "/memory list",
        "I like you",
        "Hello",
        "Thanks!",
    ):
      self.assertEqual(extract_candidates(text), [], text)

  def test_importance_reflects_category_and_cues(self):
    self.assertGreaterEqual(
        importance_for("preference", "I always use dark mode"), 4
    )
    self.assertLessEqual(importance_for("fact", "the sky is blue"), 2)
    self.assertEqual(
        importance_for("preference", "I always use dark mode"), 5
    )

  def test_subject_extraction(self):
    self.assertEqual(subject_for("my name is Alex"), "name")
    self.assertEqual(subject_for("I live in Pune"), "residence")
    self.assertEqual(subject_for("I like tea"), None)


class EpisodeTests(unittest.TestCase):
  def test_substantive_exchange_gets_a_summary(self):
    summary = episode_summary(
        "I really enjoy building small local assistant projects every evening."
    )
    self.assertTrue(summary.startswith("Discussed: "))
    self.assertLessEqual(len(summary), 190)

  def test_question_summary_is_labelled(self):
    summary = episode_summary(
        "What did we decide about the quarterly budget planning session?"
    )
    self.assertTrue(summary.startswith("Asked: "))

  def test_small_talk_is_not_an_episode(self):
    self.assertIsNone(episode_summary("hi"))
    self.assertIsNone(episode_summary("/memory list"))

  def test_topics_drop_stopwords(self):
    topics = episode_topics("please plan the quarterly budget review meeting")
    self.assertIn("quarterly", topics)
    self.assertIn("budget", topics)
    self.assertNotIn("please", topics)


class ShortTermTests(unittest.TestCase):
  def test_buffer_is_bounded_and_summarised(self):
    buffer = ShortTermMemory(3)
    for index in range(5):
      buffer.add(f"message {index}", "ok")
    self.assertEqual(len(buffer), 3)
    self.assertEqual(buffer.recent(1)[0][0], "message 4")
    self.assertIn("message 4", buffer.summary())
    buffer.clear()
    self.assertEqual(buffer.summary(), "")


class StoreLearningTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.store = make_store(self.tmp.name)
    self.addCleanup(self.store.close)

  def future(self, days=180):
    stamp = datetime.now(timezone.utc) + timedelta(days=days)
    return stamp.isoformat(timespec="seconds")

  def test_learned_memory_persists_across_reopen(self):
    self.store.add(
        "preference",
        "I prefer dark mode",
        importance=4,
        source="learned",
        confidence=0.85,
        expires_at=self.future(),
    )
    self.store.close()
    reopened = make_store(self.tmp.name)
    self.addCleanup(reopened.close)
    rows = reopened.list(source="learned")
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["confidence"], 0.85)
    self.assertEqual(rows[0]["source"], "learned")

  def test_conflict_expires_the_old_memory_and_links_it(self):
    first, _ = self.store.add(
        "preference", "my name is Alex", subject="name"
    )
    second, _ = self.store.add(
        "preference",
        "my name is Sam",
        source="learned",
        confidence=0.95,
        subject="name",
        expires_at=self.future(),
    )
    active = self.store.list()
    self.assertEqual(len(active), 1)
    self.assertEqual(active[0]["content"], "my name is Sam")
    old = self.store.get(first)
    self.assertEqual(old["superseded_by"], second)
    self.assertIsNotNone(old["expires_at"])
    self.assertEqual(self.store.find_by_subject("name")["id"], second)
    self.assertEqual(len(self.store.history("name")), 2)

  def test_expired_memory_is_hidden(self):
    past = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat(timespec="seconds")
    self.store.add("fact", "temporary fact", expires_at=past)
    self.assertEqual(self.store.list(), [])
    self.assertEqual(self.store.search("temporary"), [])
    self.assertIsNone(self.store.find_by_subject("temporary"))

  def test_ranking_prefers_important_preferences(self):
    self.store.add(
        "fact", "I use Python for short scripts", importance=1
    )
    self.store.add(
        "preference",
        "I always use Python for automation",
        importance=5,
    )
    results = self.store.search("python")
    self.assertEqual(results[0]["category"], "preference")
    self.assertGreater(
        self.store.ranked("python")[0]["importance"], 1
    )

  def test_context_block_combines_memories_and_episodes(self):
    self.store.add("preference", "my name is Alex", subject="name")
    self.store.add_episode(
        "session-1", "Discussed: planning the budget", "budget planning"
    )
    block = self.store.context_block(query="budget")
    self.assertIn("Known about the user:", block)
    self.assertIn("my name is Alex", block)
    self.assertIn("Recent events:", block)
    self.assertIn("planning the budget", block)
    self.assertLessEqual(len(block), 700)

  def test_context_block_can_exclude_episodes(self):
    self.store.add_episode("s1", "Discussed: planning the budget", "budget")
    block = self.store.context_block(episode_limit=0)
    self.assertEqual(block, "")

  def test_context_block_skips_facts_already_in_the_transcript(self):
    self.store.add("preference", "my name is Alex", subject="name")
    self.store.add("fact", "the office is in Kolkata", subject="office")
    block = self.store.context_block(
        query="alex kolkata",
        exclude_text="My name is Alex. The office is closed today.",
    )
    self.assertNotIn("my name is Alex", block)
    self.assertIn("the office is in Kolkata", block)

  def test_context_block_stays_within_budget(self):
    for index in range(10):
      self.store.add(
          "fact", f"fact {index} " + ("x" * 80), importance=3
      )
    block = self.store.context_block(limit=10, max_chars=300)
    self.assertLessEqual(len(block), 300)

  def test_episodes_can_be_ranked_and_cleared(self):
    self.store.add_episode("s1", "Discussed: budget review", "budget review")
    self.store.add_episode("s1", "Discussed: holiday plans", "holiday")
    self.assertEqual(self.store.episode_count(), 2)
    recent = self.store.recent_episodes(limit=1, query="holiday")
    self.assertEqual(recent[0]["summary"], "Discussed: holiday plans")
    recent = self.store.recent_episodes(limit=1, query="budget")
    self.assertIn("budget", recent[0]["summary"])
    self.assertEqual(self.store.clear_episodes(), 2)
    self.assertEqual(self.store.episode_count(), 0)


class AssistantLearningTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)

  def make(
      self, auto_learn=True, replies=("Noted.",), conversation_enabled=False
  ):
    assistant, client = make_assistant(
        self.tmp.name,
        auto_learn=auto_learn,
        replies=replies,
        conversation_enabled=conversation_enabled,
    )
    self.addCleanup(assistant.close)
    return assistant, client

  def test_learns_and_lists_a_preference(self):
    assistant, _client = self.make()
    assistant.reply("My name is Alex.")
    learned = assistant._memory.list(source="learned")
    self.assertEqual(len(learned), 1)
    self.assertEqual(learned[0]["content"], "My name is Alex")
    self.assertEqual(learned[0]["source"], "learned")
    self.assertIsNotNone(learned[0]["expires_at"])
    listing = assistant.reply("what have you learned")
    self.assertIn("My name is Alex", listing)
    self.assertIn("learned", listing)

  def test_learned_memory_survives_a_restart(self):
    assistant, _client = self.make()
    assistant.reply("I prefer dark mode.")
    assistant.close()
    second, _client = self.make()
    contents = [
        item["content"]
        for item in second._memory.list(source="learned")
    ]
    self.assertIn("I prefer dark mode", contents)

  def test_auto_learning_can_be_disabled(self):
    assistant, _client = self.make(auto_learn=False)
    assistant.reply("My name is Alex.")
    self.assertEqual(assistant._memory.list(source="learned"), [])

  def test_questions_and_small_talk_are_not_stored(self):
    assistant, _client = self.make()
    assistant.reply("Do you like tea?")
    assistant.reply("hi")
    self.assertEqual(assistant._memory.list(source="learned"), [])
    self.assertEqual(assistant._memory.episode_count(), 0)

  def test_substantive_turn_creates_an_episode(self):
    assistant, _client = self.make()
    assistant.reply(
        "I really enjoy building small local assistant projects every evening."
    )
    self.assertEqual(assistant._memory.episode_count(), 1)
    self.assertIn(
        "Discussed:",
        assistant._memory.recent_episodes(limit=1)[0]["summary"],
    )

  def test_explicit_memory_is_not_overwritten_by_learning(self):
    assistant, _client = self.make()
    assistant.reply("remember my name is Alex")
    assistant.reply("My name is Sam.")
    self.assertEqual(
        assistant._memory.find_by_subject("name")["content"],
        "my name is Alex",
    )

  def test_explicit_correction_replaces_the_old_value(self):
    assistant, _client = self.make()
    assistant.reply("remember my name is Alex")
    reply = assistant.reply("remember my name is Sam")
    self.assertIn("Updated", reply)
    self.assertEqual(
        assistant._memory.find_by_subject("name")["content"],
        "my name is Sam",
    )
    history = assistant._memory.history("name")
    self.assertEqual(len(history), 2)
    self.assertEqual(history[0]["superseded_by"], history[1]["id"])

  def test_explain_shows_provenance(self):
    assistant, _client = self.make()
    assistant.reply("My name is Alex.")
    memory_id = assistant._memory.list(source="learned")[0]["id"]
    detail = assistant.reply(f"/memory explain {memory_id}")
    self.assertIn("Source: learned", detail)
    self.assertIn("confidence", detail)
    self.assertIn("Expires:", detail)

  def test_episode_controls_and_confirmation(self):
    assistant, _client = self.make()
    assistant.reply(
        "I really enjoy building small local assistant projects every evening."
    )
    listing = assistant.reply("what did we talk about")
    self.assertIn("Recent events", listing)
    warning = assistant.reply("/memory episodes clear")
    self.assertIn("deletes every episodic summary", warning)
    self.assertEqual(assistant._memory.episode_count(), 1)
    done = assistant.reply("/memory episodes clear confirm")
    self.assertIn("Deleted", done)
    self.assertEqual(assistant._memory.episode_count(), 0)

  def test_commands_are_not_learned(self):
    assistant, _client = self.make()
    assistant.reply("/memory list")
    self.assertEqual(assistant._memory.episode_count(), 0)
    self.assertEqual(assistant._memory.list(source="learned"), [])

  def test_transcript_replaces_recent_episodes_in_context(self):
    assistant, client = self.make(conversation_enabled=True)
    assistant.reply("My name is Alex.")
    assistant.reply("Tell me about the ocean.")
    messages = client.calls[-1]["messages"]
    self.assertFalse(any(
        "Recent events" in item["content"] for item in messages
    ))
    # The name is already verbatim in the transcript, so it is not repeated.
    self.assertFalse(any(
        "Known about the user" in item["content"] for item in messages
    ))

  def test_session_context_reaches_the_prompt(self):
    assistant, client = self.make()
    assistant.reply("My name is Alex.")
    assistant.reply("Tell me about the ocean.")
    messages = client.calls[-1]["messages"]
    self.assertNotIn("Known about the user", messages[0]["content"])
    context = messages[-2]["content"]
    self.assertIn("Known about the user", context)
    self.assertIn("My name is Alex", context)
    self.assertIn("Session so far", context)


if __name__ == "__main__":
  unittest.main()
