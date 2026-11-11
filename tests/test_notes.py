"""Feature set 4.11 M3 tests: local notes, search and quick notes."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from core.assistant import Assistant
from core.config import ConfigError, NotesConfig, load_settings
from core.intents import IntentRouter
from core.notes import NoteError, NoteService, format_note
from tools import build_registry


class NoteServiceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    self.service = NoteService(
        NotesConfig(db=str(self.base / "notes.db"))
    )
    self.addCleanup(self.service.close)

  def test_add_list_show_update_and_delete(self):
    note = self.service.add("Call the dentist about the crown")
    self.assertEqual(note.title, "Call the dentist about the crown")
    self.assertEqual(self.service.get(note.id).content, note.content)
    self.assertEqual(self.service.list()[0].id, note.id)
    self.assertIn("Call the dentist", format_note(note))

    updated = self.service.update(note.id, content="Call the dentist tomorrow")
    self.assertEqual(updated.title, "Call the dentist tomorrow")
    self.assertEqual(updated.content, "Call the dentist tomorrow")

    removed = self.service.delete(note.id)
    self.assertEqual(removed.id, note.id)
    self.assertIsNone(self.service.get(note.id))

  def test_explicit_title_and_validation(self):
    note = self.service.add("Remember the milk", title="Shopping")
    self.assertEqual(note.title, "Shopping")
    with self.assertRaises(NoteError):
      self.service.add("   ")
    with self.assertRaises(NoteError):
      self.service.add("x" * 10_001)
    with self.assertRaises(NoteError):
      self.service.update(note.id)
    with self.assertRaises(NoteError):
      self.service.update(999, content="missing")

  def test_search_returns_notes_and_snippets(self):
    first = self.service.add(
        "The dentist appointment is on Friday. Bring the insurance card."
    )
    self.service.add("Buy milk and eggs")
    hits = self.service.search("dentist")
    self.assertEqual(len(hits), 1)
    self.assertEqual(hits[0].note.id, first.id)
    self.assertIn("dentist", hits[0].snippet.lower())
    self.assertIn(hits[0].mode, {"fts", "scan"})

  def test_search_falls_back_to_scored_scan(self):
    first = self.service.add("Project notes mention the blue folder")
    self.service.add("Unrelated shopping list")
    self.service._store._fts = False
    hits = self.service.search("blue folder")
    self.assertEqual(hits[0].note.id, first.id)
    self.assertEqual(hits[0].mode, "scan")
    with self.assertRaises(NoteError):
      self.service.search("!!!")


class IntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_quick_note_intents(self):
    cases = {
        "take a note call the dentist": "note_add",
        "make a note to buy milk": "note_add",
        "jot down book the train": "note_add",
        "note that the spare key is in the drawer": "note_add",
        "note call Sam": "note_add",
        "note, call Sam": "note_add",
        "quick note: call Sam": "note_add",
        "add a note: check the boiler": "note_add",
    }
    for text, name in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, name, text)

  def test_note_list_show_search_and_delete_intents(self):
    cases = {
        "list my notes": "note_list",
        "what are my notes": "note_list",
        "show note 2": "note_show",
        "search notes for dentist": "note_search",
        "notes about insurance": "note_search",
        "delete note 2": "note_delete",
        "remove note 2": "note_delete",
    }
    for text, name in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, name, text)

  def test_commands_and_task_controls_remain_separate(self):
    self.assertEqual(self.router.route("/notes").name, "notes")
    self.assertEqual(self.router.route("/note").name, "note")
    self.assertEqual(
        self.router.route("plan a task to organize downloads").name,
        "task_plan",
    )


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
            base.notes,
            enabled=enabled,
            db=str(self.base / "notes.db"),
        ),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    return assistant

  def test_commands_and_natural_language(self):
    assistant = self.make()
    answer = assistant.reply("take a note call the dentist tomorrow")
    self.assertIn("Note #1", answer)
    self.assertIn("dentist", assistant.reply("/notes list"))
    self.assertIn("dentist", assistant.reply("search notes for dentist"))
    self.assertIn("call the dentist", assistant.reply("show note 1"))
    self.assertIn("updated", assistant.reply("/notes edit 1 content call on Friday"))
    self.assertIn("call on Friday", assistant.reply("/notes show 1"))
    self.assertIn("removed", assistant.reply("/notes remove 1"))
    self.assertEqual(assistant.reply("/notes list"), "You have no notes, sir.")

  def test_quick_note_title_and_list_aliases(self):
    assistant = self.make()
    self.assertIn(
        "Note #1",
        assistant.reply("/notes add Shopping | buy milk and eggs"),
    )
    self.assertIn("Shopping", assistant.reply("what are my notes"))
    self.assertIn("buy milk", assistant.reply("note that buy milk and eggs"))

  def test_natural_delete_requires_confirmation_and_uses_tool(self):
    assistant = self.make(tools=True)
    assistant.reply("note that cancel the old subscription")
    pending = assistant.reply("remove note 1")
    self.assertIn("Delete note #1", pending)
    self.assertIn("confirm", pending.lower())
    self.assertEqual(assistant._pending_tool[0], "note_delete")
    self.assertIsNotNone(assistant._get_notes().get(1))
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_notes().get(1))

  def test_natural_delete_can_be_cancelled(self):
    assistant = self.make(tools=True)
    assistant.reply("note that keep this note")
    self.assertIn("confirm", assistant.reply("delete note 1").lower())
    self.assertIn("cancelled", assistant.reply("no").lower())
    self.assertIsNotNone(assistant._get_notes().get(1))

  def test_natural_delete_works_without_tools_after_confirmation(self):
    assistant = self.make(tools=False)
    assistant.reply("note that remove this later")
    self.assertIn("confirm", assistant.reply("remove note 1").lower())
    self.assertIn("removed", assistant.reply("yes").lower())
    self.assertIsNone(assistant._get_notes().get(1))

  def test_disabled_notes(self):
    assistant = self.make(enabled=False)
    self.assertEqual(assistant.reply("/notes"), "Notes are disabled, sir.")
    self.assertEqual(
        assistant.reply("note that buy milk"), "Notes are disabled, sir."
    )


class NoteToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)
    settings = load_settings()
    self.settings = dataclasses.replace(
        settings,
        notes=dataclasses.replace(
            settings.notes, db=str(self.base / "notes.db")
        ),
        tools=dataclasses.replace(settings.tools, root=str(self.base)),
    )
    self.service = NoteService(self.settings.notes)
    self.addCleanup(self.service.close)
    self.registry = build_registry(
        self.settings, notes_service=self.service
    )

  def test_tools_are_registered_with_permissions(self):
    names = set(self.registry.names())
    for name in {
        "note_add",
        "note_list",
        "note_show",
        "note_search",
        "note_update",
        "note_delete",
    }:
      self.assertIn(name, names)
    self.assertTrue(self.registry.requires_confirmation("note_delete"))
    self.assertFalse(self.registry.requires_confirmation("note_add"))

  def test_add_search_show_update_and_delete_tools(self):
    added = self.registry.execute(
        "note_add",
        {"content": "Ask Sam about the invoice", "title": "Invoice"},
    )
    self.assertTrue(added.ok, added.text)
    note_id = added.data["id"]
    self.assertEqual(added.data["title"], "Invoice")

    listed = self.registry.execute("note_list")
    self.assertTrue(listed.ok, listed.text)
    self.assertEqual(listed.data["count"], 1)

    shown = self.registry.execute("note_show", {"id": note_id})
    self.assertTrue(shown.ok, shown.text)
    self.assertIn("Ask Sam", shown.text)

    found = self.registry.execute("note_search", {"query": "invoice"})
    self.assertTrue(found.ok, found.text)
    self.assertEqual(found.data["results"][0]["note"]["id"], note_id)

    updated = self.registry.execute(
        "note_update", {"id": note_id, "content": "Ask Sam tomorrow"}
    )
    self.assertTrue(updated.ok, updated.text)
    self.assertEqual(updated.data["title"], "Ask Sam tomorrow")

    deleted = self.registry.execute("note_delete", {"id": note_id})
    self.assertTrue(deleted.ok, deleted.text)
    self.assertIsNone(self.service.get(note_id))

  def test_tool_validation(self):
    missing = self.registry.execute("note_add", {})
    self.assertFalse(missing.ok)
    self.assertIn("missing argument", missing.text)

    bad_type = self.registry.execute("note_show", {"id": "one"})
    self.assertFalse(bad_type.ok)
    self.assertIn("must be int", bad_type.text)

    empty = self.registry.execute("note_add", {"content": "   "})
    self.assertFalse(empty.ok)
    self.assertIn("needs some text", empty.text)

    missing_search = self.registry.execute("note_search", {"query": "!!!"})
    self.assertFalse(missing_search.ok)
    self.assertIn("needs a word", missing_search.text)


class ConfigTests(unittest.TestCase):
  def test_invalid_notes_limits_are_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[notes]\nmax_items = 0\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


class NoModelClient:
  """Fail loudly if a deterministic note request reaches the model."""

  def __init__(self):
    self.calls = 0

  def chat(self, *args, **kwargs):
    self.calls += 1
    raise AssertionError("the model must not be called")


if __name__ == "__main__":
  unittest.main()
