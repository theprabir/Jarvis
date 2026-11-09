"""Grounded web search: query variants, ranking, extraction and evidence.

One request fans out to a small number of query variants, merges and dedupes
the hits by URL, filters results that share nothing with the question, fetches
candidate pages until enough readable sources are found, and builds a
character-bounded evidence block that the model is only allowed to answer from.

News-shaped questions also use the provider's news results, whose publication
dates let recent pages outrank older ones. The evidence for each source is the
passage that best matches the question, not merely the top of the page.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from urllib.parse import urlsplit

import httpx

from web.base import (
    STOPWORDS,
    SearchError,
    SearchResult,
    normalize_date,
    normalize_url,
    words,
)
from web.brave import BraveProvider
from web.cache import SearchCache
from web.duckduckgo import DuckDuckGoProvider
from web.extract import best_passage, download, extract_text
from web.searxng import SearxngProvider
from web.wikipedia import WikipediaProvider

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 8.0
# Extra candidates fetched (and skipped when blocked) until enough readable
# sources exist, so a snippet-only page cannot starve the evidence.
FETCH_OVERSCAN = 3
# Bumped when stored results change shape, so old entries are never reused.
CACHE_VERSION = 5
# Words that mean the answer must be current; they switch on date ranking.
FRESH_WORDS = frozenset(
    {
        "current",
        "currently",
        "latest",
        "new",
        "newest",
        "news",
        "now",
        "recent",
        "recently",
        "today",
        "tonight",
        "trending",
        "update",
        "updates",
        "weather",
    }
)
SKIP_HOSTS = ("youtube.com", "youtu.be", "twitter.com", "x.com")
SKIP_SUFFIXES = (
    ".pdf",
    ".zip",
    ".mp3",
    ".mp4",
    ".avi",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
)


def is_fresh(query):
  """Whether a question asks for current information."""
  tokens = set(words(query))
  if tokens & FRESH_WORDS:
    return True
  year = date.today().year
  return any(
      token.isdigit() and year - 1 <= int(token) <= year + 1
      for token in tokens
  )


class SearchBundle:
  """The evidence for one question plus the sources that produced it."""

  def __init__(
      self,
      query,
      results,
      evidence,
      provider,
      from_cache=False,
      sources=None,
      terms=(),
  ):
    self.query = query
    self.results = tuple(results)
    self.sources = tuple(
        sources if sources is not None else results[:3]
    )
    self.evidence = evidence
    self.provider = provider
    self.from_cache = from_cache
    self.terms = tuple(terms)

  @property
  def empty(self):
    return not self.evidence

  def wide_evidence(self):
    """Evidence with a doubled budget, used for one grounded retry."""
    return _evidence(
        self.sources, max(len(self.evidence) * 2, 1200), self.terms
    )


class Searcher:
  """Search the web and return bounded, ranked, cited evidence."""

  def __init__(self, config, client=None, providers=None):
    self._config = config
    self._client = client or httpx.Client(
        timeout=config.timeout_s, follow_redirects=True
    )
    self._owns_client = client is None
    self._providers = providers or self._build_providers()
    self._wikipedia = WikipediaProvider(
        self._client, config.timeout_s
    )
    self._cache = SearchCache(
        config.resolved_cache_db, config.cache_ttl_s, config.cache_entries
    )

  def _build_providers(self):
    provider = self._config.provider
    key = os.environ.get(self._config.brave_api_key_env, "").strip()
    searx = (getattr(self._config, "searx_url", "") or "").strip()
    if provider == "searxng":
      if not searx:
        log.warning("[web] searx_url is empty; using DuckDuckGo")
      else:
        return [SearxngProvider(self._client, searx, self._config.timeout_s)]
    if provider == "duckduckgo":
      return [DuckDuckGoProvider(self._client, self._config.timeout_s)]
    if provider == "brave" and not key:
      log.warning(
          "Brave is configured but %s is empty; using DuckDuckGo",
          self._config.brave_api_key_env,
      )

    chain = []
    if key and provider in {"auto", "brave"}:
      chain.append(
          BraveProvider(self._client, key, self._config.timeout_s)
      )
    if searx and provider == "auto":
      chain.append(
          SearxngProvider(self._client, searx, self._config.timeout_s)
      )
    chain.append(DuckDuckGoProvider(self._client, self._config.timeout_s))
    return chain

  def close(self):
    self._cache.close()
    if self._owns_client:
      self._client.close()

  def search(self, query, wikipedia=False):
    """Run one question through the provider chain and return its evidence."""
    query = " ".join((query or "").split())
    if not query:
      raise SearchError("the search query is empty")

    fresh = is_fresh(query)
    terms = [word for word in words(query) if word not in STOPWORDS]
    key = (
        f"v{CACHE_VERSION}|{self._config.provider}"
        f"|{'wiki' if wikipedia else ''}|{query.lower()}"
        f"|{self._config.max_results}"
    )
    cached = self._cache.get(key)
    if cached:
      log.info("Search cache hit for %r", query)
      return self._bundle(query, cached, "cache", terms, from_cache=True)

    results = []
    errors = []
    used_wikipedia = False
    if wikipedia:
      try:
        results.extend(
            self._wikipedia.search(query, self._config.max_results)
        )
        used_wikipedia = bool(results)
      except SearchError as exc:
        errors.append(str(exc))
        log.info("Wikipedia lookup %r failed: %s", query, exc)
    if not results:
      for variant in _query_variants(query, self._config.max_queries):
        try:
          results.extend(self._search_one(variant, fresh=fresh))
        except SearchError as exc:
          errors.append(str(exc))
          log.info("Query variant %r failed: %s", variant, exc)

    ranked = _rank(query, results, fresh=fresh)[: self._config.max_results]
    if not ranked:
      stale = self._cache.get(key, allow_stale=True)
      if stale:
        log.info("Using a cached result for %r (search unavailable)", query)
        return self._bundle(
            query, stale, "cache", terms, from_cache=True
        )
      raise SearchError(
          errors[0] if errors else "no relevant results were found"
      )

    if self._config.extract_pages and not wikipedia:
      _fetch_pages(self._client, ranked, self._config)
    self._cache.put(key, ranked)
    provider = (
        "wikipedia" if used_wikipedia else self._providers[0].name
    )
    return self._bundle(query, ranked, provider, terms)

  def _bundle(self, query, results, provider, terms, from_cache=False):
    sources = _select_sources(results, 3)
    evidence = _evidence(sources, self._config.context_chars, terms)
    return SearchBundle(
        query,
        results,
        evidence,
        provider,
        from_cache,
        sources,
        terms,
    )

  def _search_one(self, query, fresh=False):
    last = None
    for provider in self._providers:
      try:
        return provider.search(
            query, self._config.max_results, fresh=fresh
        )
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


def _rank(query, results, fresh=False):
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
        result.text = result.text or existing.text
        result.published = result.published or existing.published
      unique[key] = result
  ordered = sorted(
      unique.values(),
      key=lambda item: (item.score, _freshness(item, fresh)),
      reverse=True,
  )
  return ordered


def _freshness(result, fresh):
  """A small bonus for recently published results when recency matters."""
  if not fresh:
    return 0.0
  published = normalize_date(result.published)
  if not published:
    return 0.0
  try:
    when = datetime.strptime(published, "%Y-%m-%d").date()
  except ValueError:
    return 0.0
  days = (date.today() - when).days
  if days < 0:
    return 0.0
  if days <= 3:
    return 0.3
  if days <= 30:
    return 0.2
  if days <= 365:
    return 0.1
  return 0.0


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


def _select_sources(results, limit=3):
  """Prefer sources with extracted text so evidence is more than a snippet."""
  ordered = sorted(
      results,
      key=lambda item: (1 if item.text else 0, item.score),
      reverse=True,
  )
  return ordered[:limit]


def _fetch_pages(client, results, config):
  """Attach page text until ``extract_pages`` readable sources exist."""
  target = max(1, int(config.extract_pages))
  readable = 0
  for result in results[: target + FETCH_OVERSCAN]:
    if readable >= target:
      break
    if _skip_fetch(result.url):
      log.info("Skipping %s (no article text)", result.url)
      continue
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
      readable += 1


def _skip_fetch(url):
  """Whether a URL is known to carry no readable article text."""
  parts = urlsplit(url or "")
  host = parts.netloc.lower()
  if host.startswith("www."):
    host = host[4:]
  if any(host == blocked or host.endswith("." + blocked)
         for blocked in SKIP_HOSTS):
    return True
  return parts.path.lower().endswith(SKIP_SUFFIXES)


def _evidence(results, max_chars, terms=()):
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
    if result.published:
      header += f" {result.published}"
    if result.text:
      body = best_passage(result.text, terms, per_source)
    else:
      body = result.snippet
      header += " (snippet only)"
    block = f"{header}\n{body}".strip()
    remaining = max_chars - used
    if remaining < 120:
      break
    if len(block) > remaining:
      block = block[:remaining].rstrip()
    blocks.append(block)
    used += len(block) + 2
  return "\n\n".join(blocks)[:max_chars]
