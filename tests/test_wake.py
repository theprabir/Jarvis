"""Feature set 4.21 tests: passive wake-word detection and listener control."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.config import ConfigError, load_settings
from voice.audio import FRAME_BYTES
from voice.wake import (
  VoskWakeDetector,
  WakeDetection,
  WakeWordListener,
  matches_wake_word,
  normalize_words,
)


class FakeRecognizer:
  text = "hey jarvis"
  partial = ""

  def __init__(self, model, rate, grammar):
    self.model = model
    self.rate = rate
    self.grammar = json.loads(grammar)
    self.frames = 0

  def AcceptWaveform(self, pcm):
    self.frames += 1
    return self.frames >= 2

  def Result(self):
    return json.dumps({"text": self.text})

  def PartialResult(self):
    return json.dumps({"partial": self.partial})


class FakeTranscriber:
  model = object()
  recognizer_class = FakeRecognizer


class WakeMatchTests(unittest.TestCase):
  def test_normalize_words(self):
    self.assertEqual(normalize_words("  Hey, JARVIS!  "), "hey jarvis")

  def test_tail_match_accepts_leading_noise(self):
    self.assertTrue(matches_wake_word("um hey jarvis", "hey jarvis"))
    self.assertTrue(matches_wake_word("hey jarvis", "hey jarvis"))
    self.assertFalse(matches_wake_word("hey jarvis what time", "hey jarvis"))
    self.assertFalse(matches_wake_word("hey jarvis", "hey computer"))

  def test_bare_final_word_matches_variant(self):
    self.assertTrue(matches_wake_word("jarvis", "jarvis"))


class VoskWakeDetectorTests(unittest.TestCase):
  def setUp(self):
    self.detector = VoskWakeDetector(
        FakeTranscriber(), phrase="hey jarvis", cooldown_ms=500
    )

  def tearDown(self):
    self.detector.close()

  def test_detection_and_cooldown(self):
    frame = b"S" * FRAME_BYTES
    self.assertIsNone(self.detector.accept(frame))
    detection = self.detector.accept(frame)
    self.assertIsInstance(detection, WakeDetection)
    self.assertEqual(detection.phrase, "hey jarvis")
    self.assertIsNone(self.detector.accept(frame))

  def test_bare_word_is_accepted(self):
    FakeRecognizer.text = "jarvis"
    try:
      frame = b"S" * FRAME_BYTES
      self.detector.accept(frame)
      detection = self.detector.accept(frame)
    finally:
      FakeRecognizer.text = "hey jarvis"
    self.assertIsNotNone(detection)
    self.assertEqual(detection.text, "jarvis")

  def test_grammar_contains_phrase_and_unknown_marker(self):
    # The fake keeps the constructor arguments for inspection.
    self.assertIn("hey jarvis", self.detector._grammar)
    self.assertIn("jarvis", self.detector._grammar)
    self.assertIn("[unk]", self.detector._grammar)

  def test_wrong_frame_size_is_ignored(self):
    self.assertIsNone(self.detector.accept(b"short"))


class FakeDetector:
  def __init__(self):
    self.closed = False

  def accept(self, frame):
    return WakeDetection(phrase="hey jarvis", text="hey jarvis")

  def close(self):
    self.closed = True


class FakeMicrophone:
  def frames(self, abort):
    while not abort():
      yield b"S" * FRAME_BYTES
      time.sleep(0.002)


class WakeListenerTests(unittest.TestCase):
  def test_listener_starts_paused_and_stops_cleanly(self):
    hits = []
    detector = FakeDetector()
    listener = WakeWordListener(
        FakeMicrophone(), detector, lambda detection: hits.append(detection)
    )
    listener.start()
    try:
      time.sleep(0.08)
      self.assertTrue(listener.paused)
      self.assertEqual(hits, [])
      listener.resume()
      deadline = time.monotonic() + 2.0
      while not hits and time.monotonic() < deadline:
        time.sleep(0.01)
      self.assertEqual(len(hits), 1)
      self.assertTrue(listener.paused)
    finally:
      listener.stop()
    self.assertFalse(listener.alive)
    self.assertTrue(detector.closed)

  def test_pause_prevents_detection_until_resume(self):
    hits = []
    detector = FakeDetector()
    listener = WakeWordListener(
        FakeMicrophone(), detector, lambda detection: hits.append(detection)
    )
    listener.start()
    try:
      listener.resume()
      time.sleep(0.05)
      listener.pause()
      count = len(hits)
      time.sleep(0.08)
      self.assertEqual(len(hits), count)
    finally:
      listener.stop()


class WakeInterfaceTests(unittest.TestCase):
  def test_off_and_status_control_the_listener(self):
    from voice.interface import VoiceInterface

    class Listener:
      def __init__(self):
        self.paused = False
        self.last_error = ""

      def pause(self):
        self.paused = True
        return True

      def resume(self):
        self.paused = False

    voice = VoiceInterface(load_settings(), lambda text: text)
    voice._wake_listener = Listener()
    voice._wake_enabled = True
    result = voice._wake_command("/wake off")
    self.assertIn("disabled", result)
    self.assertTrue(voice._wake_listener.paused)
    self.assertFalse(voice._wake_enabled)
    self.assertIn("off", voice._wake_command("/wake status"))

  def test_notify_pauses_and_resumes(self):
    from voice.interface import VoiceInterface

    events = []

    class Listener:
      def pause(self):
        events.append("pause")

      def resume(self):
        events.append("resume")

    voice = VoiceInterface(load_settings(), lambda text: text)
    voice._wake_listener = Listener()
    voice._wake_enabled = True
    voice._tts = None
    voice.notify("Briefing ready.")
    self.assertEqual(events, ["pause", "resume"])


class WakeConfigTests(unittest.TestCase):
  def test_defaults(self):
    voice = load_settings().voice
    self.assertTrue(voice.wake_enabled)
    self.assertEqual(voice.wake_phrase, "hey jarvis")
    self.assertEqual(voice.wake_ack, "chime")
    self.assertGreaterEqual(voice.wake_cooldown_ms, 500)

  def test_invalid_ack_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[voice]\nwake_ack = \"buzz\"\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_empty_phrase_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[voice]\nwake_phrase = \"  \"\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


if __name__ == "__main__":
  unittest.main()
