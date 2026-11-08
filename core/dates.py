"""Date phrase interpretation for deterministic date questions.

Handles today/tomorrow/yesterday, relative offsets ("in 3 days", "2 weeks
ago"), weekday references ("next monday") and ISO dates. Recurring reminders
and richer natural-language parsing belong to the scheduler feature set.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_RELATIVE_PHRASES = {
    "the day after tomorrow": 2,
    "the day before yesterday": -2,
    "today": 0,
    "tomorrow": 1,
    "yesterday": -1,
}

_UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7}


def find_date_reference(text, today=None):
  """Return ``{'date', 'offset_days', 'phrase'}`` for the first date phrase."""
  base = today or date.today()
  lowered = (text or "").lower()

  for phrase in sorted(_RELATIVE_PHRASES, key=len, reverse=True):
    if re.search(rf"\b{re.escape(phrase)}\b", lowered):
      return _make_reference(phrase, _RELATIVE_PHRASES[phrase], base)

  match = re.search(r"\bin\s+(\d{1,3})\s+(days?|weeks?)\b", lowered)
  if match:
    days = int(match.group(1)) * _UNIT_DAYS[match.group(2)]
    return _make_reference(match.group(0), days, base)

  match = re.search(r"\b(\d{1,3})\s+(days?|weeks?)\s+ago\b", lowered)
  if match:
    days = int(match.group(1)) * _UNIT_DAYS[match.group(2)]
    return _make_reference(match.group(0), -days, base)

  match = re.search(
      r"\b(next|last|this)\s+(" + "|".join(WEEKDAYS) + r")\b", lowered
  )
  if match:
    target = WEEKDAYS[match.group(2)]
    if match.group(1) == "last":
      offset = -((base.weekday() - target) % 7 or 7)
    else:
      offset = (target - base.weekday()) % 7 or 7
    return _make_reference(match.group(0), offset, base)

  match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", lowered)
  if match:
    try:
      target = date(
          int(match.group(1)), int(match.group(2)), int(match.group(3))
      )
    except ValueError:
      return None
    return {
        "phrase": match.group(0),
        "date": target.isoformat(),
        "offset_days": (target - base).days,
    }
  return None


def _make_reference(phrase, offset_days, base):
  return {
      "phrase": phrase,
      "date": (base + timedelta(days=offset_days)).isoformat(),
      "offset_days": offset_days,
  }
