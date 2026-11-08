"""Offline voice interface: microphone, VAD, Vosk STT and SAPI speech."""

from voice.errors import AudioError, SpeechError, SttError, VoiceError

__all__ = ["AudioError", "SpeechError", "SttError", "VoiceError"]
