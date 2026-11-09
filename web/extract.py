"""Page fetching and main-content extraction.

Many sites reject a plain HTTP client with a 403 because of its TLS
fingerprint, so fetching tries ``primp`` (browser impersonation, shipped with
``ddgs``) first and falls back to ``httpx``. ``trafilatura`` is used for
extraction when installed; otherwise a small ``lxml`` extractor is used.
"""

from __future__ import annotations

import logging
import re

import httpx
from lxml import html as lxml_html

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SKIP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "svg",
    "iframe",
    "button",
    "select",
    "template",
)
BOILERPLATE = re.compile(
    r"\b(cookies?|subscribe|newsletter|sign in|log in|advertisement"
    r"|all rights reserved|privacy policy)\b",
    re.I,
)
MIN_PARAGRAPH = 40
MAX_TEXT_CHARS = 6000

_primp_client = None
_primp_timeout = 0.0


class PageError(Exception):
  """A page could not be fetched or was not text content."""


def download(client, url, timeout_s=10.0, max_bytes=400_000):
  """Fetch a page as text, preferring a browser-like client."""
  text = _primp_fetch(url, timeout_s, max_bytes)
  if text:
    return text
  return fetch_page(client, url, timeout_s=timeout_s, max_bytes=max_bytes)


def _primp_fetch(url, timeout_s, max_bytes):
  """Fetch through primp, returning ``""`` when it is unavailable or blocked."""
  global _primp_client, _primp_timeout
  try:
    import primp
  except ImportError:
    return ""
  try:
    if _primp_client is None or timeout_s > _primp_timeout:
      _primp_client = primp.Client(impersonate="chrome", timeout=timeout_s)
      _primp_timeout = timeout_s
    response = _primp_client.get(url)
  except Exception as exc:
    log.info("primp could not fetch %s: %s", url, exc)
    return ""

  status = getattr(response, "status_code", 0)
  if status >= 400:
    log.info("primp got HTTP %s from %s", status, url)
    return ""
  headers = getattr(response, "headers", {}) or {}
  content_type = str(headers.get("content-type") or "").lower()
  if content_type and not any(
      kind in content_type for kind in ("html", "text/plain")
  ):
    return ""
  body = getattr(response, "content", b"") or b""
  if not body:
    return ""
  encoding = getattr(response, "encoding", None) or "utf-8"
  try:
    return body[:max_bytes].decode(encoding, errors="replace")
  except LookupError:
    return body[:max_bytes].decode("utf-8", errors="replace")


def fetch_page(client, url, timeout_s=10.0, max_bytes=400_000):
  """Download a page with httpx, stopping after ``max_bytes``."""
  try:
    with client.stream(
        "GET",
        url,
        timeout=timeout_s,
        follow_redirects=True,
        headers=HEADERS,
    ) as response:
      if response.status_code >= 400:
        raise PageError(f"HTTP {response.status_code}")
      content_type = (response.headers.get("content-type") or "").lower()
      if content_type and not any(
          kind in content_type for kind in ("html", "text/plain")
      ):
        raise PageError(f"not text content ({content_type.split(';')[0]})")
      chunks = []
      size = 0
      for chunk in response.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes:
          break
      raw = b"".join(chunks)
      encoding = response.encoding or "utf-8"
  except httpx.HTTPError as exc:
    raise PageError(str(exc)) from exc
  return raw.decode(encoding, errors="replace")


def extract_text(markup, url=""):
  """Return the readable body text of a page, or ``""`` when unavailable."""
  text = _trafilatura(markup, url)
  if text:
    return text
  return _lxml_text(markup)


def _trafilatura(markup, url):
  try:
    import trafilatura
  except ImportError:
    return ""
  try:
    extracted = trafilatura.extract(
        markup,
        url=url or None,
        include_comments=False,
        include_tables=False,
        favor_recall=True,
    )
  except Exception:  # extraction is best effort
    log.debug("trafilatura failed for %s", url, exc_info=True)
    return ""
  return _trim(extracted or "")


def _lxml_text(markup):
  try:
    tree = lxml_html.fromstring(markup)
  except (ValueError, lxml_html.etree.ParserError):
    return ""
  for tag in SKIP_TAGS:
    for node in tree.xpath(f"//{tag}"):
      try:
        node.drop_tree()
      except AttributeError:
        node.getparent().remove(node)
  chunks = []
  total = 0
  for node in tree.xpath("//p|//li|//h1|//h2|//h3|//blockquote"):
    piece = _trim(node.text_content())
    if len(piece) < MIN_PARAGRAPH or BOILERPLATE.search(piece):
      continue
    if chunks and chunks[-1] == piece:
      continue
    chunks.append(piece)
    total += len(piece)
    if total >= MAX_TEXT_CHARS:
      break
  if not chunks:
    return _trim(tree.text_content())[:MAX_TEXT_CHARS]
  return " ".join(chunks)[:MAX_TEXT_CHARS]


def _trim(text):
  return " ".join((text or "").split())
