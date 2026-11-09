"""Deterministic utilities: units, sizes, text and reminder parsing.

Everything here is pure Python: no model call and no network. The assistant
uses these helpers so conversions and timers answer instantly and the same
way every time.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from core.calculator import format_number
from core.dates import find_date_reference

LENGTH = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "km": 1000.0,
    "in": 0.0254,
    "ft": 0.3048,
    "yd": 0.9144,
    "mi": 1609.344,
}
MASS = {
    "mg": 1e-6,
    "g": 0.001,
    "kg": 1.0,
    "oz": 0.0283495,
    "lb": 0.453592,
    "t": 1000.0,
}
VOLUME = {
    "ml": 0.001,
    "l": 1.0,
    "cup": 0.236588,
    "pt": 0.473176,
    "qt": 0.946353,
    "gal": 3.78541,
}
SPEED = {"ms": 1.0, "kmh": 0.277778, "mph": 0.44704, "knot": 0.514444}
DATA = {
    "b": 1.0,
    "kb": 1000.0,
    "mb": 1000.0 ** 2,
    "gb": 1000.0 ** 3,
    "tb": 1000.0 ** 4,
    "kib": 1024.0,
    "mib": 1024.0 ** 2,
    "gib": 1024.0 ** 3,
    "tib": 1024.0 ** 4,
}
SIZE_BYTES = {
    "b": 1.0,
    "kb": 1024.0,
    "kib": 1024.0,
    "mb": 1024.0 ** 2,
    "mib": 1024.0 ** 2,
    "gb": 1024.0 ** 3,
    "gib": 1024.0 ** 3,
    "tb": 1024.0 ** 4,
    "tib": 1024.0 ** 4,
}
TIME = {
    "s": 1.0,
    "min": 60.0,
    "h": 3600.0,
    "day": 86400.0,
}

TABLES = (
    ("length", LENGTH),
    ("mass", MASS),
    ("volume", VOLUME),
    ("speed", SPEED),
    ("data", DATA),
    ("time", TIME),
)

ALIASES = {
    "millimeter": "mm",
    "millimeters": "mm",
    "millimetre": "mm",
    "millimetres": "mm",
    "centimeter": "cm",
    "centimeters": "cm",
    "centimetre": "cm",
    "centimetres": "cm",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "inch": "in",
    "inches": "in",
    "foot": "ft",
    "feet": "ft",
    "yard": "yd",
    "yards": "yd",
    "mile": "mi",
    "miles": "mi",
    "milligram": "mg",
    "milligrams": "mg",
    "gram": "g",
    "grams": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "ounce": "oz",
    "ounces": "oz",
    "pound": "lb",
    "pounds": "lb",
    "lbs": "lb",
    "ton": "t",
    "tons": "t",
    "tonne": "t",
    "tonnes": "t",
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "cups": "cup",
    "pint": "pt",
    "pints": "pt",
    "quart": "qt",
    "quarts": "qt",
    "gallon": "gal",
    "gallons": "gal",
    "kph": "kmh",
    "km/h": "kmh",
    "kmph": "kmh",
    "mph": "mph",
    "knots": "knot",
    "byte": "b",
    "bytes": "b",
    "kilobyte": "kb",
    "kilobytes": "kb",
    "megabyte": "mb",
    "megabytes": "mb",
    "gigabyte": "gb",
    "gigabytes": "gb",
    "terabyte": "tb",
    "terabytes": "tb",
    "second": "s",
    "seconds": "s",
    "sec": "s",
    "secs": "s",
    "minute": "min",
    "minutes": "min",
    "mins": "min",
    "hour": "h",
    "hours": "h",
    "hrs": "h",
    "days": "day",
}

TEMPERATURE = {
    "c": "c",
    "celsius": "c",
    "centigrade": "c",
    "f": "f",
    "fahrenheit": "f",
    "k": "k",
    "kelvin": "k",
}
TEMPERATURE_LABELS = {"c": "°C", "f": "°F", "k": "K"}

DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h"
    r"|days?|d)\b"
)
REMIND_IN = re.compile(r"^remind me (?:to )?(?P<task>.+?) in (?P<duration>.+)$")
REMIND_IN_FIRST = re.compile(
    r"^remind me in (?P<duration>.+?) to (?P<task>.+)$"
)
REMIND_AT = re.compile(
    r"^remind me (?:to )?(?P<task>.+?) at "
    r"(?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)$"
)
REMIND_WHEN = re.compile(
    r"^remind me (?:to )?(?P<task>.+?) "
    r"(?P<when>tomorrow|today|tonight)"
    r"(?: at (?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?$"
)
CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


class UtilityError(ValueError):
  """Raised when a conversion, transform or reminder cannot be handled."""


def _unit(value):
  text = str(value or "").strip().lower()
  text = text.replace("°", "").replace("degrees", "").strip()
  text = text.replace(" ", "")
  return ALIASES.get(text, text)


def _temperature(value, source, target):
  celsius = value
  if source == "f":
    celsius = (value - 32.0) * 5.0 / 9.0
  elif source == "k":
    celsius = value - 273.15
  if target == "c":
    return celsius
  if target == "f":
    return celsius * 9.0 / 5.0 + 32.0
  return celsius + 273.15


def convert(value, source, target):
  """Convert a quantity and return ``(result, unit_label)``."""
  src = _unit(source)
  dst = _unit(target)
  if not src or not dst:
    raise UtilityError("both units are needed")
  if src in TEMPERATURE or dst in TEMPERATURE:
    if src not in TEMPERATURE or dst not in TEMPERATURE:
      raise UtilityError("temperature cannot be mixed with other units")
    return (
        _temperature(float(value), TEMPERATURE[src], TEMPERATURE[dst]),
        TEMPERATURE_LABELS[TEMPERATURE[dst]],
    )
  for _name, table in TABLES:
    if src in table and dst in table:
      result = float(value) * table[src] / table[dst]
      return result, dst
  raise UtilityError(f"I don't know how to convert {source} to {target}")


def format_quantity(value):
  """Render a converted number without noisy precision."""
  if abs(value) >= 100:
    return format_number(round(value, 1))
  if abs(value) >= 1:
    return format_number(round(value, 3))
  return format_number(round(value, 5))


def format_size(value, unit="b"):
  """Format a byte count in the largest sensible binary unit."""
  byte_value = float(value) * SIZE_BYTES.get(_unit(unit), 1.0)
  names = (("TB", 1024.0 ** 4), ("GB", 1024.0 ** 3),
           ("MB", 1024.0 ** 2), ("KB", 1024.0))
  for name, factor in names:
    if byte_value >= factor:
      return f"{format_quantity(byte_value / factor)} {name}"
  return f"{format_number(int(byte_value))} B"


def transform_text(operation, text):
  """Apply one deterministic text operation."""
  body = str(text or "")
  if not body.strip():
    raise UtilityError("there is no text to work with")
  action = operation.strip().lower()
  if action in {"uppercase", "upper", "caps"}:
    return body.upper()
  if action in {"lowercase", "lower"}:
    return body.lower()
  if action in {"title", "title case"}:
    return body.title()
  if action in {"capitalize", "capitalise", "sentence case"}:
    return body[:1].upper() + body[1:]
  if action in {"reverse", "reversed"}:
    return body[::-1]
  if action in {"strip", "trim"}:
    return body.strip()
  if action in {"words", "word count", "count words"}:
    return str(len(body.split()))
  if action in {"characters", "character count", "chars", "letters"}:
    return str(len(body))
  raise UtilityError(f"I don't know the text operation {operation!r}")


def parse_duration(text):
  """Return a duration in seconds, or None when no amount is present."""
  parts = DURATION_PART.findall(str(text or "").lower())
  if not parts:
    return None
  total = 0.0
  for amount, unit in parts:
    normalized = _unit(unit)
    if unit.startswith("d"):
      normalized = "day"
    elif unit.startswith("h"):
      normalized = "h"
    elif unit.startswith("m") and normalized != "m":
      normalized = "min"
    elif unit.startswith("s"):
      normalized = "s"
    if normalized not in TIME:
      normalized = "min" if unit.startswith("m") else "s"
    total += float(amount) * TIME[normalized]
  seconds = int(round(total))
  return seconds if seconds > 0 else None


def humanize_duration(seconds):
  """Render seconds as a short spoken duration."""
  seconds = max(1, int(seconds))
  minutes, rest = divmod(seconds, 60)
  hours, minutes = divmod(minutes, 60)
  parts = []
  if hours:
    parts.append(f"{hours} hour" + ("" if hours == 1 else "s"))
  if minutes:
    parts.append(f"{minutes} minute" + ("" if minutes == 1 else "s"))
  if rest and not hours:
    parts.append(f"{rest} second" + ("" if rest == 1 else "s"))
  return " and ".join(parts) or "1 second"


def _clock(text):
  match = CLOCK.match(str(text or "").strip().lower())
  if not match:
    raise UtilityError("I couldn't read that time")
  hour = int(match.group(1))
  minute = int(match.group(2) or 0)
  meridiem = match.group(3)
  if minute > 59:
    raise UtilityError("minutes must be below 60")
  if meridiem:
    if not 1 <= hour <= 12:
      raise UtilityError("hour must be between 1 and 12 with am or pm")
    hour = hour % 12 + (12 if meridiem == "pm" else 0)
  elif not 0 <= hour <= 23:
    raise UtilityError("hour must be between 0 and 23")
  return hour, minute


def parse_reminder(text, now=None):
  """Return ``(due_datetime, task)`` for a reminder request, or None."""
  now = now or datetime.now()
  lowered = " ".join(str(text or "").lower().split()).rstrip("?.!")
  match = REMIND_IN_FIRST.match(lowered) or REMIND_IN.match(lowered)
  if match:
    seconds = parse_duration(match.group("duration"))
    if not seconds:
      return None
    task = match.group("task").strip(" ,")
    if not task:
      return None
    return now + timedelta(seconds=seconds), task

  reference = find_date_reference(lowered, now.date())
  base = now.date()
  if reference:
    try:
      base = date.fromisoformat(reference["date"])
    except (KeyError, ValueError):
      base = now.date()

  match = REMIND_WHEN.match(lowered) or REMIND_AT.match(lowered)
  if not match:
    return None
  task = match.group("task").strip(" ,")
  if not task:
    return None
  when = (match.groupdict().get("when") or "").strip()
  clock = (match.groupdict().get("clock") or "").strip()
  if when == "tomorrow":
    base = now.date() + timedelta(days=1)
  elif when == "tonight":
    clock = clock or "8 pm"
  elif not reference and when == "today":
    base = now.date()

  if clock:
    hour, minute = _clock(clock)
  else:
    hour, minute = 9, 0
  due = datetime.combine(base, datetime.min.time()).replace(
      hour=hour, minute=minute
  )
  if due <= now and not when:
    due += timedelta(days=1)
  if due <= now and when == "today":
    due += timedelta(days=1)
  return due, task
