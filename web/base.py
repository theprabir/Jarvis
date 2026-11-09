"""Search primitives shared by providers and the searcher."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

WORD = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
    }
)


class SearchError(Exception):
  """A search could not be completed (network, provider or parsing failure)."""


def words(text):
  """Split text into lowercase word tokens."""
  return WORD.findall((text or "").lower())


def normalize_url(url):
  """Return a comparison key for a URL, or ``""`` when it is unusable."""
  if not url:
    return ""
  parts = urlsplit(str(url).strip())
  if not parts.netloc:
    return ""
  host = parts.netloc.lower()
  if host.startswith("www."):
    host = host[4:]
  path = parts.path.rstrip("/") or "/"
  return urlunsplit((parts.scheme or "https", host, path, parts.query, ""))


@dataclass
class SearchResult:
  """One search hit; ``text`` holds extracted page content when fetched."""

  title: str
  url: str
  snippet: str = ""
  text: str = ""
  score: float = 0.0
  provider: str = ""

  def to_dict(self):
    return {
        "title": self.title,
        "url": self.url,
        "snippet": self.snippet,
        "text": self.text,
        "score": round(float(self.score), 4),
        "provider": self.provider,
    }

  @classmethod
  def from_dict(cls, data):
    """Rebuild a result from cached JSON, ignoring malformed entries."""
    if not isinstance(data, dict):
      raise ValueError("search result must be an object")
    title = str(data.get("title") or "").strip()
    url = str(data.get("url") or "").strip()
    if not title or not url:
      raise ValueError("search result needs a title and a url")
    return cls(
        title=title,
        url=url,
        snippet=str(data.get("snippet") or ""),
        text=str(data.get("text") or ""),
        score=float(data.get("score") or 0.0),
        provider=str(data.get("provider") or ""),
    )

  def content(self, limit):
    """Return the best available body text, trimmed to ``limit`` characters."""
    body = " ".join((self.text or self.snippet or "").split())
    if len(body) <= limit:
      return body
    return body[: limit - 3].rstrip(" ,;.") + "..."
