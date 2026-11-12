"""Feature set 4.13 tests: multi-source research, reports and saved history."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from core.assistant import Assistant
from core.config import WebConfig, load_settings
from core.intents import IntentRouter
from core.research import (
  ResearchError,
  ResearchService,
  ResearchStore,
  assess_freshness,
  extract_facts,
  find_contradictions,
  research_queries,
)
from web.base import SearchResult
from web.extract import extract_published
from web.search import SearchBundle


class FakeSearcher:
  def __init__(self, results=None):
    self.results = results or []
    self.queries = []

  def search(self, query, **kwargs):
    self.queries.append(query)
    return SearchBundle(
        query,
        self.results,
        "",
        "fake",
        sources=self.results,
        terms=["research"],
    )

  def provider_names(self):
    return ("fake", "second")

  def close(self):
    pass


class FakeClient:
  def chat(self, *args, **kwargs):
    raise AssertionError("research must not call the model")


def make_config(tmp):
  return WebConfig(
      cache_db=str(Path(tmp) / "cache.db"),
      research_db=str(Path(tmp) / "research.db"),
      research_max_queries=3,
      research_max_sources=6,
  )


def source(title, url, text, published="", snippet=""):
  return SearchResult(
      title=title,
      url=url,
      text=text,
      snippet=snippet or text[:200],
      published=published,
      score=1.0,
  )


class ResearchTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.config = make_config(self.tmp.name)

  def test_queries_include_docs_and_keywords(self):
    self.assertEqual(
        research_queries("python asyncio", "general", 3)[0],
        "python asyncio",
    )
    self.assertIn(
        "python asyncio official documentation",
        research_queries("python asyncio", "docs", 3),
    )
    self.assertEqual(research_queries("a b", "general", 1), ["a b"])

  def test_facts_and_contradictions(self):
    sources = [
        source(
            "Population report",
            "https://example.com/a",
            "The population was 10 million in 2025.",
            "2026-11-01",
        ),
        source(
            "Other population report",
            "https://example.net/b",
            "The population was 12 million in 2025.",
            "2024-01-01",
        ),
    ]
    facts = extract_facts(sources, "population")
    self.assertEqual(len(facts), 2)
    conflicts = find_contradictions(facts)
    self.assertTrue(conflicts)
    self.assertIn("population", conflicts[0]["claim"])
    freshness = assess_freshness(sources)
    self.assertIn(freshness["level"], {"mixed", "recent", "fresh"})
    self.assertEqual(freshness["newest"], "2026-11-01")
    self.assertEqual(
        assess_freshness([
            source("Old", "https://example.com/old", "old", "2020-01-01")
        ])["level"],
        "stale",
    )

  def test_service_builds_a_complete_report_and_saves_it(self):
    searcher = FakeSearcher(
        [
            source(
                "Python docs",
                "https://docs.python.org/3/library/asyncio.html",
                "Asyncio documentation explains the event loop and tasks.",
                "2026-10-01",
            ),
            source(
                "Tutorial",
                "https://example.com/asyncio",
                "An asyncio tutorial uses 42 examples and tasks.",
                "2025-01-01",
            ),
        ]
    )
    service = ResearchService(searcher, self.config)
    self.addCleanup(service.close)
    result = service.run("python asyncio", mode="docs")
    report = result["report"]
    self.assertIn("Research report", report)
    self.assertIn("Key facts", report)
    self.assertIn("Source comparison", report)
    self.assertIn("Contradictions", report)
    self.assertIn("Citations", report)
    self.assertIn("Source preservation", report)
    self.assertIn("docs.python.org", report)
    self.assertIn("official documentation", searcher.queries[1])
    self.assertEqual(result["mode"], "docs")
    row = service.get(result["id"])
    self.assertEqual(row["query"], "python asyncio")
    self.assertEqual(len(row["sources"]), 2)
    service.save(result["id"], "Asyncio notes")
    saved = service.history(saved=True)
    self.assertEqual(saved[0]["title"], "Asyncio notes")
    service.delete(result["id"])
    with self.assertRaises(ResearchError):
      service.get(result["id"])

  def test_domain_is_validated_and_added_to_the_query(self):
    searcher = FakeSearcher(
        [source("Example", "https://example.com/page", "Useful 12 facts.")]
    )
    service = ResearchService(searcher, self.config)
    self.addCleanup(service.close)
    service.run("example question", domain="example.com")
    self.assertTrue(any("site:example.com" in q for q in searcher.queries))
    with self.assertRaises(ResearchError):
      service.run("example", domain="not a domain")

  def test_history_store_is_bounded_and_retrievable(self):
    store = ResearchStore(Path(self.tmp.name) / "history.db", max_entries=2)
    self.addCleanup(store.close)
    source_item = source("One", "https://example.com/1", "fact 1")
    first = store.add(
        "q1", "general", "", ["q1"], "report one", [source_item], [], [], {}
    )
    store.add("q2", "general", "", ["q2"], "report two", [source_item], [], [], {})
    rows = store.list()
    self.assertEqual(len(rows), 2)
    self.assertEqual(rows[0]["query"], "q2")
    self.assertEqual(store.get(first)["report"], "report one")

  def test_published_metadata_is_extracted(self):
    markup = (
        '<html><head><meta property="article:published_time"'
        ' content="2026-11-08T10:00:00Z"></head></html>'
    )
    self.assertEqual(extract_published(markup), "2026-11-08")


class ResearchIntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_natural_research_intents(self):
    cases = {
        "research python asyncio": "research",
        "compare sources on battery life": "research_compare",
        "find documentation for python asyncio": "research_docs",
        "research example.com for pricing": "research_domain",
        "show my research history": "research_history",
        "show saved research": "research_saved",
    }
    for text, expected in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, expected, text)


class ResearchAssistantTests(unittest.TestCase):
  def test_research_command_and_history(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(base.conversation, enabled=False),
        web=make_config(tmp.name),
    )
    assistant = Assistant(settings, client=FakeClient())
    self.addCleanup(assistant.close)
    assistant._searcher = FakeSearcher(
        [
            source(
                "Research source",
                "https://example.com/research",
                "The answer contains 2026 evidence.",
                "2026-11-01",
            )
        ]
    )
    reply = assistant.reply("/research quantum computing")
    self.assertIn("Research report", reply)
    self.assertIn("Citations", reply)
    self.assertIn("fake", assistant.reply("/research providers"))
    history = assistant.reply("/research history")
    self.assertIn("quantum computing", history)
    self.assertIn("Saved research", assistant.reply("/research save 1 Notes"))
    self.assertIn("Notes", assistant.reply("/research saved"))
    search_reply = assistant.reply("/research search quantum")
    self.assertIn("Research matching", search_reply)
    self.assertIn("Notes", search_reply)
    self.assertIn("Research report", assistant.reply("/research show 1"))


if __name__ == "__main__":
  unittest.main()
