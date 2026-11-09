"""DuckDuckGo search provider.

The maintained ``ddgs`` client is preferred; the older ``duckduckgo_search``
package and the HTML endpoint are fallbacks, so a blocked or rate-limited
client still has a way to return results.
"""

from __future__ import annotations

import logging
import warnings
from urllib.parse import parse_qs, unquote, urlsplit

from lxml import html as lxml_html

from web.base import SearchError, SearchResult, normalize_date

log = logging.getLogger(__name__)

SEARCH_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _ddgs_class():
  """Return an importable DuckDuckGo client class, or ``None``."""
  for module in ("ddgs", "duckduckgo_search"):
    try:
      with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        imported = __import__(module, fromlist=["DDGS"])
      return imported.DDGS
    except ImportError:
      continue
  return None


class DuckDuckGoProvider:
  """Text search through DuckDuckGo."""

  name = "duckduckgo"

  def __init__(self, client, timeout_s=12.0):
    self._client = client
    self._timeout = timeout_s

  def search(self, query, limit, fresh=False):
    results = self._via_news(query, limit) if fresh else []
    if not results:
      results = self._via_client(query, limit)
    if not results:
      results = self._via_html(query, limit)
    if not results:
      raise SearchError("DuckDuckGo returned no results")
    return results

  def _via_client(self, query, limit):
    client_class = _ddgs_class()
    if client_class is None:
      return []
    try:
      with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with client_class() as ddgs:
          rows = list(ddgs.text(query, max_results=limit))
    except Exception as exc:
      log.info("DuckDuckGo client failed (%s); trying the HTML endpoint", exc)
      return []
    results = []
    for row in rows:
      title = " ".join(str(row.get("title") or "").split())
      url = str(row.get("href") or row.get("url") or "").strip()
      snippet = " ".join(str(row.get("body") or "").split())
      if title and url:
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippet,
                published=normalize_date(row.get("date")),
                provider=self.name,
            )
        )
      if len(results) >= limit:
        break
    return results

  def _via_news(self, query, limit):
    """Recent articles with publication dates, for news-shaped questions."""
    client_class = _ddgs_class()
    if client_class is None or not hasattr(client_class, "news"):
      return []
    try:
      with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with client_class() as ddgs:
          rows = list(ddgs.news(query, max_results=limit))
    except Exception as exc:
      log.info("DuckDuckGo news failed (%s); using text search", exc)
      return []
    results = []
    for row in rows:
      title = " ".join(str(row.get("title") or "").split())
      url = str(row.get("url") or row.get("href") or "").strip()
      snippet = " ".join(str(row.get("body") or "").split())
      if title and url:
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=snippet,
                published=normalize_date(row.get("date")),
                provider=self.name,
            )
        )
      if len(results) >= limit:
        break
    return results

  def _via_html(self, query, limit):
    try:
      response = self._client.get(
          SEARCH_URL,
          params={"q": query},
          headers={"User-Agent": USER_AGENT},
          timeout=self._timeout,
          follow_redirects=True,
      )
      response.raise_for_status()
    except Exception as exc:
      raise SearchError(f"DuckDuckGo request failed: {exc}") from exc

    try:
      tree = lxml_html.fromstring(response.text)
    except (ValueError, lxml_html.etree.ParserError) as exc:
      raise SearchError(f"DuckDuckGo returned an unreadable page: {exc}") from exc

    results = []
    for node in tree.xpath(
        '//div[contains(@class, "result")'
        ' and not(contains(@class, "result--ad"))]'
    ):
      links = node.xpath('.//a[contains(@class, "result__a")]')
      if not links:
        continue
      url = _target_url(links[0].get("href") or "")
      title = " ".join(links[0].text_content().split())
      snippets = node.xpath('.//*[contains(@class, "result__snippet")]')
      snippet = " ".join(snippets[0].text_content().split()) if snippets else ""
      if not url or not title:
        continue
      results.append(
          SearchResult(
              title=title,
              url=url,
              snippet=snippet,
              provider=self.name,
          )
      )
      if len(results) >= limit:
        break
    return results


def _target_url(href):
  """Unwrap DuckDuckGo's redirect links into the destination URL."""
  href = (href or "").strip()
  if not href:
    return ""
  if href.startswith("//"):
    href = f"https:{href}"
  parts = urlsplit(href)
  if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
    target = parse_qs(parts.query).get("uddg", [""])[0]
    return unquote(target).strip()
  if href.startswith("http://") or href.startswith("https://"):
    return href
  return ""
