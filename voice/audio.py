"""Microphone capture, resampling and VAD-driven utterance segmentation.

Audio is always 16 kHz mono 16-bit PCM, the only format both WebRTC VAD and
Vosk accept. Frames are 30 ms (480 samples), the largest VAD frame size.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from voice.errors import AudioError

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2

SILENCE = "silence"
MAX_DURATION = "max_duration"
END_OF_STREAM = "end_of_stream"
NO_SPEECH = "no_speech"


@dataclass(frozen=True)
class Utterance:
  """One captured request, already resampled to 16 kHz mono."""

  pcm: bytes
  reason: str
  speech_ms: int = 0

  @property
  def duration_ms(self) -> int:
    return int(len(self.pcm) / (2 * SAMPLE_RATE) * 1000)


def resample_pcm16(data, source_rate, target_rate=SAMPLE_RATE):
  """Linearly resample mono int16 PCM to ``target_rate``."""
  if source_rate == target_rate:
    return bytes(data)
  samples = np.frombuffer(data, dtype=np.int16)
  if samples.size == 0:
    return b""
  count = max(1, int(round(samples.size * target_rate / source_rate)))
  positions = np.linspace(0, samples.size - 1, count)
  resampled = np.interp(positions, np.arange(samples.size), samples)
  return resampled.astype(np.int16).tobytes()


def input_devices(devices=None):
  """Return ``[(index, name)]`` for devices with at least one input channel."""
  devices = sd.query_devices() if devices is None else devices
  found = []
  for index, device in enumerate(devices):
    if device.get("max_input_channels", 0) > 0:
      found.append((index, device["name"]))
  return found


def resolve_input_device(query, devices=None):
  """Resolve a device name substring to an index; ``None`` means default."""
  if not query:
    return None
  wanted = query.lower()
  devices = sd.query_devices() if devices is None else devices
  for index, name in input_devices(devices):
    if wanted in name.lower():
      return index
  available = ", ".join(name for _, name in input_devices(devices))
  raise AudioError(
      f"No input device matches {query!r}. Available devices: {available}"
  )


class Segmenter:
  """Accumulate frames into one utterance using a speech predicate.

  Recording starts on the first speech frame, keeps a short pre-roll so the
  first syllable is not clipped, and ends after ``silence_ms`` of non-speech
  or ``max_record_s`` overall. Clicks shorter than ``min_speech_ms`` are
  discarded as noise.
  """

  def __init__(
      self,
      is_speech,
      frame_ms=FRAME_MS,
      silence_ms=900,
      min_speech_ms=250,
      max_record_s=15.0,
      preroll_ms=300,
  ):
    self.frame_ms = frame_ms
    self.silence_ms = silence_ms
    self.min_speech_ms = min_speech_ms
    self.max_record_ms = int(max_record_s * 1000)
    self._is_speech = is_speech
    self._preroll = deque(maxlen=max(1, preroll_ms // frame_ms))
    self._frames = []
    self._started = False
    self._speech_ms = 0
    self._silence_run_ms = 0
    self._result = None

  @property
  def result(self):
    return self._result

  @property
  def started(self):
    return self._started

  def feed(self, frame):
    """Add one frame; return the finished :class:`Utterance` or ``None``."""
    if self._result is not None:
      return self._result
    if not frame:
      return None

    speech = bool(self._is_speech(frame))
    if not self._started:
      if not speech:
        self._preroll.append(frame)
        return None
      self._started = True
      self._frames.extend(self._preroll)
      self._preroll.clear()
      self._frames.append(frame)
      self._speech_ms += self.frame_ms
      return None

    self._frames.append(frame)
    if speech:
      self._speech_ms += self.frame_ms
      self._silence_run_ms = 0
    else:
      self._silence_run_ms += self.frame_ms

    total_ms = len(self._frames) * self.frame_ms
    if self._silence_run_ms >= self.silence_ms:
      return self._finish(SILENCE)
    if total_ms >= self.max_record_ms:
      return self._finish(MAX_DURATION)
    return None

  def close(self):
    """Finish the utterance when the audio source ends."""
    if self._result is not None:
      return self._result
    if not self._started:
      return self._finish(NO_SPEECH)
    return self._finish(END_OF_STREAM)

  def _finish(self, reason):
    if self._speech_ms < self.min_speech_ms:
      pcm = b""
      reason = NO_SPEECH
    else:
      pcm = b"".join(self._frames)
    self._result = Utterance(pcm=pcm, reason=reason, speech_ms=self._speech_ms)
    self._frames = []
    self._preroll.clear()
    return self._result


def _make_vad(aggressiveness):
  try:
    import webrtcvad
  except ImportError as exc:  # pragma: no cover - dependency is in requirements
    raise AudioError("WebRTC VAD is not installed (webrtcvad-wheels)") from exc
  return webrtcvad.Vad(int(aggressiveness))


class Microphone:
  """Blocking microphone reader that yields 30 ms 16 kHz frames."""

  def __init__(self, device=None):
    self.device = device
    self.rate = SAMPLE_RATE

  def describe(self):
    """Return a human-readable name for the selected microphone."""
    try:
      info = sd.query_devices(self.device, "input")
      return str(info["name"])
    except sd.PortAudioError:
      return "default input device"

  def probe(self):
    """Open and close the stream once so startup fails fast and clearly."""
    try:
      stream, rate, _ = self._open()
      self.rate = rate
      log.info("Microphone opened at %s Hz", rate)
      stream.stop()
      stream.close()
    except sd.PortAudioError as exc:
      raise AudioError(f"Could not open the microphone: {exc}") from exc

  def _open(self):
    try:
      stream = sd.RawInputStream(
          samplerate=SAMPLE_RATE,
          blocksize=FRAME_SAMPLES,
          channels=1,
          dtype="int16",
          device=self.device,
      )
      stream.start()
      return stream, SAMPLE_RATE, FRAME_SAMPLES
    except sd.PortAudioError:
      info = sd.query_devices(self.device, "input")
      rate = int(info["default_samplerate"] or SAMPLE_RATE)
      if rate == SAMPLE_RATE:
        raise
      block = max(1, int(rate * FRAME_MS / 1000))
      stream = sd.RawInputStream(
          samplerate=rate,
          blocksize=block,
          channels=1,
          dtype="int16",
          device=self.device,
      )
      stream.start()
      log.info(
          "Microphone runs at %s Hz; resampling to %s Hz", rate, SAMPLE_RATE
      )
      self.rate = rate
      return stream, rate, block

  def frames(self, abort=None):
    """Yield frames until ``abort()`` is true or the caller stops reading."""
    try:
      stream, rate, block = self._open()
    except sd.PortAudioError as exc:
      raise AudioError(f"Could not open the microphone: {exc}") from exc

    try:
      while abort is None or not abort():
        data, overflowed = stream.read(block)
        if overflowed:
          log.debug("Microphone input overflowed; a frame was dropped")
        pcm = resample_pcm16(data, rate)
        if pcm:
          yield pcm
    finally:
      try:
        stream.stop()
        stream.close()
      except sd.PortAudioError:
        log.debug("Microphone close failed", exc_info=True)


def listen(microphone, config, abort=None, start_timeout_s=0.0):
  """Record one utterance from ``microphone`` according to ``config``.

  ``start_timeout_s`` lets a wake-word activation return to passive mode when
  the user does not begin speaking. Normal push-to-talk leaves it at zero.
  """
  vad = _make_vad(config.vad_aggressiveness)
  segmenter = Segmenter(
      lambda frame: vad.is_speech(frame, SAMPLE_RATE),
      silence_ms=config.silence_ms,
      min_speech_ms=config.min_speech_ms,
      max_record_s=config.max_record_s,
  )
  started_at = time.monotonic()
  for frame in microphone.frames(abort):
    if len(frame) != FRAME_BYTES:
      continue
    if (
        start_timeout_s
        and not segmenter.started
        and time.monotonic() - started_at >= start_timeout_s
    ):
      return segmenter.close()
    utterance = segmenter.feed(frame)
    if utterance is not None:
      return utterance
  return segmenter.close()
