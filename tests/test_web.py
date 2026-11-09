"""Feature set 4.5 tests: ranking, cache, intents and grounded answers.

No network is used: providers are injected fakes, and page extraction is
disabled so the suite stays offline and fast.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.assistant import Assistant, _has_citation, _supported
from core.config import ConfigError, WebConfig, load_settings
from core.intents import IntentRouter
from web.base import SearchError, SearchResult
from web.cache import SearchCache
from web.search import SearchBundle, Searcher, _evidence, _query_variants, _rank


class FakeProvider:
  name = "fake"

  def __init__(self, results=(), error=None):
    self.results = list(results)
    self.error = error
    self.queries = []

  def search(self, query, limit):
    self.queries.append(query)
    if self.error is not None:
      raise self.error
    items = [
        item if isinstance(item, SearchResult)
        else SearchResult.from_dict(item)
        for item in self.results
    ]
    return items[:limit]


class FakeClient:
  def __init__(self, replies=("Tokyo is the capital of Japan [1].",)):
    self.replies = list(replies)
    self.calls = []

  def chat(self, messages, max_tokens=None, temperature=None):
    self.calls.append(messages)
    content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
    return SimpleNamespace(
        content=content,
        total_ms=10.0,
        completion_tokens=8,
        generated_tokens_per_second=5.0,
        finish_reason="stop",
    )


class FakeSearcher:
  def __init__(self, bundle=None, error=None):
    self.bundle = bundle
    self.error = error
    self.queries = []

  def search(self, query):
    self.queries.append(query)
    if self.error is not None:
      raise self.error
    return self.bundle

  def close(self):
    pass


def make_config(tmp, **overrides):
  options = {"cache_db": str(Path(tmp) / "cache.db"), "extract_pages": 0}
  options.update(overrides)
  return WebConfig(**options)


class RankingTests(unittest.TestCase):
  def test_duplicate_urls_are_merged(self):
    ranked = _rank(
        "python asyncio tutorial",
        [
            SearchResult("Asyncio tutorial", "https://example.com/a"),
            SearchResult(
                "Asyncio tutorial", "https://www.example.com/a/",
                snippet="python asyncio tutorial",
            ),
        ],
    )
    self.assertEqual(len(ranked), 1)
    self.assertEqual(ranked[0].url, "https://www.example.com/a/")

  def test_unrelated_results_are_filtered_out(self):
    ranked = _rank(
        "tokyo population",
        [
            SearchResult("Gardening tips", "https://example.com/garden",
                         "how to grow tomatoes"),
            SearchResult("Weather", "https://example.com/weather",
                         "rain expected tomorrow"),
        ],
    )
    self.assertEqual(ranked, [])

  def test_title_matches_rank_first(self):
    ranked = _rank(
        "tokyo population",
        [
            SearchResult("City facts", "https://example.com/facts",
                         "tokyo population is large"),
            SearchResult("Tokyo population 2026", "https://example.com/tokyo"),
        ],
    )
    self.assertEqual(ranked[0].title, "Tokyo population 2026")

  def test_query_variants_drop_stopwords(self):
    variants = _query_variants("what is the capital of japan", 2)
    self.assertEqual(variants[0], "what is the capital of japan")
    self.assertEqual(variants[1], "capital japan")

  def test_site_filter_is_not_stripped(self):
    variants = _query_variants("tokyo site:en.wikipedia.org", 2)
    self.assertEqual(variants, ["tokyo site:en.wikipedia.org"])

  def test_evidence_respects_the_budget(self):
    results = [
        SearchResult(
            f"Result {index}", f"https://example.com/{index}",
            text="tokyo population " * 100,
        )
        for index in range(1, 5)
    ]
    evidence = _evidence(results, 500)
    self.assertLessEqual(len(evidence), 500)
    self.assertIn("[1]", evidence)


  def test_evidence_is_limited_to_three_sources(self):
    results = [
        SearchResult(
            f"Result {index}", f"https://example.com/{index}", text="short"
        )
        for index in range(1, 6)
    ]
    evidence = _evidence(results, 2000)
    self.assertIn("[3]", evidence)
    self.assertNotIn("[4]", evidence)


class CacheTests(unittest.TestCase):
  def test_entries_expire_and_can_be_read_stale(self):
    with tempfile.TemporaryDirectory() as tmp:
      cache = SearchCache(Path(tmp) / "cache.db", ttl_s=0, max_entries=4)
      try:
        result = SearchResult("Tokyo", "https://example.com/tokyo")
        cache.put("key", [result])
        self.assertIsNone(cache.get("key"))
        stale = cache.get("key", allow_stale=True)
        self.assertIsNotNone(stale)
        self.assertEqual(stale[0].title, "Tokyo")
      finally:
        cache.close()

  def test_cache_is_bounded(self):
    with tempfile.TemporaryDirectory() as tmp:
      cache = SearchCache(Path(tmp) / "cache.db", ttl_s=3600, max_entries=2)
      try:
        for index in range(4):
          cache.put(
              f"key{index}",
              [SearchResult(f"Title {index}", f"https://example.com/{index}")],
          )
        kept = [
            index for index in range(4)
            if cache.get(f"key{index}") is not None
        ]
        self.assertLessEqual(len(kept), 2)
      finally:
        cache.close()


class SearcherTests(unittest.TestCase):
  def make(self, provider, **overrides):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    searcher = Searcher(
        make_config(tmp.name, **overrides), providers=[provider]
    )
    self.addCleanup(searcher.close)
    return searcher

  def test_repeat_query_is_served_from_cache(self):
    provider = FakeProvider(
        [
            {"title": "Tokyo", "url": "https://example.com/tokyo",
             "snippet": "tokyo is the capital of japan"},
        ]
    )
    searcher = self.make(provider)
    first = searcher.search("capital of japan")
    second = searcher.search("capital of japan")
    self.assertEqual(len(first.results), 1)
    self.assertTrue(second.from_cache)
    # Two variants on the first search, none on the cached second search.
    self.assertEqual(len(provider.queries), 2)

  def test_stale_cache_survives_an_offline_search(self):
    provider = FakeProvider(
        [
            {"title": "Tokyo", "url": "https://example.com/tokyo",
             "snippet": "tokyo is the capital of japan"},
        ]
    )
    searcher = self.make(provider, cache_ttl_s=0)
    searcher.search("capital of japan")
    provider.error = SearchError("no internet connection")
    bundle = searcher.search("capital of japan")
    self.assertTrue(bundle.from_cache)
    self.assertIn("Tokyo", bundle.evidence)

  def test_empty_results_raise(self):
    searcher = self.make(FakeProvider([]))
    with self.assertRaises(SearchError):
      searcher.search("capital of japan")

  def test_provider_failure_is_reported(self):
    searcher = self.make(FakeProvider(error=SearchError("no internet")))
    with self.assertRaises(SearchError):
      searcher.search("capital of japan")


class WebIntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter(
        allowed_apps=("notepad",), allowed_commands=("hostname",)
    )

  def test_web_phrases(self):
    cases = {
        "search the web for python asyncio": "python asyncio",
        "look up the weather in kolkata": "the weather in kolkata",
        "google rust ownership rules": "rust ownership rules",
        "find information about the voyager probes": "the voyager probes",
        "latest news about ai regulation": "ai regulation news",
        "wikipedia tokyo": "tokyo site:en.wikipedia.org",
    }
    for text, query in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, "web_search", text)
      self.assertEqual(intent.entities["query"], query, text)

  def test_web_command(self):
    intent = self.router.route("/web quantum computing")
    self.assertEqual(intent.name, "web_search")
    self.assertEqual(intent.entities["query"], "quantum computing")

  def test_file_search_is_not_web_search(self):
    self.assertEqual(
        self.router.route("search for notes in drafts").name, "search_files"
    )

  def test_open_questions_stay_with_the_model(self):
    for text in (
        "explain how file permissions work",
        "tell me a story about a lighthouse",
    ):
      self.assertIsNone(self.router.route(text), text)


class ConfigTests(unittest.TestCase):
  def test_project_config_has_web(self):
    web = load_settings().web
    self.assertTrue(web.enabled)
    self.assertEqual(web.provider, "auto")
    self.assertEqual(web.context_chars, 1000)
    self.assertTrue(web.resolved_cache_db.is_absolute())

  def test_invalid_provider_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text('[web]\nprovider = "bing"\n', encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_context_budget_is_validated(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[web]\ncontext_chars = 100\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


class GroundedAnswerTests(unittest.TestCase):
  def make(self, replies=(), bundle=None, error=None):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(base.conversation, enabled=False),
        web=dataclasses.replace(
            base.web, cache_db=str(Path(tmp.name) / "cache.db")
        ),
    )
    client = FakeClient(list(replies) or ["Tokyo is the capital of Japan [1]."])
    assistant = Assistant(settings, client=client)
    self.addCleanup(assistant.close)
    searcher = FakeSearcher(bundle=bundle, error=error)
    assistant._searcher = searcher
    return assistant, client, searcher

  def bundle(self):
    return SearchBundle(
        "capital of japan",
        [
            SearchResult(
                "Tokyo",
                "https://en.wikipedia.org/wiki/Tokyo",
                snippet="tokyo is the capital of japan",
            )
        ],
        "[1] Tokyo (https://en.wikipedia.org/wiki/Tokyo)\n"
        "tokyo is the capital of japan",
        "fake",
    )

  def test_answer_carries_sources(self):
    assistant, client, searcher = self.make(bundle=self.bundle())
    reply = assistant.reply("search the web for capital of japan")
    self.assertIn("Tokyo is the capital of Japan [1]", reply)
    self.assertIn("Sources:", reply)
    self.assertIn("https://en.wikipedia.org/wiki/Tokyo", reply)
    self.assertEqual(searcher.queries, ["capital of japan"])
    self.assertIn("[1]", client.calls[0][-1]["content"])

  def test_web_command_uses_the_searcher(self):
    assistant, _client, searcher = self.make(bundle=self.bundle())
    reply = assistant.reply("/web capital of japan")
    self.assertIn("Sources:", reply)
    self.assertEqual(searcher.queries, ["capital of japan"])

  def test_not_found_marker_is_not_spoken_to_the_user(self):
    assistant, _client, _searcher = self.make(
        replies=["NOT_FOUND"], bundle=self.bundle()
    )
    reply = assistant.reply("search the web for the mayor of atlantis")
    self.assertNotIn("NOT_FOUND", reply)
    self.assertIn("none of them actually answered", reply)

  def test_refusal_is_retried_with_a_direct_prompt(self):
    assistant, client, _searcher = self.make(
        replies=["NOT_FOUND", "Tokyo is the capital of Japan [1]."],
        bundle=self.bundle(),
    )
    reply = assistant.reply("search the web for capital of japan")
    self.assertIn("Tokyo is the capital of Japan [1]", reply)
    self.assertIn("Sources:", reply)
    self.assertEqual(len(client.calls), 2)

  def test_uncited_answer_is_never_spoken(self):
    assistant, _client, _searcher = self.make(
        replies=["Tokyo is the capital of Japan."], bundle=self.bundle()
    )
    reply = assistant.reply("search the web for capital of japan")
    self.assertIn("none of them actually answered", reply)
    self.assertNotIn("Sources:", reply)

  def test_fabricated_figure_is_rejected(self):
    assistant, _client, _searcher = self.make(
        replies=["The population is 999 million [1]."], bundle=self.bundle()
    )
    reply = assistant.reply("search the web for capital of japan")
    self.assertIn("none of them actually answered", reply)

  def test_empty_evidence_says_so(self):
    assistant, client, _searcher = self.make(
        bundle=SearchBundle("q", [], "", "fake")
    )
    reply = assistant.reply("search the web for the mayor of atlantis")
    self.assertIn("couldn't find anything reliable", reply)
    self.assertEqual(client.calls, [])

  def test_search_failure_is_reported_without_inventing(self):
    assistant, client, _searcher = self.make(
        error=SearchError("no internet connection")
    )
    reply = assistant.reply("search the web for capital of japan")
    self.assertIn("couldn't search the web", reply)
    self.assertIn("no internet connection", reply)
    self.assertEqual(client.calls, [])


class GroundingTests(unittest.TestCase):
  def test_citations_must_point_at_a_source(self):
    self.assertTrue(_has_citation("Tokyo [2].", 3))
    self.assertFalse(_has_citation("Tokyo [4].", 3))
    self.assertFalse(_has_citation("Tokyo.", 3))

  def test_figures_must_appear_in_the_evidence(self):
    evidence = "japan population was 123.4 million in 2025"
    self.assertTrue(_supported("About 123.4 million [1].", evidence))
    self.assertTrue(_supported("Roughly 123 million [1].", evidence))
    self.assertFalse(_supported("About 999 million [1].", evidence))

  def test_terms_must_appear_in_the_evidence(self):
    self.assertTrue(_supported("Tokyo is the capital.", "tokyo japan capital"))
    self.assertFalse(_supported("Atlantis sank.", "tokyo japan capital"))


if __name__ == "__main__":
  unittest.main()
