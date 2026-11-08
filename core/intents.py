"""Deterministic intent recognition, entity extraction and JSON validation.

Requests that Python can answer (arithmetic, current time and date, built-in
commands) are recognized here and answered without touching the model. The
router returns ``None`` for everything else, so ambiguous or open-ended
requests fall back to the LLM. ``Intent`` doubles as the validated structured
output format used by later tool calling.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from core.calculator import CalculationError, evaluate
from core.dates import find_date_reference

CONFIDENCE_THRESHOLD = 0.7

INTENT_NAMES = frozenset(
    {
        "time",
        "date",
        "math",
        "conversation",
        "help",
        "new",
        "mode",
        "history",
        "forget",
        "intent",
    }
)
COMMANDS = frozenset({"help", "new", "mode", "history", "forget", "intent"})

_TIME_PATTERNS = (
  r"\bwhat(?:'s| is)? the time\b",
  r"\bwhat time is it\b",
  r"\bcurrent time\b",
  r"\btime (?:now|please)\b",
  r"\bdo you know the time\b",
)

_DATE_PATTERNS = (
  r"\bwhat(?:'s| is)? the date\b",
  r"\bwhat day is it\b",
  r"\bwhat day is today\b",
  r"\btoday'?s date\b",
)

_MATH_WORDS = (
    ("multiplied by", "*"),
    ("divided by", "/"),
    ("to the power of", "**"),
    ("times", "*"),
    ("plus", "+"),
    ("minus", "-"),
    ("squared", "**2"),
)

_MATH_PREFIX = re.compile(
    r"^\s*(?:what's|whats|what is|how much is|calculate|compute|evaluate"
    r"|solve|tell me)\s+"
)
_MATH_FILLER = re.compile(r"\b(?:please|sir|for me|the result of)\b")


class IntentError(ValueError):
  """Raised when a structured intent payload fails validation."""


@dataclass(frozen=True)
class Intent:
  """A recognized intent with extracted entities and a confidence score."""

  name: str
  confidence: float
  entities: dict = field(default_factory=dict)
  source: str = "deterministic"

  def to_dict(self):
    return {
        "intent": self.name,
        "confidence": round(float(self.confidence), 2),
        "entities": dict(self.entities),
        "source": self.source,
    }

  def to_json(self):
    return json.dumps(self.to_dict(), sort_keys=True)

  @classmethod
  def from_dict(cls, data):
    """Validate an untrusted intent payload and build an Intent."""
    if not isinstance(data, dict):
      raise IntentError("intent payload must be an object")
    name = data.get("intent")
    if name not in INTENT_NAMES:
      raise IntentError(f"unknown intent: {name!r}")
    confidence = data.get("confidence", 0.0)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
      raise IntentError("confidence must be a number between 0 and 1")
    entities = data.get("entities", {})
    if not isinstance(entities, dict):
      raise IntentError("entities must be an object")
    for key, value in entities.items():
      if not isinstance(key, str):
        raise IntentError("entity names must be strings")
      if isinstance(value, bool):
        continue
      if not isinstance(value, (str, int, float)):
        raise IntentError(f"entity {key!r} has an unsupported value type")
    source = data.get("source", "deterministic")
    if source not in {"deterministic", "llm"}:
      raise IntentError(f"unknown source: {source!r}")
    return cls(
        name=name,
        confidence=float(confidence),
        entities=dict(entities),
        source=source,
    )


def recognize_time(text):
  lowered = text.lower()
  if any(re.search(pattern, lowered) for pattern in _TIME_PATTERNS):
    return Intent(name="time", confidence=0.92)
  return None


def recognize_date(text):
  lowered = text.lower()
  if not any(re.search(pattern, lowered) for pattern in _DATE_PATTERNS):
    return None
  reference = find_date_reference(lowered)
  entities = reference or {}
  return Intent(
      name="date",
      confidence=0.92 if reference else 0.85,
      entities=entities,
  )


def recognize_math(text):
  """Recognize a pure arithmetic question and evaluate it locally."""
  candidate = _math_expression(text)
  if candidate is None:
    return None
  entities = {"expression": candidate}
  try:
    entities["result"] = evaluate(candidate)
  except CalculationError as exc:
    entities["error"] = str(exc)
  return Intent(name="math", confidence=0.95, entities=entities)


def _math_expression(text):
  lowered = text.lower().strip().rstrip("?.!")
  for word, symbol in _MATH_WORDS:
    lowered = re.sub(rf"\b{re.escape(word)}\b", symbol, lowered)
  lowered = _MATH_PREFIX.sub("", lowered)
  lowered = _MATH_FILLER.sub(" ", lowered)
  lowered = lowered.replace("^", "**").strip()
  lowered = lowered.strip(" =")
  if not lowered or not re.search(r"\d", lowered):
    return None
  if not re.search(r"[+\-*/%]", lowered):
    return None
  # Leftover letters mean this is a word problem, not a calculation.
  if re.search(r"[a-z]", lowered):
    return None
  return lowered


class IntentRouter:
  """Choose a deterministic intent for a request, or None to use the LLM."""

  def __init__(self, confidence_threshold=CONFIDENCE_THRESHOLD):
    self.threshold = confidence_threshold

  def route(self, text):
    text = (text or "").strip()
    if not text:
      return None
    if text.startswith("/"):
      name = text.split()[0].lstrip("/").lower()
      if name in COMMANDS:
        return Intent(name=name, confidence=1.0)
      return None
    for recognizer in (recognize_time, recognize_date, recognize_math):
      intent = recognizer(text)
      if intent is not None:
        return intent
    return None

  def is_confident(self, intent):
    return intent is not None and intent.confidence >= self.threshold
