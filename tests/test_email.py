"""Feature set 4.11 M8 tests: local drafts and connected mailbox actions."""

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from email.message import EmailMessage as MIMEMessage
from pathlib import Path
from unittest import mock

from core.assistant import Assistant
from core.config import ConfigError, EmailConfig, load_settings
from core.intents import IntentRouter
from core.mail import (
    EmailError,
    EmailService,
    format_draft,
    format_message,
    format_summary,
    parse_email_input,
    summarize_text,
)
from tools import build_registry


class ParseTests(unittest.TestCase):
  def test_recipient_subject_and_body(self):
    parsed = parse_email_input("sam@example.com | Lunch | See you at noon")
    self.assertEqual(parsed["recipient"], "sam@example.com")
    self.assertEqual(parsed["subject"], "Lunch")
    self.assertEqual(parsed["body"], "See you at noon")

  def test_natural_about_form(self):
    parsed = parse_email_input("to sam@example.com about lunch")
    self.assertEqual(parsed["recipient"], "sam@example.com")
    self.assertEqual(parsed["subject"], "lunch")

  def test_empty_input(self):
    self.assertEqual(parse_email_input(""), {"recipient": "", "cc": "", "subject": "", "body": ""})


class StoreTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.service = EmailService(
        EmailConfig(db=str(Path(self.tmp.name) / "mail.db"))
    )
    self.addCleanup(self.service.close)

  def test_draft_crud(self):
    draft = self.service.add(
        "sam@example.com",
        subject="Lunch",
        body="See you at noon",
        cc="team@example.com",
    )
    self.assertEqual(draft.status, "draft")
    self.assertIn("Lunch", format_draft(draft))
    self.assertEqual(self.service.get(draft.id).recipient, "sam@example.com")
    self.assertEqual(len(self.service.list()), 1)

    draft = self.service.update(draft.id, subject="Lunch tomorrow")
    self.assertEqual(draft.subject, "Lunch tomorrow")
    self.assertEqual(self.service.list(status="draft")[0].id, draft.id)

    removed = self.service.delete(draft.id)
    self.assertEqual(removed.id, draft.id)
    self.assertIsNone(self.service.get(draft.id))

  def test_validation_limit_and_header_injection(self):
    with self.assertRaises(EmailError):
      self.service.add("not-an-address")
    with self.assertRaises(EmailError):
      self.service.add("sam@example.com", subject="x" * 301)
    draft = self.service.add("sam@example.com", subject="Hello\r\nBcc: bad@example.com")
    self.assertEqual(draft.subject, "Hello Bcc: bad@example.com")
    limited = EmailService(
        EmailConfig(db=str(Path(self.tmp.name) / "limited.db"), max_drafts=1)
    )
    self.addCleanup(limited.close)
    limited.add("sam@example.com")
    with self.assertRaises(EmailError):
      limited.add("other@example.com")


class IntentTests(unittest.TestCase):
  def test_email_intents(self):
    router = IntentRouter()
    cases = {
        "draft an email to sam@example.com about lunch": "email_draft",
        "list my email drafts": "email_list",
        "show email draft 2": "email_show",
        "send email draft 2": "email_send",
        "remove email draft 2": "email_delete",
        "search my email for invoice": "email_search",
        "read email 7": "email_read",
        "summarize email 7": "email_summarize",
        "reply to email 7": "email_reply",
    }
    for text, name in cases.items():
      intent = router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, name, text)

  def test_draft_entities_and_commands(self):
    intent = IntentRouter().route(
        "draft an email to sam@example.com about lunch"
    )
    self.assertEqual(intent.entities["recipient"], "sam@example.com")
    self.assertEqual(intent.entities["subject"], "lunch")
    self.assertEqual(IntentRouter().route("/email").name, "email")
    self.assertEqual(IntentRouter().route("/mail").name, "mail")


class RemoteTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.raw = self._message()
    self.imap = FakeIMAP(self.raw)
    self.smtp = FakeSMTP()
    self.config = EmailConfig(
        db=str(Path(self.tmp.name) / "mail.db"),
        from_address="me@example.com",
        username="me@example.com",
        password_env="TEST_MAIL_PASSWORD",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )
    self.env = mock.patch.dict(
        os.environ, {"TEST_MAIL_PASSWORD": "secret"}, clear=False
    )
    self.env.start()
    self.addCleanup(self.env.stop)
    self.service = EmailService(
        self.config,
        imap_factory=lambda host, port: self.imap,
        smtp_factory=lambda host, port, timeout=None: self.smtp,
    )
    self.addCleanup(self.service.close)

  @staticmethod
  def _message():
    message = MIMEMessage()
    message["From"] = "Ada <ada@example.com>"
    message["To"] = "me@example.com"
    message["Subject"] = "Invoice for November"
    message["Date"] = "Tue, 10 Nov 2026 10:00:00 +0000"
    message["Message-ID"] = "<invoice@example.com>"
    message.set_content("Please find the November invoice attached. The total is 42 units.")
    return message.as_bytes()

  def test_search_read_summary_and_reply(self):
    messages = self.service.search("invoice")
    self.assertEqual(len(messages), 1)
    self.assertEqual(messages[0].uid, "7")
    self.assertEqual(messages[0].sender, "Ada <ada@example.com>")
    self.assertIn("November invoice", messages[0].body)
    self.assertIn("UID 7", format_message(messages[0]))

    message = self.service.read(7)
    self.assertEqual(message.subject, "Invoice for November")
    message, summary = self.service.summarize(7)
    self.assertIn("invoice", summary.lower())
    self.assertIn("Summary of UID 7", format_summary(message, summary))

    original, draft = self.service.reply(7)
    self.assertEqual(original.uid, "7")
    self.assertEqual(draft.recipient, "Ada <ada@example.com>")
    self.assertEqual(draft.subject, "Re: Invoice for November")
    self.assertEqual(draft.in_reply_to, "<invoice@example.com>")
    self.assertIn("> Please find", draft.body)

  def test_send_marks_draft_sent(self):
    draft = self.service.add(
        "sam@example.com", subject="Hello", body="A short note."
    )
    sent = self.service.send(draft.id)
    self.assertEqual(sent.status, "sent")
    self.assertEqual(len(self.smtp.messages), 1)
    message = self.smtp.messages[0]
    self.assertEqual(message["To"], "sam@example.com")
    self.assertEqual(message["From"], "me@example.com")
    self.assertEqual(message["Subject"], "Hello")

  def test_missing_account_and_duplicate_send(self):
    draft = self.service.add("sam@example.com")
    self.service.send(draft.id)
    with self.assertRaises(EmailError):
      self.service.send(draft.id)

    local = EmailService(EmailConfig(db=str(Path(self.tmp.name) / "local.db")))
    self.addCleanup(local.close)
    with self.assertRaises(EmailError):
      local.search("invoice")
    with self.assertRaises(EmailError):
      local.send(local.add("sam@example.com").id)


class AssistantTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.base = Path(self.tmp.name)

  def make(self, enabled=True, tools=False):
    settings = load_settings()
    settings = dataclasses.replace(
        settings,
        conversation=dataclasses.replace(settings.conversation, enabled=False),
        memory=dataclasses.replace(
            settings.memory, enabled=False, db=str(self.base / "memory.db")
        ),
        documents=dataclasses.replace(settings.documents, enabled=False),
        tools=dataclasses.replace(
            settings.tools, enabled=tools, root=str(self.base)
        ),
        weather=dataclasses.replace(settings.weather, enabled=False),
        notes=dataclasses.replace(
            settings.notes, enabled=False, db=str(self.base / "notes.db")
        ),
        habits=dataclasses.replace(
            settings.habits, enabled=False, db=str(self.base / "habits.db")
        ),
        goals=dataclasses.replace(
            settings.goals, enabled=False, db=str(self.base / "goals.db")
        ),
        time=dataclasses.replace(
            settings.time, enabled=False, db=str(self.base / "time.db")
        ),
        calendar=dataclasses.replace(
            settings.calendar, enabled=False, db=str(self.base / "cal.db")
        ),
        email=dataclasses.replace(
            settings.email,
            enabled=enabled,
            db=str(self.base / "mail.db"),
            imap_host="",
            smtp_host="",
            username="",
            from_address="",
        ),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    return assistant

  def test_local_drafts_commands_and_natural_language(self):
    assistant = self.make()
    answer = assistant.reply(
        "draft an email to sam@example.com about lunch"
    )
    self.assertIn("Draft #1", answer)
    self.assertIn("lunch", answer)
    listed = assistant.reply("list my email drafts")
    self.assertIn("sam@example.com", listed)
    self.assertIn("lunch", listed)
    self.assertIn("lunch", assistant.reply("show email draft 1"))
    self.assertIn(
        "updated", assistant.reply("/email edit 1 subject Lunch tomorrow")
    )
    self.assertIn(
        "Draft #2", assistant.reply("/email draft sam@example.com | Hi | Body")
    )

  def test_natural_delete_and_send_confirmations(self):
    assistant = self.make(tools=True)
    assistant.reply("draft an email to sam@example.com about lunch")
    pending = assistant.reply("remove email draft 1")
    self.assertIn("Delete email draft #1", pending)
    self.assertEqual(assistant._pending_tool[0], "email_delete")
    self.assertIn("removed", assistant.reply("yes").lower())

    assistant.reply("draft an email to sam@example.com about lunch")
    pending = assistant.reply("send email draft 2")
    self.assertIn("Send email draft #2", pending)
    self.assertEqual(assistant._pending_tool[0], "email_send")
    answer = assistant.reply("yes")
    self.assertIn("smtp", answer.lower())

  def test_disabled_email(self):
    assistant = self.make(enabled=False)
    self.assertEqual(
        assistant.reply("/email"), "Email is disabled, sir."
    )
    self.assertEqual(
        assistant.reply("list my email drafts"), "Email is disabled, sir."
    )


class EmailToolTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    base = load_settings()
    self.settings = dataclasses.replace(
        base,
        email=dataclasses.replace(
            base.email, db=str(Path(self.tmp.name) / "mail.db")
        ),
        tools=dataclasses.replace(base.tools, root=self.tmp.name),
    )
    self.service = EmailService(self.settings.email)
    self.addCleanup(self.service.close)
    self.registry = build_registry(
        self.settings, email_service=self.service
    )

  def test_tools_registered_and_confirmations(self):
    names = set(self.registry.names())
    for name in {
        "email_draft",
        "email_list",
        "email_show",
        "email_update",
        "email_delete",
        "email_send",
        "email_search",
        "email_read",
        "email_summarize",
        "email_reply",
    }:
      self.assertIn(name, names)
    self.assertTrue(self.registry.requires_confirmation("email_delete"))
    self.assertTrue(self.registry.requires_confirmation("email_send"))
    self.assertFalse(self.registry.requires_confirmation("email_draft"))

  def test_draft_tools(self):
    added = self.registry.execute(
        "email_draft",
        {"recipient": "sam@example.com", "subject": "Hi", "body": "Hello"},
    )
    self.assertTrue(added.ok, added.text)
    draft_id = added.data["id"]
    self.assertEqual(self.registry.execute("email_list").data["count"], 1)
    self.assertTrue(self.registry.execute("email_show", {"id": draft_id}).ok)
    updated = self.registry.execute(
        "email_update", {"id": draft_id, "subject": "Updated"}
    )
    self.assertTrue(updated.ok, updated.text)
    self.assertEqual(updated.data["subject"], "Updated")
    self.assertTrue(
        self.registry.execute("email_delete", {"id": draft_id}).ok
    )

  def test_draft_tool_validation(self):
    missing = self.registry.execute("email_draft", {})
    self.assertFalse(missing.ok)
    self.assertIn("missing argument", missing.text)
    bad = self.registry.execute(
        "email_draft", {"recipient": "not-an-address"}
    )
    self.assertFalse(bad.ok)
    self.assertIn("valid recipient", bad.text)


class ConfigTests(unittest.TestCase):
  def test_invalid_email_limit_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[email]\nmax_drafts = 0\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


class FakeIMAP:
  def __init__(self, raw):
    self.raw = raw
    self.logged_out = False

  def login(self, username, password):
    return "OK", []

  def select(self, mailbox, readonly=False):
    return "OK", []

  def uid(self, command, *args):
    if command == "SEARCH":
      return "OK", [b"7"]
    if command == "FETCH":
      return "OK", [(b"7 (RFC822 {1}", self.raw), b")"]
    return "NO", []

  def logout(self):
    self.logged_out = True
    return "BYE", []


class FakeSMTP:
  def __init__(self):
    self.messages = []
    self.quit_called = False

  def ehlo(self):
    return 250, b"ok"

  def starttls(self):
    return 220, b"ok"

  def login(self, username, password):
    return 235, b"ok"

  def send_message(self, message):
    self.messages.append(message)

  def quit(self):
    self.quit_called = True


class NoModelClient:
  def chat(self, *args, **kwargs):
    raise AssertionError("the model must not be called")


if __name__ == "__main__":
  unittest.main()
