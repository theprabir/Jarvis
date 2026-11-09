"""Bounded, time-limited search result cache in SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from web.base import SearchResult

log = logging.getLogger(__name__)

# Expired entries are kept this long so an offline session can still answer
# from a recent search instead of failing completely.
MAX_STALE_S = 7 * 24 * 3600


class SearchCache:
  """Store recent search results by query, pruned by age and entry count."""

  def __init__(self, path, ttl_s=3600, max_entries=128):
    self.ttl_s = float(ttl_s)
    self.max_entries = max(1, int(max_entries))
    self._db = None
    try:
      target = Path(path)
      target.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(str(target), timeout=5.0)
      self._db.execute(
          "CREATE TABLE IF NOT EXISTS search_cache ("
          "key TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL)"
      )
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      log.warning("Search cache disabled: %s", exc)
      self._db = None

  def get(self, key, allow_stale=False):
    """Return cached results for ``key``, or ``None`` when absent or too old."""
    if self._db is None:
      return None
    try:
      row = self._db.execute(
          "SELECT payload, created FROM search_cache WHERE key = ?", (key,)
      ).fetchone()
    except sqlite3.Error as exc:
      log.debug("Search cache read failed: %s", exc)
      return None
    if row is None:
      return None
    age = time.time() - float(row[1])
    if age > (MAX_STALE_S if allow_stale else self.ttl_s):
      return None
    try:
      payload = json.loads(row[0])
      return [SearchResult.from_dict(item) for item in payload]
    except (ValueError, TypeError) as exc:
      log.debug("Ignoring corrupt cache entry for %r: %s", key, exc)
      return None

  def put(self, key, results):
    """Save ``results`` for ``key`` and keep the cache bounded."""
    if self._db is None or not results:
      return
    payload = json.dumps([result.to_dict() for result in results])
    try:
      self._db.execute(
          "INSERT OR REPLACE INTO search_cache (key, payload, created)"
          " VALUES (?, ?, ?)",
          (key, payload, time.time()),
      )
      self._db.execute(
          "DELETE FROM search_cache WHERE created < ?",
          (time.time() - MAX_STALE_S,),
      )
      self._db.execute(
          "DELETE FROM search_cache WHERE key NOT IN ("
          " SELECT key FROM search_cache ORDER BY created DESC LIMIT ?)",
          (self.max_entries,),
      )
      self._db.commit()
    except sqlite3.Error as exc:
      log.debug("Search cache write failed: %s", exc)

  def clear(self):
    if self._db is None:
      return
    try:
      self._db.execute("DELETE FROM search_cache")
      self._db.commit()
    except sqlite3.Error as exc:
      log.debug("Search cache clear failed: %s", exc)

  def close(self):
    if self._db is not None:
      try:
        self._db.close()
      finally:
        self._db = None
