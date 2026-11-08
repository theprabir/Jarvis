"""Feature set 4.3 tests plus session resume and /forget.

All offline: a fake LLM client and temporary SQLite files.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from brain.errors import LLMConnectionError
from core.assistant import Assistant
from core.calculator import CalculationError, evaluate, format_number
from core.config import ConversationConfig, load_settings
from core.dates import find_date_reference
from core.history import HistoryStore
from core.intents import Intent, IntentError, IntentRouter


class FakeClient:
  def __init__(self, replies=("Model answer.",), error=None):
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
        total_ms=100.0,
        completion_tokens=4,
        generated_tokens_per_second=5.0,
    )


class CalculatorTests(unittest.TestCase):
  def test_arithmetic(self):
    self.assertEqual(evaluate("2 + 2"), 4)
    self.assertEqual(evaluate("17 * 24"), 408)
    self.assertEqual(evaluate("(8 + 4) / 3"), 4)
    self.assertEqual(evaluate("-5 + 2"), -3)

  def test_power_is_bounded(self):
    self.assertEqual(evaluate("2 ** 10"), 1024)
    with self.assertRaises(CalculationError):
      evaluate("2 ** 100")

  def test_division_by_zero(self):
    with self.assertRaises(CalculationError):
      evaluate("1 / 0")

  def test_names_and_calls_are_rejected(self):
    for expression in ("import os", "os.system('dir')", "abs(-1)"):
      with self.assertRaises(CalculationError):
        evaluate(expression)

  def test_empty_and_too_long(self):
    with self.assertRaises(CalculationError):
      evaluate("  ")
    with self.assertRaises(CalculationError):
      evaluate("1+" * 100 + "1")

  def test_format_number(self):
    self.assertEqual(format_number(4), "4")
    self.assertEqual(format_number(4.0), "4")
    self.assertEqual(format_number(2.5), "2.5")
    self.assertEqual(format_number(1 / 3), "0.333333")


class DateTests(unittest.TestCase):
  TODAY = date(2026, 11, 8)

  def test_tomorrow(self):
    reference = find_date_reference("what is the date tomorrow?", self.TODAY)
    self.assertEqual(reference["date"], "2026-11-09")
    self.assertEqual(reference["offset_days"], 1)
    self.assertEqual(reference["phrase"], "tomorrow")

  def test_day_after_tomorrow_wins_over_plain_tomorrow(self):
    reference = find_date_reference(
        "what day is it the day after tomorrow", self.TODAY
    )
    self.assertEqual(reference["offset_days"], 2)

  def test_relative_offsets(self):
    self.assertEqual(
        find_date_reference("in 3 days", self.TODAY)["date"], "2026-11-11"
    )
    self.assertEqual(
        find_date_reference("2 weeks ago", self.TODAY)["date"], "2026-10-25"
    )

  def test_next_weekday(self):
    reference = find_date_reference("what date is next monday", self.TODAY)
    target = date.fromisoformat(reference["date"])
    self.assertEqual(target.weekday(), 0)
    self.assertTrue(1 <= reference["offset_days"] <= 7)

  def test_last_weekday(self):
    reference = find_date_reference("last friday", self.TODAY)
    target = date.fromisoformat(reference["date"])
    self.assertEqual(target.weekday(), 4)
    self.assertTrue(-7 <= reference["offset_days"] <= -1)

  def test_iso_date(self):
    reference = find_date_reference("2027-01-31", self.TODAY)
    self.assertEqual(reference["date"], "2027-01-31")
    self.assertEqual(reference["offset_days"], 84)

  def test_no_reference(self):
    self.assertIsNone(find_date_reference("hello there", self.TODAY))


class RouterTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_time_intent(self):
    intent = self.router.route("What time is it?")
    self.assertEqual(intent.name, "time")
    self.assertGreaterEqual(intent.confidence, 0.9)

  def test_date_intent_has_reference_entity(self):
    intent = self.router.route("what is the date tomorrow?")
    self.assertEqual(intent.name, "date")
    self.assertEqual(intent.entities["offset_days"], 1)

  def test_math_with_words(self):
    intent = self.router.route("what is 17 times 24?")
    self.assertEqual(intent.name, "math")
    self.assertEqual(intent.entities["expression"], "17 * 24")
    self.assertEqual(intent.entities["result"], 408)

  def test_math_error_is_still_recognized(self):
    intent = self.router.route("what is 10 divided by 0")
    self.assertEqual(intent.name, "math")
    self.assertIn("division by zero", intent.entities["error"])

  def test_command_intent(self):
    intent = self.router.route("/mode detailed")
    self.assertEqual(intent.name, "mode")
    self.assertEqual(intent.confidence, 1.0)

  def test_unknown_command_is_not_routed(self):
    self.assertIsNone(self.router.route("/explode"))

  def test_natural_language_falls_back(self):
    for text in (
        "What is the capital of France?",
        "Explain how TCP handshakes work.",
        "calculate the area of a circle with radius 3",
        "what is 5",
    ):
      self.assertIsNone(self.router.route(text), text)

  def test_confidence_threshold(self):
    self.assertTrue(self.router.is_confident(Intent("time", 0.9)))
    self.assertFalse(self.router.is_confident(Intent("time", 0.2)))
    self.assertFalse(self.router.is_confident(None))


class IntentValidationTests(unittest.TestCase):
  def test_roundtrip(self):
    original = Intent("math", 0.95, {"expression": "2 + 2", "result": 4})
    parsed = Intent.from_dict(json.loads(original.to_json()))
    self.assertEqual(parsed, original)

  def test_invalid_payloads(self):
    with self.assertRaises(IntentError):
      Intent.from_dict([])
    with self.assertRaises(IntentError):
      Intent.from_dict({"intent": "launch_rocket", "confidence": 1.0})
    with self.assertRaises(IntentError):
      Intent.from_dict({"intent": "time", "confidence": 1.5})
    with self.assertRaises(IntentError):
      Intent.from_dict({"intent": "time", "confidence": 0.5,
                        "entities": ["now"]})
    with self.assertRaises(IntentError):
      Intent.from_dict({"intent": "time", "confidence": 0.5,
                        "entities": {"nested": {"a": 1}}})
    with self.assertRaises(IntentError):
      Intent.from_dict({"intent": "time", "confidence": 0.5,
                        "source": "guess"})


class AssistantIntelligenceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)

  def make(self, replies=("Model answer.",), error=None, **overrides):
    options = {"history_db": str(Path(self.tmp.name) / "history.db")}
    options.update(overrides)
    settings = dataclasses.replace(
        load_settings(), conversation=ConversationConfig(**options)
    )
    client = FakeClient(replies, error)
    assistant = Assistant(settings, client=client)
    self.addCleanup(assistant.close)
    return assistant, client

  def test_math_is_answered_without_the_model(self):
    assistant, client = self.make()
    self.assertEqual(assistant.reply("what is 17 times 24?"), "That is 408.")
    self.assertEqual(client.calls, [])

  def test_time_is_answered_locally(self):
    assistant, client = self.make()
    reply = assistant.reply("what time is it?")
    self.assertTrue(reply.startswith("It is "))
    self.assertIn(datetime.now().strftime("%H:%M"), reply)
    self.assertEqual(client.calls, [])

  def test_date_with_offset(self):
    assistant, _ = self.make()
    reply = assistant.reply("what is the date tomorrow?")
    self.assertIn("Tomorrow is", reply)
    expected = date.fromordinal(date.today().toordinal() + 1)
    self.assertIn(expected.strftime("%d %B %Y"), reply)

  def test_math_error_is_reported(self):
    assistant, client = self.make()
    self.assertEqual(
        assistant.reply("what is 10 divided by 0"),
        "I can't calculate that, sir: division by zero.",
    )
    self.assertEqual(client.calls, [])

  def test_open_question_uses_the_model(self):
    assistant, client = self.make(replies=("Blue.",))
    self.assertEqual(assistant.reply("Why is the sky blue?"), "Blue.")
    self.assertEqual(len(client.calls), 1)

  def test_intent_command_returns_valid_json(self):
    assistant, client = self.make()
    payload = assistant.reply("/intent what is 2 plus 2")
    parsed = Intent.from_dict(json.loads(payload))
    self.assertEqual(parsed.name, "math")
    self.assertEqual(parsed.entities["result"], 4)
    self.assertEqual(client.calls, [])

  def test_intent_command_without_match(self):
    assistant, _ = self.make()
    payload = json.loads(assistant.reply("/intent tell me a story"))
    self.assertIsNone(payload["intent"])

  def test_local_answer_is_stored(self):
    assistant, _ = self.make()
    assistant.reply("what is 2 plus 2")
    self.assertEqual(len(assistant.conversation.turns), 1)
    self.assertIn("That is 4.", assistant.reply("/history"))


class ResumeSessionTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.db = str(Path(self.tmp.name) / "history.db")

  def build(self, **overrides):
    settings = dataclasses.replace(
        load_settings(),
        conversation=ConversationConfig(history_db=self.db, **overrides),
    )
    return settings

  def test_restart_restores_context_and_session(self):
    first = Assistant(self.build(), client=FakeClient(("Noted, Alex.",)))
    first.reply("My name is Alex")
    session = first.session_id
    first.close()

    second = Assistant(self.build(), client=FakeClient())
    self.addCleanup(second.close)
    self.assertEqual(second.session_id, session)
    self.assertEqual(len(second.conversation.turns), 1)
    self.assertEqual(second.conversation.turns[0].user, "My name is Alex")
    self.assertIn("My name is Alex", second.reply("/history"))

  def test_resume_can_be_disabled(self):
    first = Assistant(self.build(), client=FakeClient())
    first.reply("remember the number 42")
    session = first.session_id
    first.close()

    second = Assistant(
        self.build(resume_last_session=False), client=FakeClient()
    )
    self.addCleanup(second.close)
    self.assertEqual(second.conversation.turns, [])
    self.assertNotEqual(second.session_id, session)

  def test_resume_respects_max_turns(self):
    first = Assistant(self.build(max_turns=8), client=FakeClient())
    for index in range(4):
      first.reply(f"message number {index}")
    first.close()

    second = Assistant(self.build(max_turns=2), client=FakeClient())
    self.addCleanup(second.close)
    self.assertEqual(len(second.conversation.turns), 2)
    self.assertEqual(second.conversation.turns[-1].user, "message number 3")


class ForgetCommandTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.db = Path(self.tmp.name) / "history.db"
    settings = dataclasses.replace(
        load_settings(),
        conversation=ConversationConfig(history_db=str(self.db)),
    )
    self.assistant = Assistant(settings, client=FakeClient())
    self.addCleanup(self.assistant.close)

  def test_confirmation_is_required_before_deleting(self):
    self.assistant.reply("hello")
    question = self.assistant.reply("/forget")
    self.assertIn("permanently delete 1 saved exchange", question)
    self.assertEqual(self.assistant._store.count(), 1)

    done = self.assistant.reply("/forget confirm")
    self.assertIn("deleted", done)
    self.assertEqual(self.assistant._store.count(), 0)
    self.assertEqual(self.assistant.conversation.turns, [])

  def test_confirm_without_request_deletes_nothing(self):
    self.assistant.reply("hello")
    reply = self.assistant.reply("/forget confirm")
    self.assertIn("Run /forget first", reply)
    self.assertEqual(self.assistant._store.count(), 1)

  def test_nothing_to_delete(self):
    self.assertIn("no saved conversation history", self.assistant.reply("/forget"))

  def test_unknown_argument_keeps_history(self):
    self.assistant.reply("hello")
    reply = self.assistant.reply("/forget later")
    self.assertIn("/forget confirm", reply)
    self.assertEqual(self.assistant._store.count(), 1)

  def test_deletion_survives_reopen(self):
    self.assistant.reply("hello")
    self.assistant.reply("/forget")
    self.assistant.reply("/forget confirm")
    store = HistoryStore(self.db)
    try:
      self.assertEqual(store.count(), 0)
    finally:
      store.close()


if __name__ == "__main__":
  unittest.main()
