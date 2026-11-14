"""JARVIS entry point: bootstrap, conversation loop and speech.

Phase 1 delivered configuration, the llama-server lifecycle and the text
CLI. Feature set 4.1 adds an offline voice interface: push-to-talk VAD
capture, Vosk transcription and interruptible SAPI speech.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys

from brain.llm_client import close_client
from core.brain import ask_brain
from core.config import ConfigError, load_settings
from core.logger import setup_logging
from core.assistant import Assistant
from core.server_manager import LlamaServer, ServerError
from voice.errors import SpeechError, VoiceError
from voice.tts import FallbackVoice, WindowsVoice

log = logging.getLogger(__name__)

EXIT_COMMANDS = {"exit", "quit", "bye"}


def build_speaker(settings):
  """Create the configured speech engine, or None when speech is disabled."""
  if not settings.tts.enabled:
    return None
  if settings.tts.engine == "sapi":
    try:
      speaker = WindowsVoice(settings.tts)
    except SpeechError as exc:
      log.error("Speech output unavailable: %s", exc)
      print(f"[Warning] Speech output disabled: {exc}")
      return None
    log.info("Speech output ready: %s", speaker.description or "default voice")
    return speaker

  try:
    from voice.edge_tts import EdgeVoice

    primary = EdgeVoice(settings.tts)
  except Exception as exc:
    log.error("Natural speech unavailable: %s", exc)
    print(f"[Warning] Natural speech unavailable: {exc}")
    return None

  fallback_factory = None
  if settings.tts.fallback_engine == "sapi":
    fallback_factory = lambda: WindowsVoice(settings.tts)
  speaker = FallbackVoice(primary, fallback_factory)
  log.info(
      "Speech output ready: %s (local fallback: %s)",
      speaker.description,
      "SAPI" if fallback_factory is not None else "none",
  )
  return speaker


def speak(text, speaker):
  """Print and speak a reply; speech errors never end the session."""
  print(f"Jarvis: {text}")
  if speaker is None:
    return
  try:
    speaker.speak(text)
  except SpeechError as exc:
    log.error("Text-to-speech failed: %s", exc)


def run_cli(settings, speaker, assistant):
  """Typed conversation loop; also the fallback when voice mode is off."""
  print("\n[Jarvis CLI Active] Type your message below (Type 'exit' to quit).\n")
  speak("Jarvis online, sir. Systems operational.", speaker)
  try:
    while True:
      try:
        user_input = input("You: ").strip()
      except EOFError:
        break
      if not user_input:
        continue
      if user_input.lower() in EXIT_COMMANDS:
        speak("Shutting down systems, sir. Goodbye.", speaker)
        break
      speak(assistant.reply(user_input), speaker)
      print("-" * 50)
  except KeyboardInterrupt:
    print("\nJarvis: Shutting down.")
  return 0


def run(settings):
  server = LlamaServer(settings.llama, settings.app.log_dir)
  try:
    server.ensure_running()
  except ServerError as exc:
    log.error("LLM server unavailable: %s", exc)
    print(f"[Error] {exc}")
    return 1

  speaker = build_speaker(settings)
  assistant = Assistant(settings)
  # Offline and privacy modes must never send spoken text to Edge.
  if speaker is not None and hasattr(speaker, "set_offline_check"):
    speaker.set_offline_check(assistant.speech_offline)
  assistant.set_notifier(lambda text: speak(text, speaker))
  try:
    if settings.voice.enabled:
      # Imported here so text-only sessions never load numpy/sounddevice/vosk.
      from voice.interface import build_voice

      try:
        interface = build_voice(settings, assistant.reply, tts=speaker)
      except VoiceError as exc:
        log.warning("Voice interface unavailable: %s", exc)
        print(f"[Warning] Voice unavailable ({exc}); using text mode.")
      else:
        # Timer and proactive notifications must pause passive listening too.
        assistant.set_notifier(interface.notify)
        return interface.run()
    return run_cli(settings, speaker, assistant)
  except KeyboardInterrupt:
    print("\nJarvis: Shutting down.")
    return 0
  finally:
    close_client()
    server.stop()
    assistant.close()
    if speaker is not None:
      speaker.close()


def main(argv=None):
  parser = argparse.ArgumentParser(description="JARVIS voice assistant")
  parser.add_argument("--config", default=None, help="path to settings.toml")
  parser.add_argument(
      "--voice", action="store_true", help="force voice mode on"
  )
  parser.add_argument(
      "--text", action="store_true", help="disable the microphone, text only"
  )
  parser.add_argument(
      "--no-tts", action="store_true", help="keep listening, mute speech output"
  )
  args = parser.parse_args(argv)

  if args.voice and args.text:
    print("[Error] --voice and --text are mutually exclusive")
    return 2

  try:
    settings = load_settings(args.config)
  except ConfigError as exc:
    print(f"[Error] {exc}")
    return 2

  setup_logging(settings.app.log_level, settings.app.log_dir)
  log.info("Starting %s", settings.app.name)

  if args.no_tts and settings.tts.enabled:
    settings = dataclasses.replace(
        settings, tts=dataclasses.replace(settings.tts, enabled=False)
    )
  if (args.voice or args.text) and settings.voice.enabled != args.voice:
    settings = dataclasses.replace(
        settings,
        voice=dataclasses.replace(settings.voice, enabled=args.voice),
    )

  return run(settings)


if __name__ == "__main__":
  sys.exit(main())
