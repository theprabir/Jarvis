"""Automatic memory candidates, importance scoring and episodic summaries.

Feature set 4.8 learns only from a small set of high-confidence first-person
statements. Everything else is either ignored or kept as an expiring episode.
Nothing is promoted to permanent memory just because it was said: learned
memories carry a TTL, a confidence score and their source, and they can be
inspected or deleted with the normal memory controls.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from core.memory import MAX_CONTENT_CHARS, classify

WORD = re.compile(r"[a-z0-9']+")
QUESTION_START = re.compile(
    r"^(?:what|who|where|when|why|how|which|whose|do|does|did|can|could|"
    r"would|should|is|are|was|were|will|have|has)\b",
    re.I,
)
IMPORTANT_CUES = (
    "important",
    "always",
    "never",
    "remember",
    "critical",
    "essential",
    "must",
    "favorite",
    "favourite",
)
GREETINGS = {
    "hi",
    "hello",
    "hey",
    "thanks",
    "thank you",
    "ok",
    "okay",
    "yes",
    "no",
    "good morning",
    "good afternoon",
    "good evening",
    "good night",
}
SUBJECT_NOUNS = {
    "office",
    "company",
    "school",
    "college",
    "team",
    "manager",
    "phone",
    "email",
    "address",
    "car",
    "project",
}
BASE_IMPORTANCE = {
    "preference": 4,
    "path": 3,
    "folder": 3,
    "website": 3,
    "task": 3,
    "fact": 2,
    "general": 2,
}
TOPIC_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "because",
    "before",
    "being",
    "could",
    "does",
    "doing",
    "from",
    "have",
    "just",
    "like",
    "make",
    "many",
    "more",
    "much",
    "need",
    "please",
    "really",
    "should",
    "some",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "thing",
    "this",
    "those",
    "very",
    "want",
    "what",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "would",
    "your",
}


@dataclass(frozen=True)
class Candidate:
  """One semantic memory worth considering for automatic storage."""

  category: str
  content: str
  importance: int
  confidence: float
  subject: str | None = None


def _clean(sentence):
  text = " ".join(str(sentence or "").split())
  return text.strip(" ,;:-").rstrip(".").strip()


def _words(text):
  return WORD.findall(str(text or "").lower())


def importance_for(category, content):
  """Score how useful a memory is likely to be, from 1 (low) to 5 (high)."""
  text = str(content or "").lower()
  score = BASE_IMPORTANCE.get(str(category or ""), 2)
  if any(cue in text for cue in IMPORTANT_CUES):
    score += 1
  if len(_words(text)) >= 9:
    score += 1
  return max(1, min(5, score))


def subject_for(content):
  """Return the attribute a statement describes, for conflict detection.

  Only stable user attributes get a subject. Generic preferences such as
  "I like tea" and "I like coffee" can coexist, so they are not conflicts.
  """
  lowered = " ".join(str(content or "").lower().split())
  if re.search(r"\bmy name(?:'s| is)\b|\bcall me\b", lowered):
    return "name"
  match = re.search(r"\bmy favou?rite ([a-z ]{2,24}?) (?:is|are)\b", lowered)
  if match:
    return f"favorite:{' '.join(match.group(1).split())}"
  if re.search(
      r"\bi (?:live|am living) in\b|\bi'?m from\b|\bmy home is\b"
      r"|\bmy (?:house|city) is\b",
      lowered,
  ):
    return "residence"
  if re.search(
      r"\bi work (?:at|for)\b|\bmy (?:employer|company) is\b", lowered
  ):
    return "employer"
  if re.search(
      r"\bi work as\b|\bmy job is\b|\bmy (?:profession|role) is\b", lowered
  ):
    return "job"
  if re.search(r"\bi am \d{1,3} years old\b|\bmy age is\b", lowered):
    return "age"
  if re.search(r"\bmy birthday is\b", lowered):
    return "birthday"
  folder = re.search(r"\bmy (.+?) (?:folder|directory) is\b", lowered)
  if folder:
    return f"my:{' '.join(folder.group(1).split())} folder"
  for noun in SUBJECT_NOUNS:
    if re.search(rf"\bmy {noun} is\b", lowered):
      return f"my:{noun}"
  return None


def _candidate(content, category, confidence, subject=None):
  text = str(content or "")[:MAX_CONTENT_CHARS]
  return Candidate(
      category=category,
      content=text,
      importance=importance_for(category, text),
      confidence=confidence,
      subject=subject or subject_for(text),
  )


def _extract_sentence(sentence):
  text = _clean(sentence)
  if not text:
    return None
  lowered = text.lower()
  if lowered in GREETINGS:
    return None
  # Questions and requests addressed to the assistant are not facts about
  # the user, even when they contain "I" or "my".
  if (
      text.endswith("?")
      or QUESTION_START.match(text)
      or re.search(r"\byou\b", lowered)
  ):
    return None
  if re.match(r"^(?:my name(?:'s| is)|call me) .+", lowered):
    return _candidate(text, "preference", 0.95, "name")
  favorite = re.match(
      r"^my favou?rite [a-z ]{2,24} (?:is|are) .+", lowered
  )
  if favorite:
    return _candidate(text, "preference", 0.9)
  if re.match(
      r"^(?:i (?:live|am living) in|i'?m from|my home is|my city is) .+",
      lowered,
  ):
    return _candidate(text, "preference", 0.9)
  if re.match(r"^(?:i work (?:at|for)|my employer is|my company is) .+",
              lowered):
    return _candidate(text, "preference", 0.9)
  if re.match(r"^(?:i work as|my job is|my profession is) .+", lowered):
    return _candidate(text, "preference", 0.9)
  if re.match(r"^i am \d{1,3} years old$", lowered):
    return _candidate(text, "preference", 0.95)
  if re.match(r"^my age is .+", lowered):
    return _candidate(text, "preference", 0.95)
  if re.match(r"^my birthday is .+", lowered):
    return _candidate(text, "preference", 0.9)
  folder = re.match(r"^my .+ (?:folder|directory) is .+", lowered)
  if folder:
    return _candidate(text, "folder", 0.9)
  generic = re.match(r"^my ([a-z ]{2,30}?) is .+", lowered)
  if generic and generic.group(1).split()[0] in SUBJECT_NOUNS:
    return _candidate(text, "fact", 0.85)
  if re.match(r"^i (?:don'?t|do not) (?:like|prefer|use) .+", lowered):
    return _candidate(text, "preference", 0.8)
  if re.match(r"^i (?:really )?(?:prefer|like|love|enjoy|use) .+", lowered):
    return _candidate(text, "preference", 0.85)
  if re.match(r"^i (?:always|usually|never) .+", lowered):
    return _candidate(text, "preference", 0.8)
  return None


def extract_candidates(text):
  """Return semantic memory candidates found in one user message."""
  body = " ".join(str(text or "").split())
  if not body or body.startswith("/"):
    return []
  if body.lower().strip(" .!?") in GREETINGS:
    return []
  candidates = []
  seen = set()
  for sentence in re.split(r"(?<=[.!?])\s+", body):
    candidate = _extract_sentence(sentence)
    if candidate is None:
      continue
    key = candidate.content.lower()
    if key in seen:
      continue
    seen.add(key)
    candidates.append(candidate)
  return candidates


def episode_summary(user_text, answer_text=None):
  """Summarize a substantive exchange for expiring episodic memory."""
  body = " ".join(str(user_text or "").split())
  if not body or body.startswith("/"):
    return None
  if body.lower().strip(" .!?") in GREETINGS:
    return None
  if len(_words(body)) < 6:
    return None
  prefix = "Asked: " if body.endswith("?") else "Discussed: "
  summary = body
  if len(summary) > 180:
    summary = summary[:177].rsplit(" ", 1)[0] + "..."
  return prefix + summary


def episode_topics(user_text, limit=8):
  """Keywords that make an episode easier to retrieve later."""
  seen = []
  for word in _words(user_text):
    if len(word) < 4 or word in TOPIC_STOPWORDS or word in seen:
      continue
    seen.append(word)
    if len(seen) >= limit:
      break
  return " ".join(seen)


class ShortTermMemory:
  """A bounded, session-only buffer of recent exchanges.

  Short-term memory is deliberately not persisted: durable facts belong in
  the memory store, while this buffer only reconstructs the current session.
  """

  def __init__(self, limit=6):
    self._limit = max(2, int(limit))
    self._items = deque(maxlen=self._limit)

  def add(self, user_text, answer):
    text = " ".join(str(user_text or "").split())
    if text:
      self._items.append((text, str(answer or "")))

  def recent(self, limit=None):
    items = list(self._items)
    if limit:
      return items[-max(1, int(limit)):]
    return items

  def summary(self, max_chars=280):
    texts = [text for text, _answer in self._items
             if not text.startswith("/")]
    joined = " | ".join(texts)
    if not joined:
      return ""
    if len(joined) <= max_chars:
      return joined
    return "..." + joined[-max_chars:]

  def clear(self):
    self._items.clear()

  def __len__(self):
    return len(self._items)
