"""Feature set 4.1 tests: voice config, VAD segmentation, resampling, speech.

No microphone or speaker is needed; only the optional Vosk model test touches
the model directory and skips when it or the package is missing.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from core.config import ConfigError, load_settings
from voice.audio import (
    END_OF_STREAM,
    FRAME_BYTES,
    MAX_DURATION,
    NO_SPEECH,
    SILENCE,
    Segmenter,
    input_devices,
    resample_pcm16,
    resolve_input_device,
)
from voice.errors import AudioError, SttError
from voice.tts import _sapi_rate, clean_for_speech

try:
  import vosk  # noqa: F401

  HAVE_VOSK = True
except ImportError:
  HAVE_VOSK = False

FRAME = FRAME_BYTES


def speech_frame():
  return b"S" * FRAME


def silent_frame():
  return b"." * FRAME


class VoiceConfigTests(unittest.TestCase):
  def test_project_config_has_voice_defaults(self):
    voice = load_settings().voice
    self.assertTrue(voice.enabled)
    self.assertEqual(voice.vad_aggressiveness, 2)
    self.assertEqual(voice.ptt_key, "ctrl+space")
    self.assertTrue(voice.resolved_stt_model.is_absolute())

  def test_invalid_vad_aggressiveness_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[voice]\nvad_aggressiveness = 4\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_unknown_voice_key_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[voice]\nmic = 3\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_silence_out_of_range_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text("[voice]\nsilence_ms = 10\n", encoding="utf-8")
      with self.assertRaises(ConfigError):
        load_settings(path)


class SegmenterTests(unittest.TestCase):
  def make(self, **overrides):
    options = {
        "is_speech": lambda frame: frame == speech_frame(),
        "frame_ms": 30,
        "silence_ms": 60,
        "min_speech_ms": 60,
        "max_record_s": 15.0,
        "preroll_ms": 60,
    }
    options.update(overrides)
    return Segmenter(**options)

  def test_silence_ends_utterance(self):
    segmenter = self.make()
    for _ in range(3):
      self.assertIsNone(segmenter.feed(speech_frame()))
    self.assertIsNone(segmenter.feed(silent_frame()))
    utterance = segmenter.feed(silent_frame())
    self.assertIsNotNone(utterance)
    self.assertEqual(utterance.reason, SILENCE)
    self.assertEqual(utterance.speech_ms, 90)
    self.assertEqual(len(utterance.pcm), 5 * FRAME)

  def test_short_noise_is_discarded(self):
    segmenter = self.make(min_speech_ms=250)
    self.assertIsNone(segmenter.feed(speech_frame()))
    segmenter.feed(silent_frame())
    utterance = segmenter.feed(silent_frame())
    self.assertEqual(utterance.reason, NO_SPEECH)
    self.assertEqual(utterance.pcm, b"")

  def test_max_duration_stops_long_speech(self):
    segmenter = self.make(max_record_s=0.15, silence_ms=1000)
    utterance = None
    for _ in range(5):
      utterance = segmenter.feed(speech_frame())
    self.assertIsNotNone(utterance)
    self.assertEqual(utterance.reason, MAX_DURATION)
    self.assertEqual(len(utterance.pcm), 5 * FRAME)

  def test_preroll_keeps_first_syllable(self):
    segmenter = self.make()
    segmenter.feed(silent_frame())
    segmenter.feed(silent_frame())
    segmenter.feed(speech_frame())
    segmenter.feed(speech_frame())
    segmenter.feed(silent_frame())
    utterance = segmenter.feed(silent_frame())
    # 2 pre-roll + 2 speech + 2 trailing silence frames.
    self.assertEqual(len(utterance.pcm), 6 * FRAME)

  def test_close_without_speech(self):
    segmenter = self.make()
    segmenter.feed(silent_frame())
    utterance = segmenter.close()
    self.assertEqual(utterance.reason, NO_SPEECH)
    self.assertEqual(utterance.pcm, b"")

  def test_close_mid_utterance(self):
    segmenter = self.make()
    for _ in range(4):
      segmenter.feed(speech_frame())
    utterance = segmenter.close()
    self.assertEqual(utterance.reason, END_OF_STREAM)
    self.assertEqual(len(utterance.pcm), 4 * FRAME)
    self.assertEqual(utterance.duration_ms, 120)

  def test_frames_after_finish_are_ignored(self):
    segmenter = self.make()
    for _ in range(3):
      segmenter.feed(speech_frame())
    segmenter.feed(silent_frame())
    first = segmenter.feed(silent_frame())
    self.assertIs(first, segmenter.feed(speech_frame()))


class ResampleTests(unittest.TestCase):
  def test_44100_to_16000(self):
    source = (np.arange(441, dtype=np.int16) * 10).tobytes()
    result = resample_pcm16(source, 44100)
    samples = np.frombuffer(result, dtype=np.int16)
    self.assertLessEqual(abs(samples.size - 160), 1)

  def test_same_rate_is_returned_unchanged(self):
    source = b"\x01\x02\x03\x04"
    self.assertEqual(resample_pcm16(source, 16000), source)

  def test_empty_input(self):
    self.assertEqual(resample_pcm16(b"", 44100), b"")


class DeviceTests(unittest.TestCase):
  DEVICES = [
      {"name": "Speakers", "max_input_channels": 0, "default_samplerate": 44100},
      {
          "name": "Microphone (USB Audio)",
          "max_input_channels": 2,
          "default_samplerate": 44100,
      },
  ]

  def test_empty_query_uses_default(self):
    self.assertIsNone(resolve_input_device("", self.DEVICES))

  def test_name_substring_matches(self):
    self.assertEqual(resolve_input_device("usb", self.DEVICES), 1)

  def test_unknown_name_raises_with_choices(self):
    with self.assertRaises(AudioError) as caught:
      resolve_input_device("webcam", self.DEVICES)
    self.assertIn("Microphone (USB Audio)", str(caught.exception))

  def test_only_input_devices_are_listed(self):
    self.assertEqual(input_devices(self.DEVICES), [(1, "Microphone (USB Audio)")])


class SpeechTextTests(unittest.TestCase):
  def test_markdown_and_links_are_removed(self):
    text = "**Hello** there. See https://example.com for more."
    cleaned = clean_for_speech(text)
    self.assertNotIn("*", cleaned)
    self.assertNotIn("http", cleaned)
    self.assertIn("Hello there.", cleaned)

  def test_bullets_and_newlines_collapse(self):
    self.assertEqual(clean_for_speech("- one\n- two"), "one two")

  def test_code_blocks_are_dropped(self):
    self.assertEqual(clean_for_speech("Use this:\n```x=1```\ndone"), "Use this: done")

  def test_empty_text(self):
    self.assertEqual(clean_for_speech(""), "")

  def test_sapi_rate_mapping(self):
    self.assertEqual(_sapi_rate(175), 0)
    self.assertEqual(_sapi_rate(400), 10)
    self.assertEqual(_sapi_rate(80), -5)


class InterruptTests(unittest.TestCase):
  """Escape must stop speech even when the keyboard hook is unavailable."""

  def make(self):
    from voice.interface import VoiceInterface

    return VoiceInterface(load_settings(), lambda text: text)

  def test_escape_during_speech_sets_the_interrupt(self):
    import threading
    import time
    from unittest import mock

    from voice import interface as voice_interface

    voice = self.make()
    voice._speaking = True
    with mock.patch.object(
        voice_interface, "_escape_was_pressed", return_value=True
    ):
      thread = threading.Thread(target=voice._watch_escape, daemon=True)
      thread.start()
      time.sleep(0.1)
      voice._stop_watch.set()
      thread.join(timeout=1.0)
    self.assertTrue(voice._interrupt.is_set())

  def test_escape_while_idle_is_ignored(self):
    import threading
    import time
    from unittest import mock

    from voice import interface as voice_interface

    voice = self.make()
    with mock.patch.object(
        voice_interface, "_escape_was_pressed", return_value=True
    ):
      thread = threading.Thread(target=voice._watch_escape, daemon=True)
      thread.start()
      time.sleep(0.1)
      voice._stop_watch.set()
      thread.join(timeout=1.0)
    self.assertFalse(voice._interrupt.is_set())


class TranscriberTests(unittest.TestCase):
  def test_missing_model_raises(self):
    from voice.stt import VoskTranscriber

    with tempfile.TemporaryDirectory() as tmp:
      with self.assertRaises(SttError):
        VoskTranscriber(Path(tmp) / "missing-model")

  @unittest.skipUnless(HAVE_VOSK, "vosk is not installed")
  def test_silence_transcribes_to_empty_text(self):
    from voice.stt import VoskTranscriber

    model = load_settings().voice.resolved_stt_model
    if not model.is_dir():
      self.skipTest(f"Vosk model not present at {model}")
    transcriber = VoskTranscriber(model)
    try:
      text = transcriber.transcribe(bytes(FRAME) * 30)
    finally:
      transcriber.close()
    self.assertEqual(text, "")


if __name__ == "__main__":
  unittest.main()
