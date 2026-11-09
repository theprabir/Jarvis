"""SearXNG provider for a self-hosted meta-search instance.

DuckDuckGo scraping can break without notice and Brave needs a paid key, so a
local SearXNG instance (``docker run searxng/searxng``) is the dependable
option without an account. It is used only when ``[web] searx_url`` is set.
"""

from __future__ import annotations

import logging

from web.base import SearchError, SearchResult, normalize_date

log = logging.getLogger(__name__)

FRESH_RANGE = "week"


class SearxngProvider:
  """Web search through a SearXNG instance's JSON API."""

  name = "searxng"

  def __init__(self, client, base_url, timeout_s=12.0):
    self._client = client
    self._base = (base_url or "").rstrip("/")
    self._timeout = timeout_s

  def search(self, query, limit, fresh=False):
    if not self._base:
      raise SearchError("the SearXNG URL is not configured")
    params = {"q": query, "format": "json", "safesearch": "1"}
    if fresh:
      params["time_range"] = FRESH_RANGE
    try:
      response = self._client.get(
          f"{self._base}/search", params=params, timeout=self._timeout
      )
    except Exception as exc:
      raise SearchError(f"SearXNG request failed: {exc}") from exc

    if response.status_code >= 400:
      raise SearchError(f"SearXNG returned HTTP {response.status_code}")
    try:
      payload = response.json()
    except ValueError as exc:
      raise SearchError(f"SearXNG returned invalid JSON: {exc}") from exc

    results = []
    for item in payload.get("results", []):
      title = " ".join(str(item.get("title") or "").split())
      url = str(item.get("url") or "").strip()
      snippet = " ".join(str(item.get("content") or "").split())
      if not title or not url:
        continue
      results.append(
          SearchResult(
              title=title,
              url=url,
              snippet=snippet,
              published=normalize_date(item.get("publishedDate")),
              provider=self.name,
          )
      )
      if len(results) >= limit:
        break
    if not results:
      raise SearchError("SearXNG returned no results")
    return results
