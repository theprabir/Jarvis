"""Advanced web research: multi-query evidence, comparison and saved reports.

The assistant already had grounded single-question search. This module adds a
research workflow on top of the same providers: several query variants are
merged, articles are preserved, facts and conflicting figures are extracted
deterministically, and every report is stored with its source list so it can
be reopened later. No new resident model or dependency is used.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from web.base import (
    STOPWORDS,
    SearchError,
    SearchResult,
    normalize_date,
    normalize_url,
    words,
)

SENTENCE = re.compile(r"(?<=[.!?])\s+")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
DOC_HOSTS = (
    "docs.python.org",
    "developer.mozilla.org",
    "learn.microsoft.com",
    "docs.microsoft.com",
    "developer.apple.com",
    "docs.oracle.com",
    "nodejs.org",
    "react.dev",
    "rust-lang.org",
    "go.dev",
    "kubernetes.io",
    "readthedocs.io",
    "wikipedia.org",
)
FRESH_DAYS = 30
RECENT_DAYS = 365


class ResearchError(Exception):
  """A research request could not be completed."""


class ResearchStore:
  """Bounded SQLite history for completed research reports."""

  def __init__(self, path, max_entries=200, retention_days=180):
    self.max_entries = max(1, int(max_entries))
    self.retention_days = max(1, int(retention_days))
    self._db = None
    try:
      target = Path(path)
      target.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(str(target), timeout=5.0)
      self._db.execute(
          "CREATE TABLE IF NOT EXISTS research ("
          "id INTEGER PRIMARY KEY AUTOINCREMENT,"
          "created REAL NOT NULL,"
          "query TEXT NOT NULL,"
          "mode TEXT NOT NULL,"
          "domain TEXT NOT NULL,"
          "queries TEXT NOT NULL,"
          "report TEXT NOT NULL,"
          "sources TEXT NOT NULL,"
          "facts TEXT NOT NULL,"
          "contradictions TEXT NOT NULL,"
          "freshness TEXT NOT NULL,"
          "saved INTEGER NOT NULL DEFAULT 0,"
          "title TEXT NOT NULL DEFAULT '')"
      )
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise ResearchError(f"research history is unavailable: {exc}") from exc

  def close(self):
    if self._db is not None:
      try:
        self._db.close()
      finally:
        self._db = None

  def add(
      self,
      query,
      mode,
      domain,
      queries,
      report,
      sources,
      facts,
      contradictions,
      freshness,
  ):
    if self._db is None:
      raise ResearchError("research history is unavailable")
    try:
      cursor = self._db.execute(
          "INSERT INTO research (created, query, mode, domain, queries, report,"
          " sources, facts, contradictions, freshness)"
          " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          (
              time.time(),
              query,
              mode,
              domain,
              json.dumps(list(queries)),
              report,
              json.dumps([source.to_dict() for source in sources]),
              json.dumps(list(facts)),
              json.dumps(list(contradictions)),
              json.dumps(freshness),
          ),
      )
      self._db.commit()
      self._prune()
      return int(cursor.lastrowid)
    except sqlite3.Error as exc:
      raise ResearchError(f"could not save research: {exc}") from exc

  def get(self, identifier):
    row = self._row(identifier)
    if row is None:
      raise ResearchError(f"research #{identifier} was not found")
    return row

  def save(self, identifier, title=""):
    row = self.get(identifier)
    title = " ".join(str(title or "").split())[:160]
    try:
      self._db.execute(
          "UPDATE research SET saved = 1, title = ? WHERE id = ?",
          (title or row["query"][:160], identifier),
      )
      self._db.commit()
    except sqlite3.Error as exc:
      raise ResearchError(f"could not save research: {exc}") from exc
    return self.get(identifier)

  def delete(self, identifier):
    self.get(identifier)
    try:
      self._db.execute("DELETE FROM research WHERE id = ?", (identifier,))
      self._db.commit()
    except sqlite3.Error as exc:
      raise ResearchError(f"could not delete research: {exc}") from exc

  def list(self, limit=20, saved=False, query=""):
    if self._db is None:
      return []
    clauses = ["saved = 1"] if saved else []
    values = []
    needle = str(query or "").strip()
    if needle:
      clauses.append("(query LIKE ? OR title LIKE ? OR report LIKE ?)")
      pattern = f"%{needle}%"
      values.extend([pattern, pattern, pattern])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
      rows = self._db.execute(
          "SELECT id, created, query, mode, domain, queries, report, sources,"
          " facts, contradictions, freshness, saved, title FROM research"
          f"{where} ORDER BY created DESC LIMIT ?",
          (*values, max(1, min(100, int(limit)))),
      ).fetchall()
    except sqlite3.Error as exc:
      raise ResearchError(f"could not read research history: {exc}") from exc
    return [self._decode(row) for row in rows]

  def _row(self, identifier):
    if self._db is None:
      return None
    try:
      row = self._db.execute(
          "SELECT id, created, query, mode, domain, queries, report, sources,"
          " facts, contradictions, freshness, saved, title FROM research"
          " WHERE id = ?",
          (int(identifier),),
      ).fetchone()
    except (ValueError, sqlite3.Error) as exc:
      raise ResearchError(f"could not read research: {exc}") from exc
    return self._decode(row) if row else None

  @staticmethod
  def _decode(row):
    created = float(row[1])
    try:
      sources = [
          SearchResult.from_dict(item) for item in json.loads(row[7] or "[]")
      ]
    except (ValueError, TypeError):
      sources = []
    return {
        "id": int(row[0]),
        "created": datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M"),
        "query": row[2],
        "mode": row[3],
        "domain": row[4],
        "queries": _json(row[5], []),
        "report": row[6],
        "sources": sources,
        "facts": _json(row[8], []),
        "contradictions": _json(row[9], []),
        "freshness": _json(row[10], {}),
        "saved": bool(row[11]),
        "title": row[12] or row[2],
    }

  def _prune(self):
    cutoff = time.time() - self.retention_days * 86400
    self._db.execute("DELETE FROM research WHERE saved = 0 AND created < ?", (cutoff,))
    self._db.execute(
        "DELETE FROM research WHERE saved = 0 AND id NOT IN ("
        " SELECT id FROM research WHERE saved = 0"
        " ORDER BY created DESC LIMIT ?)",
        (self.max_entries,),
    )
    self._db.commit()


class ResearchService:
  """Run multi-query research and persist the resulting report."""

  def __init__(self, searcher, config, store=None):
    self._searcher = searcher
    self._config = config
    self._store = store or ResearchStore(
        config.resolved_research_db,
        config.research_entries,
        config.research_retention_days,
    )

  @property
  def providers(self):
    names = getattr(self._searcher, "provider_names", None)
    if callable(names):
      try:
        return tuple(names())
      except Exception:
        pass
    return ()

  def close(self):
    """Close research storage; the searcher is owned by the caller."""
    self._store.close()

  def run(self, query, mode="general", domain="", save=True):
    query = " ".join(str(query or "").split())
    if not query:
      raise ResearchError("a research question is required")
    domain = _domain(domain)
    mode = mode if mode in {"general", "compare", "docs"} else "general"
    queries = research_queries(
        query, mode, getattr(self._config, "research_max_queries", 3)
    )
    results = []
    errors = []
    for current in queries:
      try:
        bundle = self._search(current, domain)
      except SearchError as exc:
        errors.append(str(exc))
        continue
      results.extend(list(getattr(bundle, "results", ()) or ()))
      if len(results) >= self._config.research_max_sources * 2:
        break
    merged = _merge(results)
    if not merged:
      raise ResearchError(
          errors[0] if errors else "no relevant sources were found"
      )
    if mode == "docs":
      merged = sorted(
          merged,
          key=lambda item: (_docs_score(item), item.score),
          reverse=True,
      )
    sources = merged[: self._config.research_max_sources]
    facts = extract_facts(sources, query)
    contradictions = find_contradictions(facts)
    freshness = assess_freshness(sources)
    comparison = compare_sources(sources, facts)
    citations = citation_lines(sources)
    report = build_report(
        query,
        mode,
        domain,
        queries,
        sources,
        facts,
        contradictions,
        freshness,
        comparison,
        citations,
    )
    identifier = None
    if save:
      identifier = self._store.add(
          query,
          mode,
          domain,
          queries,
          report,
          sources,
          facts,
          contradictions,
          freshness,
      )
    return {
        "id": identifier,
        "query": query,
        "mode": mode,
        "domain": domain,
        "queries": queries,
        "report": report,
        "sources": sources,
        "facts": facts,
        "contradictions": contradictions,
        "freshness": freshness,
        "comparison": comparison,
        "citations": citations,
    }

  def history(self, limit=20, saved=False, query=""):
    return self._store.list(limit, saved=saved, query=query)

  def get(self, identifier):
    return self._store.get(identifier)

  def save(self, identifier, title=""):
    return self._store.save(identifier, title)

  def delete(self, identifier):
    self._store.delete(identifier)

  def _search(self, query, domain):
    text = query if not domain or "site:" in query else f"{query} site:{domain}"
    try:
      return self._searcher.search(text)
    except TypeError:
      # A small injected fake may implement only the original signature.
      return self._searcher.search(query)


def research_queries(query, mode, limit=3):
  """Return the primary question plus bounded research variants."""
  query = " ".join(query.split())
  variants = [query]
  terms = [word for word in words(query) if len(word) > 2 and word not in STOPWORDS]
  if mode == "docs":
    variants.append(f"{query} official documentation")
    variants.append(f"{query} API reference")
  elif mode == "compare":
    variants.append(f"{query} comparison")
  elif terms:
    variants.append(" ".join(dict.fromkeys(terms))[:120])
  return list(dict.fromkeys(variants))[: max(1, int(limit))]


def extract_facts(sources, query=""):
  """Return bounded, source-attributed factual sentences."""
  query_terms = {word for word in words(query) if word not in STOPWORDS}
  facts = []
  seen = set()
  for index, source in enumerate(sources, 1):
    body = " ".join(f"{source.snippet} {source.text}".split())
    for sentence in SENTENCE.split(body):
      sentence = " ".join(sentence.split()).strip(" -")
      if len(sentence) < 30 or len(sentence) > 320:
        continue
      lower = sentence.lower()
      if any(bad in lower for bad in ("cookie", "subscribe", "sign in", "advertisement")):
        continue
      tokens = set(words(sentence))
      if not (NUMBER.search(sentence) or query_terms & tokens):
        continue
      signature = " ".join(sorted(tokens))
      if signature in seen:
        continue
      seen.add(signature)
      facts.append({"source": index, "text": sentence})
      if len(facts) >= 12:
        return facts
  return facts


def find_contradictions(facts):
  """Find different figures attached to otherwise similar claims."""
  groups = {}
  for fact in facts:
    numbers = tuple(
        number.replace(",", "").rstrip(".")
        for number in NUMBER.findall(fact["text"])
    )
    if not numbers:
      continue
    key = _claim_key(fact["text"])
    if not key:
      continue
    groups.setdefault(key, []).append((fact["source"], numbers, fact["text"]))
  contradictions = []
  for key, rows in groups.items():
    values = {numbers for _source, numbers, _text in rows}
    sources = sorted({source for source, _numbers, _text in rows})
    if len(sources) >= 2 and len(values) >= 2:
      contradictions.append(
          {
              "claim": key,
              "values": sorted(values),
              "sources": sources,
              "details": [text for _source, _numbers, text in rows[:4]],
          }
      )
    if len(contradictions) >= 5:
      break
  return contradictions


def assess_freshness(sources):
  """Summarize the publication dates attached to the selected sources."""
  statuses = []
  dates = []
  today = date.today()
  for source in sources:
    published = normalize_date(source.published)
    if not published:
      statuses.append("unknown")
      continue
    try:
      when = datetime.strptime(published, "%Y-%m-%d").date()
    except ValueError:
      statuses.append("unknown")
      continue
    dates.append(when)
    days = (today - when).days
    if days <= FRESH_DAYS:
      statuses.append("fresh")
    elif days <= RECENT_DAYS:
      statuses.append("recent")
    else:
      statuses.append("stale")
  if not dates:
    level = "unknown"
  elif all(status == "fresh" for status in statuses if status != "unknown"):
    level = "fresh"
  elif all(status == "stale" for status in statuses if status != "unknown"):
    level = "stale"
  elif any(status == "stale" for status in statuses):
    level = "mixed"
  elif dates:
    level = "recent"
  else:
    level = "unknown"
  return {
      "level": level,
      "newest": max(dates).isoformat() if dates else "",
      "oldest": min(dates).isoformat() if dates else "",
      "counts": {name: statuses.count(name) for name in set(statuses)},
  }


def compare_sources(sources, facts):
  """Build a compact per-source comparison table."""
  counts = {}
  for fact in facts:
    counts[fact["source"]] = counts.get(fact["source"], 0) + 1
  rows = []
  for index, source in enumerate(sources, 1):
    host = urlsplit(source.url).netloc.lower().removeprefix("www.")
    freshness = "unknown"
    published = normalize_date(source.published)
    if published:
      try:
        days = (date.today() - datetime.strptime(published, "%Y-%m-%d").date()).days
        freshness = (
            "fresh" if days <= FRESH_DAYS
            else "recent" if days <= RECENT_DAYS
            else "stale"
        )
      except ValueError:
        freshness = "unknown"
    rows.append(
        {
            "index": index,
            "title": source.title,
            "domain": host,
            "published": published,
            "freshness": freshness,
            "facts": counts.get(index, 0),
            "has_article": bool(source.text),
        }
    )
  return rows


def citation_lines(sources):
  lines = []
  for index, source in enumerate(sources, 1):
    suffix = f" ({source.published})" if source.published else ""
    lines.append(f"[{index}] {source.title} - {source.url}{suffix}")
  return lines


def build_report(
    query,
    mode,
    domain,
    queries,
    sources,
    facts,
    contradictions,
    freshness,
    comparison,
    citations,
):
  lines = [f"# Research report: {query}", ""]
  meta = [f"Mode: {mode}", f"Sources: {len(sources)}"]
  if domain:
    meta.append(f"Domain: {domain}")
  lines.append(" · ".join(meta))
  lines.extend(["", f"Queries: {' | '.join(queries)}"])
  lines.extend(["", "## Key facts", ""])
  if facts:
    lines.extend(
        f"- {fact['text']} [{fact['source']}]" for fact in facts[:10]
    )
  else:
    lines.append("- No numeric or question-specific facts were extracted.")
  lines.extend(["", "## Source comparison", ""])
  lines.append("| # | Source | Domain | Published | Freshness | Facts |")
  lines.append("| --- | --- | --- | --- | --- | --- |")
  for row in comparison:
    lines.append(
        f"| {row['index']} | {_cell(row['title'])} | {_cell(row['domain'])}"
        f" | {row['published'] or 'unknown'} | {row['freshness']}"
        f" | {row['facts']} |"
    )
  lines.extend(["", "## Contradictions", ""])
  if contradictions:
    for item in contradictions:
      values = ", ".join(" / ".join(value) for value in item["values"])
      lines.append(
          f"- Possible conflict in **{item['claim']}**: {values}"
          f" (sources {', '.join(f'[{n}]' for n in item['sources'])})."
      )
  else:
    lines.append("- No conflicting figures were found in the extracted facts.")
  lines.extend(["", "## Freshness", ""])
  newest = freshness.get("newest") or "unknown"
  oldest = freshness.get("oldest") or "unknown"
  lines.append(
      f"- Overall: **{freshness.get('level', 'unknown')}**;"
      f" newest {newest}, oldest {oldest}."
  )
  counts = freshness.get("counts", {})
  if counts:
    lines.append(
        "- Dates: " + ", ".join(f"{key} {value}" for key, value in sorted(counts.items()))
    )
  lines.extend(["", "## Citations", ""])
  lines.extend(citations or ["- No sources."])
  lines.extend(["", "## Source preservation", ""])
  lines.append(
      f"{len(sources)} full source records were stored with this report"
      " so it can be reopened without another search."
  )
  return "\n".join(lines).rstrip() + "\n"


def _merge(results):
  unique = {}
  for result in results:
    key = normalize_url(result.url)
    if not key:
      continue
    current = unique.get(key)
    if current is None:
      unique[key] = result
      continue
    if result.score > current.score:
      result.snippet = result.snippet or current.snippet
      result.text = result.text or current.text
      result.published = result.published or current.published
      unique[key] = result
    else:
      current.snippet = current.snippet or result.snippet
      current.text = current.text or result.text
      current.published = current.published or result.published
  return sorted(unique.values(), key=lambda item: item.score, reverse=True)


def _docs_score(result):
  host = urlsplit(result.url).netloc.lower().removeprefix("www.")
  return 2.0 if any(
      host == domain or host.endswith("." + domain) for domain in DOC_HOSTS
  ) else 0.0


def _claim_key(text):
  stripped = NUMBER.sub(" # ", text.lower())
  tokens = [
      word for word in words(stripped)
      if word not in STOPWORDS and word not in {"is", "was", "has", "had"}
  ]
  return " ".join(tokens[:8])


def _domain(value):
  text = str(value or "").strip().lower()
  if not text:
    return ""
  text = re.sub(r"^https?://", "", text).split("/")[0]
  if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", text):
    raise ResearchError("a domain must look like example.com")
  return text


def _json(value, default):
  try:
    return json.loads(value or "")
  except (TypeError, ValueError):
    return default


def _cell(value):
  return str(value or "").replace("|", "/").replace("\n", " ")[:80]
