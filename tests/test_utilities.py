"""Feature set 4.7 tests: conversions, text utilities, weather, timers.

All offline: weather uses recorded payloads and timers are polled manually.
"""

from __future__ import annotations

import dataclasses
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.assistant import Assistant
from core.config import WeatherConfig, load_settings
from core.intents import IntentRouter
from core.timers import TimerService
from core.utilities import (
    UtilityError,
    convert,
    format_quantity,
    format_size,
    humanize_duration,
    parse_duration,
    parse_reminder,
    transform_text,
)
from core.weather import WeatherClient, WeatherError


class ConversionTests(unittest.TestCase):
  def test_length_and_mass(self):
    result, unit = convert(10, "km", "miles")
    self.assertEqual(unit, "mi")
    self.assertAlmostEqual(result, 6.2137, places=3)
    result, unit = convert(1, "kg", "pounds")
    self.assertEqual(unit, "lb")
    self.assertAlmostEqual(result, 2.2046, places=3)

  def test_temperature(self):
    result, unit = convert(25, "celsius", "fahrenheit")
    self.assertEqual(unit, "°F")
    self.assertAlmostEqual(result, 77.0)
    result, unit = convert(212, "f", "c")
    self.assertEqual(unit, "°C")
    self.assertAlmostEqual(result, 100.0)

  def test_data_sizes(self):
    result, unit = convert(500, "mb", "gb")
    self.assertEqual(unit, "gb")
    self.assertAlmostEqual(result, 0.5)

  def test_unknown_units_are_rejected(self):
    with self.assertRaises(UtilityError):
      convert(1, "banana", "kg")
    with self.assertRaises(UtilityError):
      convert(1, "kg", "celsius")

  def test_format_quantity(self):
    self.assertEqual(format_quantity(6.21371), "6.214")
    self.assertEqual(format_quantity(100.04), "100")

  def test_format_size(self):
    self.assertEqual(format_size(1048576), "1 MB")
    self.assertEqual(format_size(1536), "1.5 KB")
    self.assertEqual(format_size(512), "512 B")
    self.assertEqual(format_size(2, "gb"), "2 GB")


class TextTests(unittest.TestCase):
  def test_transforms(self):
    self.assertEqual(transform_text("uppercase", "hello world"), "HELLO WORLD")
    self.assertEqual(transform_text("lowercase", "HELLO"), "hello")
    self.assertEqual(transform_text("title case", "hello world"), "Hello World")
    self.assertEqual(transform_text("reverse", "abc"), "cba")

  def test_counts(self):
    self.assertEqual(transform_text("words", "one two three"), "3")
    self.assertEqual(transform_text("characters", "abc"), "3")

  def test_unknown_operation(self):
    with self.assertRaises(UtilityError):
      transform_text("shout", "hi")


class DurationTests(unittest.TestCase):
  def test_parse_duration(self):
    self.assertEqual(parse_duration("5 minutes"), 300)
    self.assertEqual(parse_duration("90 seconds"), 90)
    self.assertEqual(parse_duration("1 hour 30 minutes"), 5400)
    self.assertEqual(parse_duration("2 hrs"), 7200)
    self.assertIsNone(parse_duration("soon"))

  def test_humanize_duration(self):
    self.assertEqual(humanize_duration(300), "5 minutes")
    self.assertEqual(humanize_duration(5400), "1 hour and 30 minutes")
    self.assertEqual(humanize_duration(1), "1 second")


class ReminderParsingTests(unittest.TestCase):
  NOW = datetime(2026, 11, 9, 18, 0)

  def test_relative_reminder(self):
    due, task = parse_reminder("remind me in 20 minutes to stretch", self.NOW)
    self.assertEqual(due, self.NOW + timedelta(minutes=20))
    self.assertEqual(task, "stretch")

  def test_clock_reminder_rolls_to_tomorrow(self):
    due, task = parse_reminder("remind me to call mom at 5 pm", self.NOW)
    self.assertEqual(task, "call mom")
    self.assertEqual(due.date(), self.NOW.date() + timedelta(days=1))
    self.assertEqual((due.hour, due.minute), (17, 0))

  def test_tomorrow_reminder(self):
    due, task = parse_reminder(
        "remind me to submit the report tomorrow at 9", self.NOW
    )
    self.assertEqual(task, "submit the report")
    self.assertEqual(due.date(), self.NOW.date() + timedelta(days=1))
    self.assertEqual((due.hour, due.minute), (9, 0))

  def test_unparseable_reminder(self):
    self.assertIsNone(parse_reminder("remind me about the thing", self.NOW))


class WeatherTests(unittest.TestCase):
  GEO = {
      "results": [
          {
              "name": "Kolkata",
              "country": "India",
              "latitude": 22.5,
              "longitude": 88.3,
          }
      ]
  }
  FORECAST = {
      "current": {
          "temperature_2m": 30.6,
          "relative_humidity_2m": 55,
          "apparent_temperature": 34.7,
          "weather_code": 1,
          "wind_speed_10m": 2.9,
      },
      "daily": {
          "temperature_2m_max": [31.3],
          "temperature_2m_min": [23.3],
      },
  }

  def fake(self, geo=None, forecast=None):
    routes = {
        "geocoding": geo or self.GEO,
        "forecast": forecast or self.FORECAST,
    }
    return FakeHttp(routes)

  def test_current_conditions(self):
    http = self.fake()
    client = WeatherClient(WeatherConfig(timeout_s=5), client=http)
    data = client.current("Kolkata")
    self.assertEqual(data["place"], "Kolkata")
    self.assertEqual(data["description"], "mainly clear")
    self.assertEqual(data["temperature"], 30.6)
    self.assertEqual(data["high"], 31.3)
    self.assertEqual(data["unit"], "°C")
    self.assertEqual(data["wind_unit"], "km/h")

  def test_imperial_units_are_requested(self):
    http = self.fake()
    client = WeatherClient(
        WeatherConfig(units="imperial", timeout_s=5), client=http
    )
    data = client.current("Kolkata")
    self.assertEqual(data["unit"], "°F")
    forecast_params = [
        params for url, params in http.calls if "forecast" in url
    ][0]
    self.assertEqual(forecast_params["temperature_unit"], "fahrenheit")

  def test_unknown_place(self):
    client = WeatherClient(
        WeatherConfig(timeout_s=5),
        client=self.fake(geo={"results": []}),
    )
    with self.assertRaises(WeatherError):
      client.current("Atlantis")


class FakeResponse:
  def __init__(self, payload, status=200):
    self.payload = payload
    self.status = status

  def raise_for_status(self):
    if self.status >= 400:
      raise RuntimeError(f"HTTP {self.status}")

  def json(self):
    return self.payload


class FakeHttp:
  def __init__(self, routes):
    self.routes = routes
    self.calls = []

  def get(self, url, params=None, **kwargs):
    self.calls.append((url, dict(params or {})))
    for key, payload in self.routes.items():
      if key in url:
        return FakeResponse(payload)
    raise AssertionError(f"unexpected URL {url}")


class TimerTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.path = Path(self.tmp.name) / "timers.db"

  def test_timer_fires_once(self):
    service = TimerService(self.path, autostart=False)
    self.addCleanup(service.close)
    entry = service.add_timer(60, "pasta")
    self.assertEqual(len(service.list()), 1)
    future = datetime.now() + timedelta(seconds=61)
    messages = service.poll(future)
    self.assertEqual(messages, ["Timer finished: pasta."])
    self.assertEqual(service.poll(future), [])

  def test_reminder_survives_reopen(self):
    service = TimerService(self.path, autostart=False)
    due = datetime.now() + timedelta(minutes=5)
    service.add_reminder(due, "call mom")
    service.close()

    reopened = TimerService(self.path, autostart=False)
    self.assertEqual(len(reopened.list()), 1)
    self.assertEqual(
        reopened.poll(due + timedelta(seconds=1)),
        ["Reminder: call mom."],
    )
    reopened.close()

    final = TimerService(self.path, autostart=False)
    self.addCleanup(final.close)
    self.assertEqual(final.list(), [])

  def test_cancel(self):
    service = TimerService(self.path, autostart=False)
    self.addCleanup(service.close)
    entry = service.add_timer(120)
    self.assertIsNotNone(service.cancel(entry.id))
    self.assertEqual(service.list(), [])

  def test_bad_duration_is_rejected(self):
    service = TimerService(self.path, autostart=False)
    self.addCleanup(service.close)
    with self.assertRaises(Exception):
      service.add_timer(0)


class IntentTests(unittest.TestCase):
  def setUp(self):
    self.router = IntentRouter()

  def test_unit_conversion(self):
    intent = self.router.route("convert 10 km to miles")
    self.assertEqual(intent.name, "unit_convert")
    self.assertEqual(intent.entities["unit"], "mi")
    intent = self.router.route("how many miles in 10 km")
    self.assertEqual(intent.name, "unit_convert")

  def test_text_operations(self):
    intent = self.router.route("uppercase hello world")
    self.assertEqual(intent.name, "text_transform")
    self.assertEqual(intent.entities["result"], "HELLO WORLD")
    intent = self.router.route("how many words in one two three")
    self.assertEqual(intent.entities["result"], "3")

  def test_size_formatting(self):
    intent = self.router.route("how big is 1048576 bytes")
    self.assertEqual(intent.name, "format_size")
    self.assertEqual(intent.entities["result"], "1 MB")

  def test_weather(self):
    intent = self.router.route("what's the weather in Kolkata")
    self.assertEqual(intent.name, "weather")
    self.assertEqual(intent.entities["location"], "kolkata")

  def test_timer_set_and_list(self):
    intent = self.router.route("set a timer for 5 minutes named pasta")
    self.assertEqual(intent.name, "timer_set")
    self.assertEqual(intent.entities["seconds"], 300)
    self.assertEqual(intent.entities["label"], "pasta")
    self.assertEqual(
        self.router.route("list timers").name, "timer_list"
    )

  def test_reminder_set(self):
    intent = self.router.route("remind me in 20 minutes to stretch")
    self.assertEqual(intent.name, "reminder_set")
    self.assertIn("due_at", intent.entities)
    self.assertEqual(intent.entities["text"], "stretch")

  def test_timer_cancel(self):
    intent = self.router.route("cancel timer 2")
    self.assertEqual(intent.name, "timer_cancel")
    self.assertEqual(intent.entities["id"], 2)
    self.assertFalse(intent.entities["all"])


class NoModelClient:
  """Fail loudly if a deterministic request ever reaches the model."""

  def __init__(self):
    self.calls = 0

  def chat(self, *args, **kwargs):
    self.calls += 1
    raise AssertionError("the model must not be called")


class AssistantUtilityTests(unittest.TestCase):
  def make(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    base = load_settings()
    settings = dataclasses.replace(
        base,
        conversation=dataclasses.replace(base.conversation, enabled=False),
        memory=dataclasses.replace(
            base.memory, enabled=True, db=str(Path(tmp.name) / "a.db")
        ),
        weather=dataclasses.replace(base.weather, enabled=False),
    )
    assistant = Assistant(settings, client=NoModelClient())
    self.addCleanup(assistant.close)
    return assistant

  def test_conversion_reply(self):
    answer = self.make().reply("convert 10 km to miles")
    self.assertIn("6.214 mi", answer)

  def test_text_reply(self):
    answer = self.make().reply("uppercase hello world")
    self.assertEqual(answer, "HELLO WORLD")

  def test_timer_lifecycle(self):
    assistant = self.make()
    answer = assistant.reply("set a timer for 10 minutes named pasta")
    self.assertIn("10 minutes", answer)
    self.assertIn("pasta", answer)
    self.assertIn("pasta", assistant.reply("list timers"))
    self.assertIn("Cancelled", assistant.reply("cancel timer 1"))
    self.assertIn("no timers", assistant.reply("list timers"))

  def test_reminder_reply(self):
    assistant = self.make()
    answer = assistant.reply("remind me in 20 minutes to stretch")
    self.assertIn("remind you to stretch", answer)
    self.assertIn("reminder stretch", assistant.reply("list timers"))

  def test_weather_disabled(self):
    assistant = self.make()
    answer = assistant.reply("what's the weather in Kolkata")
    self.assertEqual(answer, "Weather is disabled, sir.")


class NotifierTests(unittest.TestCase):
  def test_timer_notification_calls_back(self):
    messages = []
    fired = threading.Event()

    def notify(text):
      messages.append(text)
      fired.set()

    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    service = TimerService(
        Path(tmp.name) / "notify.db",
        notify=notify,
        poll_s=0.2,
    )
    self.addCleanup(service.close)
    service.add_timer(1, "tea")
    self.assertTrue(fired.wait(3.0))
    self.assertEqual(messages, ["Timer finished: tea."])


if __name__ == "__main__":
  unittest.main()
