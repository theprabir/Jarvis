"""JARVIS entry point: bootstrap, CLI loop and speech output.

Phase 1 responsibilities only: load configuration, start or reuse the local
llama-server, speak replies, and shut everything down cleanly. Intent
routing, tools and voice input arrive in later phases.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys

import pyttsx3

from brain.llm_client import close_client
from core.brain import ask_brain
from core.config import ConfigError, load_settings
from core.logger import setup_logging
from core.server_manager import LlamaServer, ServerError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Jarvis, a helpful personal AI assistant. Answer the user's"
    " questions directly and accurately. Never refuse a safe, normal factual"
    " question. Keep answers under 2 sentences."
)

EXIT_COMMANDS = {"exit", "quit", "bye"}


def speak(text, tts_config):
  """Print and speak the given text using Windows SAPI5."""
  print(f"Jarvis: {text}")
  if not tts_config.enabled:
    return
  try:
    engine = pyttsx3.init("sapi5")
    engine.setProperty("rate", tts_config.rate)
    engine.setProperty("volume", tts_config.volume)
    if tts_config.voice:
      engine.setProperty("voice", tts_config.voice)
    engine.say(text)
    engine.runAndWait()
    engine.stop()
  except Exception as exc:  # SAPI/COM failures must not kill the assistant
    log.error("Text-to-speech failed: %s", exc)


def run_cli(settings):
  server = LlamaServer(settings.llama, settings.app.log_dir)
  try:
    server.ensure_running()
  except ServerError as exc:
    log.error("LLM server unavailable: %s", exc)
    print(f"[Error] {exc}")
    return 1

  tts_config = settings.tts
  speak("Jarvis online, sir. Systems operational.", tts_config)
  print("\n[Jarvis CLI Active] Type your commands below (Type 'exit' to quit).\n")

  try:
    while True:
      try:
        user_input = input("You: ").strip()
      except EOFError:
        break
      if not user_input:
        continue

      if user_input.lower() in EXIT_COMMANDS:
        speak("Shutting down systems, sir. Goodbye.", tts_config)
        break

      reply = ask_brain(user_input, SYSTEM_PROMPT)
      speak(reply, tts_config)
      print("-" * 50)
  except KeyboardInterrupt:
    print("\nJarvis: Shutting down.")
  finally:
    close_client()
    server.stop()
  return 0


def main(argv=None):
  parser = argparse.ArgumentParser(description="JARVIS voice assistant")
  parser.add_argument("--config", default=None, help="path to settings.toml")
  parser.add_argument(
      "--no-tts", action="store_true", help="text only, no speech output"
  )
  args = parser.parse_args(argv)

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

  return run_cli(settings)


if __name__ == "__main__":
  sys.exit(main())
