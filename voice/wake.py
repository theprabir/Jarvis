"""Passive wake-word detection over the existing local Vosk model.

The detector keeps a small grammar recognizer alive while JARVIS is idle. It
reuses the Vosk model already loaded for speech-to-text, so passive listening
does not add a second acoustic model. Audio is processed in memory and is not
saved or transmitted.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from voice.audio import FRAME_BYTES, SAMPLE_RATE
from voice.errors import AudioError

log = logging.getLogger(__name__)

RESET_FRAMES = 1200  # about 36 seconds of 30 ms frames


@dataclass(frozen=True)
class WakeDetection:
  """One accepted activation from the passive listener."""

  phrase: str
  text: str = ""
  confidence: float = 1.0
  at: float = 0.0


def normalize_words(text):
  """Lowercase and strip punctuation for tolerant wake matching."""
  cleaned = (text or "").lower()
  cleaned = "".join(
      character if character.isalnum() or character.isspace() else " "
      for character in cleaned
  )
  return " ".join(cleaned.split())


def matches_wake_word(text, phrase):
  """True when ``text`` ends with the configured phrase.

  Matching only the tail avoids treating an old partial transcript as a new
  activation. Grammar mode normally produces the phrase alone, while full
  recognition may add a little leading noise.
  """
  words = normalize_words(text)
  target = normalize_words(phrase)
  if not words or not target:
    return False
  return words == target or words.endswith(" " + target)


class VoskWakeDetector:
  """Grammar-constrained wake detector backed by a loaded Vosk model."""

  def __init__(
      self,
      transcriber,
      phrase="hey jarvis",
      cooldown_ms=2000,
      reset_frames=RESET_FRAMES,
  ):
    phrase = " ".join((phrase or "").split()).lower()
    if not phrase:
      raise ValueError("a wake phrase is required")
    self.phrase = phrase
    self.cooldown_s = max(0.0, float(cooldown_ms) / 1000.0)
    self.reset_frames = max(1, int(reset_frames))
    self._transcriber = transcriber
    self._model = transcriber.model
    self._recognizer_class = transcriber.recognizer_class
    self._last_hit = 0.0
    self._frames = 0
    self._recognizer = None
    # Include the bare final word so "Jarvis" still has a chance when the
    # small English model does not contain the full phrase as one token path.
    words = phrase.split()
    variants = [phrase]
    if len(words) > 1 and words[-1] not in variants:
      variants.append(words[-1])
    self._phrases = tuple(variants)
    self._grammar = json.dumps(variants + ["[unk]"])
    self._reset()

  def _reset(self):
    self._recognizer = self._recognizer_class(
        self._model, SAMPLE_RATE, self._grammar
    )
    self._frames = 0

  @property
  def ready(self):
    return self._recognizer is not None and self._model is not None

  def accept(self, pcm):
    """Feed one 30 ms frame and return a detection, if any."""
    if not self.ready or len(pcm) != FRAME_BYTES:
      return None
    self._frames += 1
    if self._frames >= self.reset_frames:
      # Kaldi/Vosk recognizers can degrade on an unbounded partial stream.
      self._reset()
    try:
      final = bool(self._recognizer.AcceptWaveform(bytes(pcm)))
      payload = json.loads(
          (self._recognizer.Result() if final else self._recognizer.PartialResult())
          or "{}"
      )
    except Exception:
      log.debug("Wake-word recognizer frame failed", exc_info=True)
      self._reset()
      return None

    text = payload.get("text") or payload.get("partial") or ""
    if final:
      self._reset()
    if not any(matches_wake_word(text, phrase) for phrase in self._phrases):
      return None

    now = time.monotonic()
    if now - self._last_hit < self.cooldown_s:
      return None
    self._last_hit = now
    self._reset()
    return WakeDetection(phrase=self.phrase, text=text, at=now)

  def reset(self):
    self._reset()

  def close(self):
    self._recognizer = None
    self._model = None


class WakeWordListener:
  """Daemon microphone loop for passive detection.

  The loop owns the microphone only while idle. ``pause`` aborts the open
  stream so the active VAD capture can open the same device; ``resume`` starts
  a fresh stream after the reply has been spoken.
  """

  def __init__(self, microphone, detector, on_detected):
    self.microphone = microphone
    self.detector = detector
    self._on_detected = on_detected
    self._stop = threading.Event()
    self._paused = threading.Event()
    self._resume = threading.Event()
    self._released = threading.Event()
    self._thread = None
    self._last_error = ""

  def start(self):
    """Start the daemon paused; call :meth:`resume` after the greeting."""
    if self._thread is not None and self._thread.is_alive():
      return
    self._stop.clear()
    self._paused.set()
    self._released.set()
    self._thread = threading.Thread(
        target=self._run, name="jarvis-wake", daemon=True
    )
    self._thread.start()

  def _run(self):
    while not self._stop.is_set():
      if self._paused.is_set():
        self._released.set()
        self._resume.wait(0.1)
        continue
      self._released.clear()
      try:
        for frame in self.microphone.frames(
            abort=lambda: self._stop.is_set() or self._paused.is_set()
        ):
          if self._stop.is_set() or self._paused.is_set():
            break
          detection = self.detector.accept(frame)
          if detection is not None:
            # Do not call pause() from this thread: pause waits for the stream
            # loop to release, which happens only after we break out below.
            self._paused.set()
            self._resume.clear()
            try:
              self._on_detected(detection)
            except Exception:
              log.exception("Wake-word callback failed")
            break
      except AudioError as exc:
        self._last_error = str(exc)
        log.warning("Wake-word microphone unavailable: %s", exc)
        self._stop.wait(1.0)
      finally:
        self._released.set()
    self._released.set()

  @property
  def last_error(self):
    return self._last_error

  @property
  def paused(self):
    return self._paused.is_set()

  @property
  def alive(self):
    return self._thread is not None and self._thread.is_alive()

  def pause(self, timeout=2.0):
    """Stop passive inference and wait for the microphone stream to close."""
    self._paused.set()
    self._resume.clear()
    return self._released.wait(timeout)

  def resume(self):
    if self._stop.is_set():
      return
    # A pause may have left a partial phrase in the recognizer. Clear it so
    # the next activation always starts from a clean acoustic state.
    try:
      self.detector.reset()
    except Exception:
      log.debug("Wake-word detector reset failed", exc_info=True)
    self._paused.clear()
    self._released.clear()
    self._resume.set()

  def stop(self):
    self._stop.set()
    self._paused.set()
    self._resume.set()
    if self._thread is not None:
      self._thread.join(timeout=2.0)
      self._thread = None
    try:
      self.detector.close()
    except Exception:
      log.debug("Wake-word detector close failed", exc_info=True)
