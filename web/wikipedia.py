"""Wikipedia lookup through the MediaWiki Action API.

The REST endpoint refuses automated clients with HTTP 403 ("set a user-agent
and respect our robot policy"), but the Action API accepts a descriptive
User-Agent. ``generator=search`` returns the matching articles together with
their plain-text introductions in a single request.
"""

from __future__ import annotations

import logging

from web.base import SearchError, SearchResult

log = logging.getLogger(__name__)

API_URL = "https://en.wikipedia.org/w/api.php"
# Wikimedia refuses a User-Agent without contact information (HTTP 403), so
# the request identifies the client and links to the API etiquette page.
USER_AGENT = (
    "JARVIS/0.1 (local Windows personal assistant; "
    "+https://www.mediawiki.org/wiki/API:Etiquette) python-httpx"
)


class WikipediaProvider:
  """Look up articles on English Wikipedia."""

  name = "wikipedia"

  def __init__(self, client, timeout_s=12.0):
    self._client = client
    self._timeout = timeout_s

  def search(self, query, limit, fresh=False):
    term = " ".join((query or "").split())
    if not term:
      raise SearchError("the Wikipedia lookup is empty")
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": term,
        "gsrlimit": str(min(3, max(1, int(limit)))),
        "prop": "extracts",
        "exintro": "1",
        "explaintext": "1",
        "redirects": "1",
    }
    try:
      response = self._client.get(
          API_URL,
          params=params,
          headers={"User-Agent": USER_AGENT},
          timeout=self._timeout,
      )
    except Exception as exc:
      raise SearchError(f"Wikipedia request failed: {exc}") from exc

    if response.status_code >= 400:
      raise SearchError(f"Wikipedia returned HTTP {response.status_code}")
    try:
      payload = response.json()
    except ValueError as exc:
      raise SearchError(f"Wikipedia returned invalid JSON: {exc}") from exc

    pages = payload.get("query", {}).get("pages", {})
    ordered = sorted(
        pages.values(), key=lambda page: page.get("index") or 0
    )
    results = []
    for page in ordered:
      title = " ".join(str(page.get("title") or "").split())
      extract = " ".join(str(page.get("extract") or "").split())
      if not title or len(extract) < 120:
        continue
      results.append(
          SearchResult(
              title=title,
              url=(
                  "https://en.wikipedia.org/wiki/"
                  + title.replace(" ", "_")
              ),
              snippet=extract[:300],
              text=extract,
              provider=self.name,
          )
      )
      if len(results) >= limit:
        break
    if not results:
      raise SearchError(f"Wikipedia has no article for {term!r}")
    # An exact title ("tokyo" -> "Tokyo") is the article the user meant;
    # otherwise near matches such as "Tokyo!" (a film) would be offered too.
    exact = [
        item for item in results if item.title.lower() == term.lower()
    ]
    if exact:
      results = exact
    return results[: max(1, min(2, int(limit)))]
