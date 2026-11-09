"""Offline speech-to-text with Vosk.

The small English model is loaded once and kept in memory; recognizers are
created per utterance because they are cheap and hold per-request state.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from voice.audio import SAMPLE_RATE
from voice.errors import SttError

log = logging.getLogger(__name__)


def normalize_transcript(text):
  """Collapse whitespace and strip control characters from Vosk output."""
  if not text:
    return ""
  cleaned = re.sub(r"\s+", " ", str(text)).strip()
  return cleaned


def _model_size(path):
  try:
    return sum(
        item.stat().st_size for item in path.rglob("*") if item.is_file()
    )
  except OSError:
    return 0


def resolve_model(path):
  """Return the configured model, or the best sibling Vosk model available.

  Accuracy is limited by the model, so when a better model has been
  downloaded into ``models/`` it is used instead of failing on a stale path.
  """
  if path.is_dir():
    return path
  candidates = [
      entry for entry in path.parent.glob("vosk-model-*") if entry.is_dir()
  ]
  if not candidates:
    raise SttError(f"Vosk model not found: {path}")
  best = max(candidates, key=_model_size)
  log.warning("Vosk model %s missing; using %s", path.name, best.name)
  return best


class VoskTranscriber:
  """Transcribe 16 kHz mono int16 PCM with a local Vosk model."""

  def __init__(self, model_path):
    path = resolve_model(Path(model_path))
    try:
      from vosk import KaldiRecognizer, Model, SetLogLevel
    except ImportError as exc:
      raise SttError("vosk is not installed; run pip install -r requirements.txt") from exc

    SetLogLevel(-1)
    try:
      self._model = Model(str(path))
    except Exception as exc:
      raise SttError(f"Could not load the Vosk model at {path}: {exc}") from exc
    self._recognizer_class = KaldiRecognizer
    log.info("Vosk model loaded: %s", path.name)

  def transcribe(self, pcm, sample_rate=SAMPLE_RATE):
    """Return the recognized text, or ``""`` when nothing intelligible was said."""
    if not pcm:
      return ""
    recognizer = self._recognizer_class(self._model, sample_rate)
    try:
      recognizer.AcceptWaveform(bytes(pcm))
      payload = json.loads(recognizer.FinalResult() or "{}")
    except Exception as exc:
      raise SttError(f"Transcription failed: {exc}") from exc
    return normalize_transcript(payload.get("text", ""))

  def close(self):
    self._model = None
