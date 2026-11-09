"""Brave Search API provider (optional, needs an API key)."""

from __future__ import annotations

import logging

from web.base import SearchError, SearchResult, normalize_date

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveProvider:
  """Web search through the Brave Search API."""

  name = "brave"

  def __init__(self, client, api_key, timeout_s=12.0):
    self._client = client
    self._api_key = (api_key or "").strip()
    self._timeout = timeout_s

  def search(self, query, limit, fresh=False):
    if not self._api_key:
      raise SearchError("the Brave API key is not configured")
    params = {"q": query, "count": min(20, max(1, int(limit)))}
    if fresh:
      params["freshness"] = "pw"
    try:
      response = self._client.get(
          SEARCH_URL,
          params=params,
          headers={
              "Accept": "application/json",
              "X-Subscription-Token": self._api_key,
          },
          timeout=self._timeout,
      )
    except Exception as exc:
      raise SearchError(f"Brave request failed: {exc}") from exc

    if response.status_code in (401, 403):
      raise SearchError("Brave rejected the API key")
    if response.status_code == 429:
      raise SearchError("Brave rate limit reached")
    if response.status_code >= 400:
      raise SearchError(f"Brave returned HTTP {response.status_code}")

    try:
      payload = response.json()
    except ValueError as exc:
      raise SearchError(f"Brave returned invalid JSON: {exc}") from exc

    results = []
    for item in payload.get("web", {}).get("results", []):
      title = " ".join(str(item.get("title") or "").split())
      url = str(item.get("url") or "").strip()
      snippet = " ".join(str(item.get("description") or "").split())
      if title and url:
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippet,
                published=normalize_date(
                    item.get("page_age") or item.get("age")
                ),
                provider=self.name,
            )
        )
      if len(results) >= limit:
        break
    if not results:
      raise SearchError("Brave returned no results")
    return results
