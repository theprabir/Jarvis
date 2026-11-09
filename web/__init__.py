"""Web search, extraction and grounded answer support."""

from __future__ import annotations

from web.base import SearchError, SearchResult, normalize_url
from web.search import SearchBundle, Searcher

__all__ = [
  "SearchBundle",
  "SearchError",
  "SearchResult",
  "Searcher",
  "normalize_url",
]
