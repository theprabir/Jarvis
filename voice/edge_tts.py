"""Natural speech through the online Microsoft Edge TTS service.

Edge TTS gives JARVIS a much more natural voice than SAPI without shipping a
large local model. Synthesis is network-backed, so the caller must provide a
local fallback for offline and privacy modes.

Audio is synthesized one sentence at a time. That keeps the first spoken words
quick and bounds the amount of audio held in memory; playback is checked
between chunks so Escape and push-to-talk still interrupt speech.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from voice.errors import SpeechError
from voice.tts import clean_for_speech

log = logging.getLogger(__name__)

MAX_SENTENCE_CHARS = 240
DEFAULT_SAMPLE_RATE = 24_000


def split_sentences(text, max_chars=MAX_SENTENCE_CHARS):
  """Split cleaned text into speakable pieces without losing words."""
  if not text:
    return []
  parts = [
      part.strip()
      for part in re.split(r"(?<=[.!?])\s+", text)
      if part.strip()
  ]
  pieces = []
  for part in parts:
    while len(part) > max_chars:
      cut = part.rfind(" ", 0, max_chars)
      if cut <= 0:
        cut = max_chars
      pieces.append(part[:cut].strip())
      part = part[cut:].strip()
    if part:
      pieces.append(part)
  return pieces


class EdgeVoice:
  """Interruptible online speech using ``edge-tts`` and ``miniaudio``."""

  def __init__(self, config):
    self._config = config
    self.voice = config.edge_voice
    self.rate = config.edge_rate
    self.pitch = config.edge_pitch
    self.volume = max(0.0, min(1.0, float(config.volume)))
    self.description = f"Microsoft Edge ({self.voice})"

  def speak(self, text, should_stop=None):
    """Speak ``text`` in sentence chunks, stopping when requested."""
    spoken = clean_for_speech(text)
    if not spoken:
      return
    for sentence in split_sentences(spoken):
      if should_stop is not None and should_stop():
        return
      audio = self._synthesize(sentence, should_stop)
      if not audio:
        if should_stop is not None and should_stop():
          return
        raise SpeechError("Edge TTS returned no audio")
      samples = self._decode(audio)
      if self._play(samples, should_stop):
        return

  def _synthesize(self, text, should_stop=None):
    """Return MP3 bytes for one sentence, or ``b""`` when interrupted."""
    try:
      import edge_tts
    except ImportError as exc:
      raise SpeechError(
          "edge-tts is not installed; install the voice requirements"
      ) from exc

    async def collect():
      audio = bytearray()
      try:
        communicate = edge_tts.Communicate(
            text,
            voice=self.voice,
            rate=self.rate,
            pitch=self.pitch,
        )
        async for chunk in communicate.stream():
          if should_stop is not None and should_stop():
            return b""
          if chunk.get("type") == "audio":
            audio.extend(chunk.get("data", b""))
      except Exception as exc:
        raise SpeechError(f"Edge TTS synthesis failed: {exc}") from exc
      return bytes(audio)

    try:
      return asyncio.run(
          asyncio.wait_for(collect(), timeout=self._config.edge_timeout_s)
      )
    except asyncio.TimeoutError as exc:
      raise SpeechError("Edge TTS timed out") from exc
    except SpeechError:
      raise
    except Exception as exc:
      raise SpeechError(f"Edge TTS synthesis failed: {exc}") from exc

  def _decode(self, mp3):
    """Decode Edge's MP3 output to normalized mono float samples."""
    try:
      import miniaudio
      import numpy as np
    except ImportError as exc:
      raise SpeechError(
          "miniaudio/numpy are not installed; install the voice requirements"
      ) from exc

    try:
      decoded = miniaudio.decode(
          mp3,
          nchannels=1,
          sample_rate=DEFAULT_SAMPLE_RATE,
          output_format=miniaudio.SampleFormat.SIGNED16,
      )
      samples = np.frombuffer(decoded.samples, dtype=np.int16).astype(
          np.float32
      )
      samples /= 32768.0
      if self.volume != 1.0:
        samples *= self.volume
      return np.clip(samples, -1.0, 1.0), decoded.sample_rate
    except Exception as exc:
      raise SpeechError(f"Edge TTS audio decode failed: {exc}") from exc

  def _play(self, decoded, should_stop=None):
    """Play decoded audio; return True when playback was interrupted."""
    samples, sample_rate = decoded
    if should_stop is not None and should_stop():
      return True
    try:
      import sounddevice as sd
    except ImportError as exc:
      raise SpeechError("sounddevice is not installed") from exc

    try:
      sd.play(samples, sample_rate, blocking=False)
      while True:
        stream = sd.get_stream()
        if not stream.active:
          break
        if should_stop is not None and should_stop():
          sd.stop()
          log.debug("Edge TTS playback interrupted by the user")
          return True
        time.sleep(0.05)
    except Exception as exc:
      try:
        sd.stop()
      except Exception:
        pass
      raise SpeechError(f"Edge TTS playback failed: {exc}") from exc
    return False

  def stop(self):
    try:
      import sounddevice as sd

      sd.stop()
    except Exception:
      log.debug("Could not stop Edge TTS playback", exc_info=True)

  def close(self):
    self.stop()

  def available_voices(self):
    return [f"Edge: {self.voice}"]
