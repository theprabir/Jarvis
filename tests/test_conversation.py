"""Feature set 4.2 tests: conversation context, compression, modes, history.

These run offline with a fake LLM client and temporary SQLite files.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from brain.errors import LLMConnectionError, LLMTimeoutError
from core.assistant import (
    CLARIFY_REPLY,
    FAILURE_REPLIES,
    HELP_TEXT,
    Assistant,
    SYSTEM_PROMPT,
)
from core.config import ConfigError, ConversationConfig, load_settings
from core.conversation import (
    MODE_INSTRUCTIONS,
    Conversation,
    needs_clarification,
)
from core.history import HistoryStore


def write_config(directory, body):
  path = Path(directory) / "settings.toml"
  path.write_text(body, encoding="utf-8")
  return path


class FakeClient:
  """Records requests and returns canned replies."""

  def __init__(self, replies=("Paris.",), error=None):
    self.replies = list(replies)
    self.error = error
    self.calls = []

  def chat(self, messages, max_tokens=None, temperature=None):
    self.calls.append({"messages": messages, "max_tokens": max_tokens})
    if self.error is not None:
      raise self.error
    content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
    return SimpleNamespace(
        content=content,
        total_ms=120.0,
        completion_tokens=4,
        generated_tokens_per_second=5.0,
    )


class ConversationConfigTests(unittest.TestCase):
  def test_project_config_has_conversation_defaults(self):
    conversation = load_settings().conversation
    self.assertTrue(conversation.enabled)
    self.assertEqual(conversation.mode, "short")
    self.assertEqual(conversation.max_turns, 8)
    self.assertEqual(conversation.short_max_tokens, 90)
    self.assertTrue(conversation.resolved_history_db.is_absolute())

  def test_invalid_mode_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = write_config(tmp, "[conversation]\nmode = \"poetic\"\n")
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_summarize_before_max_turns_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = write_config(
          tmp, "[conversation]\nmax_turns = 10\nsummarize_after = 5\n"
      )
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_unknown_key_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = write_config(tmp, "[conversation]\nmemory = true\n")
      with self.assertRaises(ConfigError):
        load_settings(path)


class ConversationTests(unittest.TestCase):
  def test_messages_carry_context_and_mode_instruction(self):
    conversation = Conversation(ConversationConfig(max_context_chars=1000))
    conversation.add("My name is Alex", "Noted, Alex.")
    messages = conversation.messages("BASE")
    self.assertEqual(messages[0]["role"], "system")
    self.assertIn("BASE", messages[0]["content"])
    self.assertIn(MODE_INSTRUCTIONS["short"], messages[0]["content"])
    self.assertEqual(
        [message["role"] for message in messages[1:]], ["user", "assistant"]
    )
    self.assertEqual(messages[1]["content"], "My name is Alex")

  def test_mode_changes_instruction_and_budget(self):
    conversation = Conversation(ConversationConfig())
    conversation.mode = "technical"
    messages = conversation.messages("BASE")
    self.assertIn(MODE_INSTRUCTIONS["technical"], messages[0]["content"])
    self.assertEqual(conversation.max_tokens(), 320)

  def test_reset_clears_turns_and_summary(self):
    conversation = Conversation(ConversationConfig())
    conversation.add("hello", "hi")
    conversation.summary = "earlier"
    conversation.reset()
    self.assertEqual(conversation.turns, [])
    self.assertEqual(conversation.summary, "")

  def test_context_budget_keeps_the_newest_turn(self):
    conversation = Conversation(ConversationConfig(max_context_chars=60))
    conversation.add("a" * 40, "b" * 40)
    conversation.add("c" * 40, "d" * 40)
    users = [
        message for message in conversation.messages("S")
        if message["role"] == "user"
    ]
    self.assertEqual(len(users), 1)
    self.assertEqual(users[0]["content"], "c" * 40)

  def test_summary_replaces_old_turns(self):
    conversation = Conversation(
        ConversationConfig(max_turns=1, summarize_after=2, max_context_chars=1000)
    )
    for index in range(3):
      conversation.add(f"u{index}", f"a{index}")

    changed = conversation.maybe_summarize(
        lambda transcript: f"SUMMARY of {len(transcript)} chars"
    )
    self.assertTrue(changed)
    self.assertEqual(len(conversation.turns), 1)
    self.assertIn("SUMMARY", conversation.summary)
    system = [
        message["content"]
        for message in conversation.messages("S")
        if message["role"] == "system"
    ]
    self.assertTrue(any("Summary of the earlier conversation" in text
                        for text in system))

  def test_failed_summarizer_falls_back_to_extract(self):
    conversation = Conversation(
        ConversationConfig(max_turns=1, summarize_after=1)
    )
    for index in range(2):
      conversation.add(f"u{index}", f"a{index}")
    self.assertTrue(conversation.maybe_summarize(lambda transcript: ""))
    self.assertIn("Earlier the user asked about", conversation.summary)

  def test_no_summary_below_threshold(self):
    conversation = Conversation(ConversationConfig())
    conversation.add("hello", "hi")
    self.assertFalse(conversation.maybe_summarize(lambda text: "x"))
    self.assertEqual(conversation.summary, "")

  def test_clarification_only_for_bare_references_without_history(self):
    self.assertTrue(needs_clarification("what about that?", False))
    self.assertTrue(needs_clarification("how big is it", False))
    self.assertFalse(needs_clarification("what about that?", True))
    self.assertFalse(
        needs_clarification("What is the capital of France?", False)
    )
    self.assertFalse(needs_clarification("tell me more", False))
    self.assertFalse(needs_clarification("", False))


class AssistantTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)

  def make(self, replies=("Paris.",), error=None, **overrides):
    options = {"history_db": str(Path(self.tmp.name) / "history.db")}
    options.update(overrides)
    settings = dataclasses.replace(
        load_settings(), conversation=ConversationConfig(**options)
    )
    client = FakeClient(replies, error)
    assistant = Assistant(settings, client=client)
    self.addCleanup(assistant.close)
    return assistant, client

  def test_follow_up_sends_previous_turn(self):
    assistant, client = self.make(replies=("Noted, Alex.", "Alex."))
    self.assertEqual(assistant.reply("My name is Alex"), "Noted, Alex.")
    assistant.reply("What is my name?")
    second = client.calls[1]["messages"]
    contents = [message["content"] for message in second]
    self.assertIn("My name is Alex", contents)
    self.assertIn("Noted, Alex.", contents)
    self.assertEqual(len(assistant.conversation.turns), 2)

  def test_mode_command_changes_budget(self):
    assistant, client = self.make()
    self.assertIn("Detailed answers enabled", assistant.reply("/mode detailed"))
    self.assertEqual(assistant.mode, "detailed")
    assistant.reply("Explain floats")
    self.assertEqual(client.calls[-1]["max_tokens"], 250)

  def test_mode_listing_and_unknown_mode(self):
    assistant, _ = self.make()
    self.assertIn("Current answer mode: short", assistant.reply("/mode"))
    self.assertIn("Unknown mode", assistant.reply("/mode poetic"))

  def test_new_resets_conversation_and_session(self):
    assistant, _ = self.make()
    assistant.reply("hello")
    session = assistant.session_id
    self.assertIn("new conversation", assistant.reply("/new"))
    self.assertEqual(assistant.conversation.turns, [])
    self.assertNotEqual(assistant.session_id, session)

  def test_history_command_lists_exchanges(self):
    assistant, _ = self.make()
    assistant.reply("first question")
    reply = assistant.reply("/history")
    self.assertIn("You: first question", reply)
    self.assertIn("Jarvis: Paris.", reply)

  def test_history_is_persisted_to_sqlite(self):
    assistant, _ = self.make()
    assistant.reply("remember the number 42")
    assistant.close()
    store = HistoryStore(Path(self.tmp.name) / "history.db")
    try:
      rows = store.recent()
    finally:
      store.close()
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["user"], "remember the number 42")

  def test_failure_is_reported_and_not_stored(self):
    assistant, _ = self.make(error=LLMConnectionError("down"))
    self.assertEqual(
        assistant.reply("hello"), FAILURE_REPLIES["connection"]
    )
    self.assertEqual(assistant.conversation.turns, [])
    self.assertEqual(assistant._store.count(), 0)

  def test_timeout_has_its_own_message(self):
    assistant, _ = self.make(error=LLMTimeoutError("slow"))
    self.assertEqual(assistant.reply("hello"), FAILURE_REPLIES["timeout"])

  def test_disabled_conversation_sends_only_current_message(self):
    assistant, client = self.make(enabled=False)
    assistant.reply("first")
    assistant.reply("second")
    self.assertEqual(len(client.calls[1]["messages"]), 2)
    self.assertEqual(assistant.conversation.turns, [])
    self.assertIsNone(assistant._store)

  def test_bare_reference_without_history_asks_for_clarification(self):
    assistant, client = self.make()
    self.assertEqual(assistant.reply("what about that?"), CLARIFY_REPLY)
    self.assertEqual(client.calls, [])

  def test_bare_reference_with_history_reaches_the_model(self):
    assistant, client = self.make()
    assistant.reply("The sky is blue.")
    assistant.reply("what about that?")
    self.assertEqual(len(client.calls), 2)

  def test_help_and_unknown_command(self):
    assistant, client = self.make()
    self.assertEqual(assistant.reply("/help"), HELP_TEXT)
    self.assertIn("Unknown command", assistant.reply("/explode"))
    self.assertEqual(client.calls, [])

  def test_summarization_runs_before_the_answer(self):
    assistant, client = self.make(
        replies=("Answer one.", "Answer two.", "Compact summary.", "Answer three."),
        max_turns=1,
        summarize_after=1,
    )
    assistant.reply("old question")
    assistant.reply("second question")
    # The third request first compresses the two stored turns.
    self.assertEqual(assistant.reply("third question"), "Answer three.")
    request = client.calls[-1]["messages"]
    system = [message["content"] for message in request
              if message["role"] == "system"]
    self.assertTrue(any("Compact summary." in text for text in system))
    # One turn kept verbatim plus the new exchange.
    self.assertEqual(len(assistant.conversation.turns), 2)


if __name__ == "__main__":
  unittest.main()
