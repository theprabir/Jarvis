"""Feature set 4.22 tests: natural Edge speech and the local SAPI fallback."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.config import ConfigError, load_settings
from voice.edge_tts import EdgeVoice, split_sentences
from voice.errors import SpeechError
from voice.tts import FallbackVoice


class TtsConfigTests(unittest.TestCase):
  def test_project_defaults_to_edge_with_sapi_fallback(self):
    tts = load_settings().tts
    self.assertTrue(tts.enabled)
    self.assertEqual(tts.engine, "edge")
    self.assertEqual(tts.fallback_engine, "sapi")
    self.assertEqual(tts.edge_voice, "en-GB-RyanNeural")
    self.assertEqual(tts.edge_rate, "+0%")
    self.assertEqual(tts.edge_pitch, "+0Hz")

  def test_invalid_edge_settings_are_rejected(self):
    for section, fragment in (
        ('engine = "cloud"', "engine"),
        ('fallback_engine = "edge"', "fallback_engine"),
        ('edge_voice = ""', "edge_voice"),
        ('edge_rate = "fast"', "edge_rate"),
        ('edge_pitch = "0Hz"', "edge_pitch"),
        ("edge_timeout_s = 1.0", "edge_timeout_s"),
    ):
      with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.toml"
        path.write_text("[tts]\n" + section + "\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
          load_settings(path)
        self.assertIn(fragment, str(caught.exception))

  def test_sapi_only_configuration_remains_valid(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text(
          '[tts]\nengine = "sapi"\nfallback_engine = "none"\n',
          encoding="utf-8",
      )
      tts = load_settings(path).tts
      self.assertEqual(tts.engine, "sapi")
      self.assertEqual(tts.fallback_engine, "none")


class SentenceTests(unittest.TestCase):
  def test_sentences_keep_punctuation(self):
    self.assertEqual(
        split_sentences("One. Two! Three?"),
        ["One.", "Two!", "Three?"],
    )

  def test_long_text_is_split_without_losing_words(self):
    text = " ".join(["word"] * 120)
    pieces = split_sentences(text, max_chars=50)
    self.assertGreater(len(pieces), 1)
    self.assertTrue(all(len(piece) <= 50 for piece in pieces))
    self.assertEqual(" ".join(pieces), text)


class EdgeVoiceTests(unittest.TestCase):
  def make(self):
    return EdgeVoice(load_settings().tts)

  def test_speak_synthesizes_each_sentence(self):
    voice = self.make()
    synthesized = []
    voice._synthesize = lambda text, should_stop=None: (
        synthesized.append(text) or b"mp3"
    )
    voice._decode = lambda mp3: ("samples", 24000)
    voice._play = lambda decoded, should_stop=None: False

    voice.speak("One. Two!")

    self.assertEqual(synthesized, ["One.", "Two!"])

  def test_interruption_stops_before_the_next_sentence(self):
    voice = self.make()
    synthesized = []
    voice._synthesize = lambda text, should_stop=None: (
        synthesized.append(text) or b"mp3"
    )
    voice._decode = lambda mp3: ("samples", 24000)
    voice._play = lambda decoded, should_stop=None: True

    voice.speak("One. Two. Three.")

    self.assertEqual(synthesized, ["One."])

  def test_empty_and_markup_only_text_is_silent(self):
    voice = self.make()
    voice._synthesize = mock.Mock()
    voice.speak("**```code```**")
    voice._synthesize.assert_not_called()

  def test_stop_callback_before_synthesis(self):
    voice = self.make()
    voice._synthesize = mock.Mock()
    voice.speak("Hello.", should_stop=lambda: True)
    voice._synthesize.assert_not_called()


class FakePrimary:
  def __init__(self, error=None):
    self.error = error
    self.spoken = []
    self.stopped = False
    self.closed = False

  def speak(self, text, should_stop=None):
    if self.error is not None:
      raise self.error
    self.spoken.append(text)

  def stop(self):
    self.stopped = True

  def close(self):
    self.closed = True


class FakeFallback:
  def __init__(self):
    self.spoken = []
    self.stopped = False
    self.closed = False

  def speak(self, text, should_stop=None):
    self.spoken.append(text)

  def stop(self):
    self.stopped = True

  def close(self):
    self.closed = True


class FallbackVoiceTests(unittest.TestCase):
  def test_primary_success_does_not_create_sapi(self):
    primary = FakePrimary()
    created = []
    voice = FallbackVoice(primary, lambda: created.append(FakeFallback()))

    voice.speak("Hello.")

    self.assertEqual(primary.spoken, ["Hello."])
    self.assertEqual(created, [])

  def test_failure_creates_and_uses_the_lazy_fallback(self):
    primary = FakePrimary(SpeechError("network down"))
    fallback = FakeFallback()
    created = []
    voice = FallbackVoice(
        primary, lambda: created.append(fallback) or fallback
    )

    voice.speak("Hello.")

    self.assertEqual(created, [fallback])
    self.assertEqual(fallback.spoken, ["Hello."])

  def test_fallback_is_reused_after_the_first_failure(self):
    primary = FakePrimary(SpeechError("network down"))
    fallback = FakeFallback()
    factory = mock.Mock(return_value=fallback)
    voice = FallbackVoice(primary, factory)

    voice.speak("One.")
    voice.speak("Two.")

    factory.assert_called_once()
    self.assertEqual(fallback.spoken, ["One.", "Two."])

  def test_offline_mode_skips_the_network_engine(self):
    primary = FakePrimary()
    fallback = FakeFallback()
    voice = FallbackVoice(primary, lambda: fallback, offline_check=lambda: True)

    voice.speak("Hello.")

    self.assertEqual(primary.spoken, [])
    self.assertEqual(fallback.spoken, ["Hello."])

  def test_offline_check_is_dynamic(self):
    primary = FakePrimary()
    fallback = FakeFallback()
    settings = {"offline": False}
    voice = FallbackVoice(primary, lambda: fallback)
    voice.set_offline_check(lambda: settings["offline"])

    voice.speak("Online.")
    settings["offline"] = True
    voice.speak("Local.")

    self.assertEqual(primary.spoken, ["Online."])
    self.assertEqual(fallback.spoken, ["Local."])

  def test_interrupt_does_not_fall_back(self):
    primary = FakePrimary(SpeechError("interrupted"))
    fallback = FakeFallback()
    voice = FallbackVoice(primary, lambda: fallback)

    voice.speak("Hello.", should_stop=lambda: True)

    self.assertEqual(primary.spoken, [])
    self.assertEqual(fallback.spoken, [])

  def test_no_fallback_configured_raises(self):
    voice = FallbackVoice(FakePrimary(SpeechError("offline")), None)
    with self.assertRaises(SpeechError):
      voice.speak("Hello.")

  def test_close_closes_both_engines_once_created(self):
    primary = FakePrimary(SpeechError("offline"))
    fallback = FakeFallback()
    voice = FallbackVoice(primary, lambda: fallback)
    voice.speak("Hello.")

    voice.close()

    self.assertTrue(primary.closed)
    self.assertTrue(fallback.closed)


class MainSpeakerTests(unittest.TestCase):
  def test_sapi_engine_builds_a_single_local_voice(self):
    import main

    settings = dataclasses.replace(
        load_settings(),
        tts=dataclasses.replace(load_settings().tts, engine="sapi"),
    )
    with mock.patch.object(main, "WindowsVoice") as sapi:
      speaker = main.build_speaker(settings)
    self.assertIs(speaker, sapi.return_value)

  def test_edge_engine_builds_a_fallback_wrapper_without_sapi(self):
    import main

    settings = load_settings()
    with mock.patch("voice.edge_tts.EdgeVoice") as edge:
      speaker = main.build_speaker(settings)
    self.assertIsInstance(speaker, FallbackVoice)
    self.assertIs(speaker.primary, edge.return_value)
    self.assertIsNone(speaker._fallback)


class AssistantSpeechModeTests(unittest.TestCase):
  def test_offline_or_privacy_requires_local_speech(self):
    from core.assistant import Assistant

    assistant = object.__new__(Assistant)
    assistant._reliability_mode = mock.Mock(side_effect=[True, False])
    self.assertTrue(assistant.speech_offline())

    assistant._reliability_mode = mock.Mock(side_effect=[False, True])
    self.assertTrue(assistant.speech_offline())

    assistant._reliability_mode = mock.Mock(side_effect=[False, False])
    self.assertFalse(assistant.speech_offline())


if __name__ == "__main__":
  unittest.main()
