"""Error types raised by the voice subsystem."""

from __future__ import annotations


class VoiceError(Exception):
  """Base class for recoverable voice interface failures."""


class AudioError(VoiceError):
  """The microphone could not be opened or is unusable."""


class SttError(VoiceError):
  """Speech-to-text model missing or transcription failed."""


class SpeechError(VoiceError):
  """Windows SAPI speech synthesis failed."""
