"""Grounded web search: query variants, ranking, extraction and evidence.

One request fans out to a small number of query variants, merges and dedupes
the hits by URL, filters results that share nothing with the question, fetches
the top pages, and builds a character-bounded evidence block that the model is
only allowed to answer from.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from web.base import (
    STOPWORDS,
    SearchError,
    SearchResult,
    normalize_url,
    words,
)
from web.brave import BraveProvider
from web.cache import SearchCache
from web.duckduckgo import DuckDuckGoProvider
from web.extract import download, extract_text

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 8.0
# Bumped when stored results change shape, so old entries are never reused.
CACHE_VERSION = 2


class SearchBundle:
  """The evidence for one question plus the sources that produced it."""

  def __init__(self, query, results, evidence, provider, from_cache=False):
    self.query = query
    self.results = tuple(results)
    self.evidence = evidence
    self.provider = provider
    self.from_cache = from_cache

  @property
  def empty(self):
    return not self.evidence


class Searcher:
  """Search the web and return bounded, ranked, cited evidence."""

  def __init__(self, config, client=None, providers=None):
    self._config = config
    self._client = client or httpx.Client(
        timeout=config.timeout_s, follow_redirects=True
    )
    self._owns_client = client is None
    self._providers = providers or self._build_providers()
    self._cache = SearchCache(
        config.resolved_cache_db, config.cache_ttl_s, config.cache_entries
    )

  def _build_providers(self):
    key = os.environ.get(self._config.brave_api_key_env, "").strip()
    if key and self._config.provider in {"auto", "brave"}:
      return [BraveProvider(self._client, key, self._config.timeout_s)]
    if self._config.provider == "brave":
      log.warning(
          "Brave is configured but %s is empty; using DuckDuckGo",
          self._config.brave_api_key_env,
      )
    return [DuckDuckGoProvider(self._client, self._config.timeout_s)]

  def close(self):
    self._cache.close()
    if self._owns_client:
      self._client.close()

  def search(self, query):
    """Run one question through the provider chain and return its evidence."""
    query = " ".join((query or "").split())
    if not query:
      raise SearchError("the search query is empty")

    key = (
        f"v{CACHE_VERSION}|{self._config.provider}|"
        f"{query.lower()}|{self._config.max_results}"
    )
    cached = self._cache.get(key)
    if cached:
      log.info("Search cache hit for %r", query)
      return SearchBundle(
          query, cached, _evidence(cached, self._config.context_chars),
          "cache", True,
      )

    results = []
    errors = []
    for variant in _query_variants(query, self._config.max_queries):
      try:
        results.extend(self._search_one(variant))
      except SearchError as exc:
        errors.append(str(exc))
        log.info("Query variant %r failed: %s", variant, exc)

    ranked = _rank(query, results)[: self._config.max_results]
    if not ranked:
      stale = self._cache.get(key, allow_stale=True)
      if stale:
        log.info("Using a cached result for %r (search unavailable)", query)
        return SearchBundle(
            query, stale, _evidence(stale, self._config.context_chars),
            "cache", True,
        )
      raise SearchError(
          errors[0] if errors else "no relevant results were found"
      )

    if self._config.extract_pages:
      _fetch_pages(self._client, ranked, self._config)
    evidence = _evidence(ranked, self._config.context_chars)
    self._cache.put(key, ranked)
    return SearchBundle(query, ranked, evidence, self._providers[0].name)

  def _search_one(self, query):
    last = None
    for provider in self._providers:
      try:
        return provider.search(query, self._config.max_results)
      except SearchError as exc:
        last = exc
        log.info("Provider %s failed: %s", provider.name, exc)
    raise last or SearchError("no search provider is available")


def _query_variants(query, limit):
  """Return the question plus a keyword form, capped by ``limit``."""
  base = " ".join(query.split())
  if "site:" in base or limit <= 1:
    return [base]
  terms = [
      word for word in words(base)
      if len(word) > 2 and word not in STOPWORDS
  ]
  variants = [base]
  if terms:
    keywords = " ".join(dict.fromkeys(terms))[:100]
    if keywords and keywords != base.lower():
      variants.append(keywords)
  return variants[: max(1, int(limit))]


def _rank(query, results):
  """Dedupe by URL, score by term overlap and sort best first."""
  terms = [word for word in words(query) if word not in STOPWORDS]
  unique = {}
  for result in results:
    key = normalize_url(result.url)
    if not key:
      continue
    score = _relevance(terms, result)
    if terms and score <= 0.0:
      continue
    result.score = score
    existing = unique.get(key)
    if existing is None or score > existing.score:
      if existing is not None:
        result.snippet = result.snippet or existing.snippet
      unique[key] = result
  return sorted(unique.values(), key=lambda item: item.score, reverse=True)


def _relevance(terms, result):
  if not terms:
    return 0.0
  wanted = set(terms)
  title = set(words(result.title))
  link = set(words(result.url))
  body = set(words(f"{result.snippet} {result.text[:500]}"))
  total = 0.0
  for term in wanted:
    if term in title:
      total += 2.0
    elif term in body or term in link:
      total += 1.0
  return total / (2.0 * len(wanted))


def _fetch_pages(client, results, config):
  """Attach extracted page text to the best results, best effort."""
  for result in results[: config.extract_pages]:
    try:
      markup = download(
          client,
          result.url,
          timeout_s=min(FETCH_TIMEOUT_S, config.timeout_s),
          max_bytes=config.max_page_bytes,
      )
    except Exception as exc:
      log.info("Could not fetch %s: %s", result.url, exc)
      continue
    text = extract_text(markup, result.url)
    if text:
      result.text = text


def _evidence(results, max_chars):
  """Build the numbered evidence block the model may answer from."""
  if not results:
    return ""
  source_count = min(len(results), 3)
  per_source = max(200, max_chars // max(1, source_count))
  blocks = []
  used = 0
  for index, result in enumerate(results[:source_count], 1):
    header = f"[{index}] {result.title}"
    if result.url:
      header += f" ({result.url})"
    body = result.content(per_source)
    block = f"{header}\n{body}".strip()
    remaining = max_chars - used
    if remaining < 120:
      break
    if len(block) > remaining:
      block = block[:remaining].rstrip()
    blocks.append(block)
    used += len(block) + 2
  return "\n\n".join(blocks)[:max_chars]
