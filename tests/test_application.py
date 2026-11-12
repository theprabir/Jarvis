"""Feature set 4.14 tests: active apps, clipboard help and browser actions."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from core.application import ApplicationError, ApplicationService
from core.assistant import Assistant
from core.config import ApplicationConfig, ConfigError, load_settings
from core.intents import IntentRouter
from tools import build_registry


class NoModelClient:
  def chat(self, *args, **kwargs):
    raise AssertionError("application commands must not call the model")


class FakeSearcher:
  def close(self):
    pass


def make_service(tmp, **overrides):
  options = {
      "active_window": lambda: {
          "title": "Docs - Google Chrome",
          "process": "chrome",
          "pid": 10,
      },
      "clipboard_reader": lambda: "The population is 42 in 2026.",
      "key_sender": lambda keys: overrides.setdefault("keys", []).append(keys),
      "screenshot_provider": lambda: str(Path(tmp) / "shot.png"),
      "page_fetcher": lambda url: "The page explains the API in detail. " * 20,
      "document_reader": lambda path: (
          "Screenshot text from the active window",
          {"quality": "good", "chars": 37},
      ),
  }
  options.update(overrides)
  return ApplicationService(ApplicationConfig(), **options)


class ApplicationServiceTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    Path(self.tmp.name, "shot.png").write_bytes(b"png")
    self.keys = []
    self.service = make_service(self.tmp.name, key_sender=self.keys.append)

  def test_active_application_and_window(self):
    active = self.service.active()
    self.assertEqual(active["application"], "Google Chrome")
    self.assertTrue(active["browser"])
    self.assertEqual(active["title"], "Docs - Google Chrome")
    context = self.service.context()
    self.assertEqual(context["category"], "browser")
    self.assertIn("summarize this page", context["actions"])

  def test_editor_shortcuts_and_non_browser_refusal(self):
    editor = make_service(
        self.tmp.name,
        active_window=lambda: {
            "title": "main.py - Visual Studio Code",
            "process": "code",
            "pid": 11,
        },
        key_sender=self.keys.append,
    )
    self.assertEqual(editor.shortcuts()["category"], "editor")
    text, data = editor.shortcut("command_palette")
    self.assertIn("command palette", text.lower())
    self.assertEqual(self.keys[-1], "ctrl+shift+p")
    with self.assertRaises(ApplicationError):
      editor.browser_action("new_tab")

  def test_browser_actions_send_only_validated_shortcuts(self):
    text, data = self.service.browser_action("new_tab")
    self.assertIn("new tab", text.lower())
    self.assertEqual(data["keys"], "ctrl+t")
    self.assertEqual(self.keys[-1], "ctrl+t")
    with self.assertRaises(ApplicationError):
      self.service.browser_action("run_shell")

  def test_page_summary_uses_browser_url_and_article(self):
    service = make_service(
        self.tmp.name,
        key_sender=self.keys.append,
        clipboard_reader=lambda: "https://example.com/article",
        page_fetcher=lambda url: "The article explains the API. " * 20,
    )
    text, data = service.page_summary()
    self.assertEqual(data["url"], "https://example.com/article")
    self.assertIn("article explains", text)
    self.assertEqual(self.keys, ["ctrl+l", "ctrl+c"])

  def test_clipboard_classification_and_error_explanation(self):
    url = make_service(self.tmp.name, clipboard_reader=lambda: "https://example.com")
    self.assertEqual(url.clipboard()["kind"], "url")
    error = make_service(
        self.tmp.name,
        clipboard_reader=lambda: (
            "Traceback (most recent call last):\n"
            "ModuleNotFoundError: No module named 'requests'"
        ),
    )
    info = error.clipboard()
    self.assertEqual(info["kind"], "error")
    text, _data = error.explain_clipboard()
    self.assertIn("module", text.lower())
    self.assertIn("install", text.lower())

  def test_search_selected_returns_bounded_query(self):
    service = make_service(
        self.tmp.name, clipboard_reader=lambda: "selected words " * 100
    )
    query = service.search_selected()
    self.assertLessEqual(len(query), 500)
    self.assertIn("selected words", query)

  def test_screenshot_ocr_returns_text_and_quality(self):
    text, data = self.service.screenshot_text()
    self.assertIn("Screenshot text", text)
    self.assertEqual(data["quality"], "good")

  def test_documentation_query_mentions_the_application(self):
    query = self.service.documentation_query("asyncio")
    self.assertIn("Google Chrome", query)
    self.assertIn("official documentation", query)

  def test_tool_registry_exposes_read_and_confirmed_actions(self):
    settings = load_settings()
    registry = build_registry(settings, application_service=self.service)
    for name in (
        "application_context",
        "application_shortcuts",
        "application_clipboard",
        "application_explain",
        "application_browser",
        "application_shortcut",
        "application_page",
        "application_screenshot",
    ):
      self.assertIn(name, registry.names())
    self.assertFalse(registry.requires_confirmation("application_context"))
    self.assertTrue(registry.requires_confirmation("application_browser"))
    result = registry.execute("application_context")
    self.assertTrue(result.ok, result.text)
    self.assertIn("Google Chrome", result.text)

  def test_config_validation(self):
    settings = load_settings()
    self.assertTrue(settings.application.enabled)
    self.assertIn("chrome", settings.application.browser_processes)
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text(
          "[application]\nmax_clipboard_chars = 10\n", encoding="utf-8"
      )
      with self.assertRaises(ConfigError):
        load_settings(path)


class ApplicationIntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_application_intents(self):
    cases = {
        "what app am i using": "application_active",
        "what application is active": "application_active",
        "what is the active window title": "application_title",
        "show application shortcuts": "application_shortcuts",
        "show application context": "application_context",
        "explain copied error": "application_explain",
        "search selected text": "application_search",
        "summarize this page": "application_page",
        "new browser tab": "application_tab",
        "ocr screenshot": "application_screenshot",
        "application documentation for asyncio": "application_docs",
    }
    for text, expected in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, expected, text)


class ApplicationAssistantTests(unittest.TestCase):
  def test_app_status_shortcuts_and_pending_action(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(base.conversation, enabled=False),
        application=ApplicationConfig(),
        web=dataclasses.replace(
            base.web, cache_db=str(Path(tmp.name) / "cache.db")
        ),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    assistant._application = make_service(tmp.name)
    self.assertIn("Google Chrome", assistant.reply("/app status"))
    self.assertIn("Shortcuts for Google Chrome", assistant.reply("/app shortcuts"))
    pending = assistant.reply("/app new-tab")
    self.assertIn("confirm", pending.lower())
    self.assertIsNotNone(assistant._pending_tool)


if __name__ == "__main__":
  unittest.main()
