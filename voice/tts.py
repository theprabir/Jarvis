"""Speech output engines and fallback handling.

Natural speech is provided by :mod:`voice.edge_tts`; Windows SAPI remains the
local fallback and the only engine used in offline/privacy modes.

``pyttsx3`` starts a new engine for every utterance (~3.5 s on this machine)
and cannot cancel playback cleanly. Talking to ``SAPI.SpVoice`` directly
keeps one voice object alive: speech starts in a few milliseconds and can be
purged mid-sentence for barge-in. SAPI is created lazily by
:class:`FallbackVoice`, so the normal Edge path does not pay its memory cost.
"""

from __future__ import annotations

import logging
import re
import threading

from voice.errors import SpeechError

log = logging.getLogger(__name__)

SVSF_ASYNC = 1
SVSF_PURGE = 2
POLL_MS = 50

# SAPI rate runs from -10 (slowest) to 10 (fastest); 0 is normal.
RATE_REFERENCE = 175
RATE_STEP = 20


def clean_for_speech(text):
  """Remove markup that SAPI would otherwise read aloud literally."""
  if not text:
    return ""
  cleaned = re.sub(r"```.*?```", " ", str(text), flags=re.S)
  cleaned = re.sub(r"https?://\S+", " link ", cleaned)
  cleaned = re.sub(r"[*_`#>|]+", " ", cleaned)
  cleaned = re.sub(r"^\s*[-+]\s+", "", cleaned, flags=re.M)
  return re.sub(r"\s+", " ", cleaned).strip()


def _sapi_rate(words_per_minute):
  steps = round((int(words_per_minute) - RATE_REFERENCE) / RATE_STEP)
  return max(-10, min(10, steps))


class WindowsVoice:
  """A single persistent SAPI voice usable for blocking or cancellable speech."""

  def __init__(self, config):
    try:
      import comtypes
      import comtypes.client
    except ImportError as exc:
      raise SpeechError("comtypes is not installed") from exc

    try:
      comtypes.CoInitialize()
    except Exception:  # already initialized on this thread
      pass

    try:
      self._voice = comtypes.client.CreateObject("SAPI.SpVoice")
    except Exception as exc:
      raise SpeechError(f"Windows SAPI is unavailable: {exc}") from exc

    self._voice.Rate = _sapi_rate(config.rate)
    self._voice.Volume = max(0, min(100, int(round(config.volume * 100))))
    self.description = self._select_voice(config.voice)

  def _select_voice(self, wanted):
    voices = self._voice.GetVoices()
    descriptions = [voices.Item(i).GetDescription() for i in range(voices.Count)]
    if not wanted:
      return self._current_description()

    for index, description in enumerate(descriptions):
      if wanted.lower() in description.lower():
        self._voice.Voice = voices.Item(index)
        return description
    log.warning(
        "SAPI voice %r not found; using %r. Available: %s",
        wanted,
        self._current_description(),
        ", ".join(descriptions),
    )
    return self._current_description()

  def _current_description(self):
    try:
      return self._voice.Voice.GetDescription()
    except Exception:
      return ""

  @staticmethod
  def available_voices():
    """Return installed SAPI voice descriptions (empty list on failure)."""
    try:
      import comtypes
      import comtypes.client

      try:
        comtypes.CoInitialize()
      except Exception:
        pass
      voice = comtypes.client.CreateObject("SAPI.SpVoice")
      tokens = voice.GetVoices()
      return [tokens.Item(i).GetDescription() for i in range(tokens.Count)]
    except Exception:
      log.debug("Could not enumerate SAPI voices", exc_info=True)
      return []

  def speak(self, text, should_stop=None):
    """Speak ``text``, returning early when ``should_stop()`` turns true."""
    spoken = clean_for_speech(text)
    if not spoken:
      return
    try:
      self._voice.Speak(spoken, SVSF_ASYNC)
      while not self._voice.WaitUntilDone(POLL_MS):
        if should_stop is not None and should_stop():
          self._voice.Speak("", SVSF_PURGE)
          log.debug("Speech interrupted by the user")
          break
    except Exception as exc:
      raise SpeechError(f"Speech playback failed: {exc}") from exc

  def stop(self):
    if self._voice is None:
      return
    try:
      self._voice.Speak("", SVSF_PURGE)
    except Exception:
      log.debug("Could not stop speech", exc_info=True)

  def close(self):
    self.stop()
    self._voice = None


class FallbackVoice:
  """Use a primary speech engine with a lazy local fallback.

  SAPI is created only when Edge fails or when offline/privacy mode is active.
  That keeps the default natural-voice path from loading the extra SAPI
  libraries and voices at startup.
  """

  def __init__(self, primary, fallback_factory=None, offline_check=None):
    self.primary = primary
    self.description = getattr(primary, "description", "speech output")
    self._fallback_factory = fallback_factory
    self._fallback = None
    self._offline_check = offline_check
    self._lock = threading.Lock()

  def set_offline_check(self, check):
    """Set a callable that returns True when local speech is required."""
    self._offline_check = check

  def _local_required(self):
    if self._offline_check is None:
      return False
    try:
      return bool(self._offline_check())
    except Exception:
      log.debug("Could not read local-speech mode", exc_info=True)
      return False

  def _get_fallback(self):
    if self._fallback_factory is None:
      return None
    with self._lock:
      if self._fallback is None:
        self._fallback = self._fallback_factory()
      return self._fallback

  def speak(self, text, should_stop=None):
    """Speak with the primary engine, falling back once on failure."""
    if should_stop is not None and should_stop():
      return
    if not self._local_required():
      try:
        self.primary.speak(text, should_stop)
        return
      except Exception as exc:
        log.warning(
            "Natural speech unavailable (%s); falling back to local speech",
            exc,
        )
        if should_stop is not None and should_stop():
          return
    fallback = self._get_fallback()
    if fallback is None:
      raise SpeechError(
          "Natural speech is unavailable and no local fallback is configured"
      )
    fallback.speak(text, should_stop)

  def stop(self):
    try:
      self.primary.stop()
    except Exception:
      log.debug("Could not stop primary speech", exc_info=True)
    if self._fallback is not None:
      self._fallback.stop()

  def close(self):
    self.stop()
    if self._fallback is not None:
      self._fallback.close()
    try:
      self.primary.close()
    except Exception:
      log.debug("Could not close primary speech", exc_info=True)

  def available_voices(self):
    """List the configured Edge voice and installed SAPI fallback voices."""
    names = []
    getter = getattr(self.primary, "available_voices", None)
    if callable(getter):
      try:
        names.extend(getter())
      except Exception:
        log.debug("Could not list primary voices", exc_info=True)
    names.extend(f"SAPI: {name}" for name in WindowsVoice.available_voices())
    return names or ["none"]
