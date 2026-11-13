"""Feature set 4.18 tests: local knowledge base and spaced learning."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.assistant import Assistant
from core.config import load_settings
from core.intents import IntentRouter
from core.knowledge import KnowledgeError, KnowledgeService, classify
from core.notes import NoteService


class FakeDocuments:
  def context(self, query):
    return (
        "[1] Python notes (passage 2): Python lists are mutable.",
        [{"title": "Python notes", "text": "Python lists are mutable."}],
    )


class FakeClient:
  def __init__(self):
    self.calls = []

  def chat(self, messages, max_tokens=None, temperature=None):
    self.calls.append(messages)
    return SimpleNamespace(
        content="Model answer.",
        total_ms=1.0,
        completion_tokens=1,
        generated_tokens_per_second=1.0,
        finish_reason="stop",
    )

  def close(self):
    pass


class KnowledgeServiceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    base = load_settings()
    self.config = dataclasses.replace(
        base.knowledge, db=str(self.root / "knowledge.db")
    )
    self.notes_config = dataclasses.replace(
        base.notes, db=str(self.root / "notes.db")
    )
    self.service = KnowledgeService(self.config, FakeDocuments())
    self.addCleanup(self.service.close)
    self.service.add(
        "Python is a programming language used for scripting and APIs.\n"
        "- It is dynamically typed.\n"
        "- It has a large standard library.",
        title="Python",
    )
    self.service.add(
        "Python lists are ordered mutable collections.",
        title="Python lists",
    )
    self.service.add(
        "The Eiffel Tower is in Paris and is a famous landmark.",
        title="Eiffel Tower",
    )

  def test_auto_categorization_and_search(self):
    category, topics = classify("Python is a programming language")
    self.assertEqual(category, "Programming")
    self.assertIn("python", topics)
    items = self.service.search("Python")
    self.assertGreaterEqual(len(items), 2)
    self.assertEqual(items[0].category, "Programming")

  def test_graph_and_topics(self):
    topics = dict(self.service.topics())
    self.assertGreaterEqual(topics["python"], 2)
    items, edges = self.service.graph()
    self.assertGreaterEqual(len(items), 2)
    self.assertTrue(any(edge["topic"] == "python" for edge in edges))

  def test_flashcards_review_and_progress(self):
    cards = self.service.flashcards()
    self.assertTrue(cards)
    self.assertEqual(len({card.question for card in cards}), len(cards))
    first = cards[0]
    reviewed = self.service.review(first.id, True)
    self.assertEqual(reviewed.reviews, 1)
    self.assertEqual(reviewed.correct, 1)
    self.assertGreater(reviewed.mastery, 0)
    self.assertGreater(reviewed.interval_days, 0)
    wrong = self.service.review(reviewed.id, False)
    self.assertEqual(wrong.streak, 0)
    progress = self.service.progress()
    self.assertGreaterEqual(progress["items"], 3)
    self.assertGreaterEqual(progress["reviews"], 2)

  def test_quiz_and_study_plan(self):
    quiz = self.service.quiz(3)
    self.assertTrue(quiz)
    for question in quiz:
      self.assertIn(question["answer"], question["choices"])
      self.assertEqual(
          question["choices"][question["correct_index"]], question["answer"]
      )
    plan = self.service.study_plan(3)
    self.assertEqual(len(plan), 3)
    self.assertGreaterEqual(plan[0]["minutes"], 5)

  def test_summary_and_cross_document_reasoning(self):
    summary = self.service.summary("Python")
    self.assertIn("Python", summary["summary"])
    cross = self.service.cross("Python")
    self.assertTrue(cross["document_hits"])
    self.assertIn("Python", cross["summary"])
    self.assertTrue(cross["topics"])

  def test_import_notes_skips_duplicates(self):
    notes = NoteService(self.notes_config)
    self.addCleanup(notes.close)
    notes.add("A memory is a stored fact.", title="Memory")
    first = self.service.import_notes(notes)
    second = self.service.import_notes(notes)
    self.assertEqual(first["added"], 1)
    self.assertEqual(second["skipped"], 1)
    self.assertTrue(
        any(item.title == "Memory" for item in self.service.list())
    )

  def test_item_limit(self):
    limited = dataclasses.replace(
        self.config,
        db=str(self.root / "limited.db"),
        max_items=1,
    )
    service = KnowledgeService(limited)
    self.addCleanup(service.close)
    service.add("One small fact.")
    with self.assertRaises(KnowledgeError):
      service.add("Another small fact.")

  def test_natural_knowledge_intents(self):
    router = IntentRouter()
    cases = (
        ("add this to my knowledge base: Rust is a language", "add"),
        ("search my knowledge for Python", "search"),
        ("what do I know about Python", "summary"),
        ("quiz me on Python", "quiz"),
        ("what's due for review", "due"),
        ("make flashcards", "flashcards"),
        ("study progress", "progress"),
        ("study plan for 5 days", "plan"),
        ("show my knowledge graph", "graph"),
        ("show my knowledge topics", "topics"),
    )
    for text, action in cases:
      with self.subTest(text=text):
        intent = router.route(text)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.name, "knowledge")
        self.assertEqual(intent.entities["action"], action)


class KnowledgeAssistantTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = Path(self.tmp.name)
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(
            base.conversation,
            enabled=False,
            history_db=str(self.root / "history.db"),
        ),
        memory=dataclasses.replace(base.memory, enabled=False),
        documents=dataclasses.replace(base.documents, enabled=False),
        tools=dataclasses.replace(base.tools, root=str(self.root)),
        knowledge=dataclasses.replace(
            base.knowledge, db=str(self.root / "knowledge.db")
        ),
        notes=dataclasses.replace(base.notes, db=str(self.root / "notes.db")),
    )
    self.client = FakeClient()
    self.assistant = Assistant(settings, client=self.client)
    self.addCleanup(self.assistant.close)

  def test_knowledge_commands_and_natural_add(self):
    self.assertIn("/knowledge", self.assistant.reply("/help"))
    reply = self.assistant.reply(
        "/knowledge add Python is a programming language. | Python"
    )
    self.assertIn("Knowledge #1", reply)
    self.assertIn("Programming", reply)
    listed = self.assistant.reply("/knowledge list")
    self.assertIn("Python", listed)
    natural = self.assistant.reply(
        "add this to my knowledge base: The sky is blue."
    )
    self.assertIn("Knowledge #2", natural)
    self.assertIn("Knowledge", self.assistant.reply("what do I know about blue"))

  def test_knowledge_study_commands(self):
    self.assistant.reply(
        "/knowledge add Python lists are ordered mutable collections. | Lists"
    )
    cards = self.assistant.reply("/knowledge flashcards")
    self.assertIn("What is Python", cards)
    due = self.assistant.reply("/knowledge due")
    self.assertIn("Due for review", due)
    reviewed = self.assistant.reply("/knowledge review 1 correct")
    self.assertIn("reviewed", reviewed)
    quiz = self.assistant.reply("/knowledge quiz 2")
    self.assertIn("Knowledge quiz", quiz)
    plan = self.assistant.reply("/knowledge plan 3")
    self.assertIn("Study plan", plan)
    progress = self.assistant.reply("/knowledge progress")
    self.assertIn("Knowledge progress", progress)


if __name__ == "__main__":
  unittest.main()
