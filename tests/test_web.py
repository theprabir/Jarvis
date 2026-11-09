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

from core.assistant import (
    Assistant,
    _has_citation,
    _strip_preamble,
    _supported,
)
from core.config import ConfigError, WebConfig, load_settings
from core.intents import IntentRouter
from web.base import SearchError, SearchResult
from web.extract import PageError, best_passage
from web.search import (
    SearchBundle,
    Searcher,
    _evidence,
    _fetch_pages,
    _query_variants,
    _rank,
    is_fresh,
)
from web.searxng import SearxngProvider
from web.wikipedia import WikipediaProvider
from web.cache import SearchCache


class FakeProvider:
  name = "fake"

  def __init__(self, results=(), error=None):
    self.results = list(results)
    self.error = error
    self.queries = []

  def search(self, query, limit, fresh=False):
    self.queries.append(query)
    if self.error is not None:
      raise self.error
    items = [
        item if isinstance(item, SearchResult)
        else SearchResult.from_dict(item)
        for item in self.results
    ]
    return items[:limit]


class FakeResponse:
  def __init__(self, payload, status=200):
    self.status_code = status
    self._payload = payload

  def json(self):
    return self._payload


class FakeHttp:
  def __init__(self, payload, status=200):
    self.payload = payload
    self.status = status
    self.calls = []

  def get(self, url, **kwargs):
    self.calls.append((url, kwargs))
    return FakeResponse(self.payload, self.status)


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

  def search(self, query, wikipedia=False):
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
    }
    for text, query in cases.items():
      intent = self.router.route(text)
      self.assertIsNotNone(intent, text)
      self.assertEqual(intent.name, "web_search", text)
      self.assertEqual(intent.entities["query"], query, text)

  def test_wikipedia_uses_the_wikipedia_provider(self):
    intent = self.router.route("wikipedia tokyo")
    self.assertEqual(
        intent.entities, {"query": "tokyo", "wikipedia": True}
    )

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

  def test_searxng_provider_needs_a_url(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text('[web]\nprovider = "searxng"\n', encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_searx_url_must_be_http(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text(
          '[web]\nprovider = "searxng"\nsearx_url = "127.0.0.1:8888"\n',
          encoding="utf-8",
      )
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

  def test_grounding_retry_uses_wider_evidence(self):
    body = "tokyo capital japan " * 80
    bundle = SearchBundle(
        "capital of japan",
        [SearchResult("Tokyo", "https://example.com/tokyo", text=body)],
        "[1] Tokyo\n" + "tokyo is the capital of japan",
        "fake",
        sources=[
            SearchResult("Tokyo", "https://example.com/tokyo", text=body)
        ],
        terms=["capital", "japan"],
    )
    assistant, client, _searcher = self.make(
        replies=["NOT_FOUND", "Tokyo is the capital of Japan [1]."],
        bundle=bundle,
    )
    reply = assistant.reply("search the web for capital of japan")
    self.assertIn("Sources:", reply)
    self.assertGreater(
        len(client.calls[1][-1]["content"]),
        len(client.calls[0][-1]["content"]),
    )

  def test_prompt_preamble_is_not_spoken(self):
    assistant, client, _searcher = self.make(
        replies=[
            "Here are the answers to the question using only the evidence"
            " provided:\nTokyo is the capital of Japan [1]."
        ],
        bundle=self.bundle(),
    )
    reply = assistant.reply("search the web for capital of japan")
    self.assertTrue(
        reply.startswith("Tokyo is the capital of Japan [1]")
    )
    self.assertNotIn("Here are the answers", reply)
    self.assertEqual(len(client.calls), 1)

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


class PassageTests(unittest.TestCase):
  def test_passage_finds_a_fact_beyond_the_opening_text(self):
    filler = "The city has many parks. " * 40
    text = filler + "The current population is 122,225,684 people."
    passage = best_passage(text, ["population", "japan"], 200)
    self.assertIn("122,225,684", passage)
    self.assertLessEqual(len(passage), 200)

  def test_short_text_is_returned_whole(self):
    self.assertEqual(
        best_passage("short page", ["short"], 200), "short page"
    )


class FreshnessTests(unittest.TestCase):
  def test_news_questions_are_detected(self):
    self.assertTrue(is_fresh("latest news about python"))
    self.assertTrue(is_fresh("what is the weather in kolkata"))
    self.assertFalse(is_fresh("capital of japan"))

  def test_recent_results_rank_first_for_fresh_questions(self):
    old = SearchResult(
        "AI regulation news", "https://example.com/old",
        snippet="ai regulation news", published="2020-01-01",
    )
    new = SearchResult(
        "AI regulation news", "https://example.com/new",
        snippet="ai regulation news", published="2026-11-08",
    )
    ranked = _rank("latest ai regulation news", [old, new], fresh=True)
    self.assertEqual(ranked[0].url, "https://example.com/new")


class ProviderTests(unittest.TestCase):
  def test_wikipedia_returns_article_text(self):
    payload = {
        "query": {
            "pages": {
                "2": {"index": 2, "title": "Japan", "extract": "x" * 200},
                "1": {
                    "index": 1,
                    "title": "Tokyo",
                    "extract": "Tokyo is the capital of Japan. " * 10,
                },
            }
        }
    }
    http = FakeHttp(payload)
    results = WikipediaProvider(http, 5).search("tokyo", 2)
    self.assertEqual(results[0].title, "Tokyo")
    self.assertIn("en.wikipedia.org/wiki/Tokyo", results[0].url)
    self.assertIn("capital of Japan", results[0].text)
    self.assertTrue(http.calls[0][1]["headers"]["User-Agent"])

  def test_searxng_reads_results_and_dates(self):
    payload = {
        "results": [
            {
                "title": "AI rules",
                "url": "https://example.com/a",
                "content": "new rules",
                "publishedDate": "2026-11-06T10:00:00",
            }
        ]
    }
    http = FakeHttp(payload)
    results = SearxngProvider(http, "http://localhost:8888/").search(
        "ai", 3
    )
    self.assertEqual(results[0].published, "2026-11-06")
    self.assertEqual(http.calls[0][0], "http://localhost:8888/search")

  def test_searxng_http_error_is_reported(self):
    with self.assertRaises(SearchError):
      SearxngProvider(
          FakeHttp({}, 502), "http://localhost:8888"
      ).search("ai", 3)


class FetchTests(unittest.TestCase):
  def test_wikipedia_prefers_an_exact_title_match(self):
    payload = {
        "query": {
            "pages": {
                "1": {
                    "index": 1,
                    "title": "Tokyo",
                    "extract": "Tokyo is the capital of Japan. " * 8,
                },
                "2": {
                    "index": 2,
                    "title": "Tokyo!",
                    "extract": "Tokyo! is a 2008 anthology film. " * 8,
                },
            }
        }
    }
    results = WikipediaProvider(FakeHttp(payload), 5).search("tokyo", 6)
    self.assertEqual([item.title for item in results], ["Tokyo"])

  def test_blocked_pages_are_skipped_for_readable_sources(self):
    results = [
        SearchResult(f"R{index}", f"https://site{index}.example/page")
        for index in range(1, 6)
    ]
    fetched = []

    def fake_download(client, url, **kwargs):
      fetched.append(url)
      if "site1" in url or "site2" in url:
        raise PageError("HTTP 403")
      return (
          "<html><body><p>" + ("readable text " * 20) + "</p></body></html>"
      )

    config = SimpleNamespace(
        extract_pages=2, timeout_s=10, max_page_bytes=1000
    )
    from unittest import mock

    with mock.patch("web.search.download", fake_download):
      _fetch_pages(None, results, config)
    self.assertEqual(sum(1 for item in results if item.text), 2)
    self.assertGreaterEqual(len(fetched), 4)

  def test_snippet_only_sources_are_labelled(self):
    evidence = _evidence(
        [
            SearchResult(
                "Tokyo",
                "https://example.com/tokyo",
                snippet="tokyo is the capital",
            )
        ],
        500,
    )
    self.assertIn("snippet only", evidence)


class GroundingTests(unittest.TestCase):
  def test_prompt_echo_is_removed(self):
    answer = (
        "Here are the answers to the question in one or two sentences,"
        " using only the evidence provided:\n"
        "Tokyo is the capital of Japan [1]."
    )
    self.assertEqual(
        _strip_preamble(answer), "Tokyo is the capital of Japan [1]."
    )

  def test_a_real_answer_is_left_alone(self):
    self.assertEqual(
        _strip_preamble("Tokyo is the capital [1]."),
        "Tokyo is the capital [1].",
    )

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
