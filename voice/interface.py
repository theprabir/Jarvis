"""Voice conversation loop: push-to-talk, transcription, speech and text fallback.

One thread owns all interaction. A global hotkey and a console reader only
push flags or lines into that loop, so audio, SAPI and the LLM are never used
from two threads at once.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from voice.audio import Microphone, listen, resolve_input_device
from voice.errors import AudioError, SpeechError, SttError, VoiceError
from voice.stt import VoskTranscriber
from voice.tts import WindowsVoice
from voice.wake import VoskWakeDetector, WakeWordListener

log = logging.getLogger(__name__)

EXIT_COMMANDS = {"exit", "quit", "bye"}
ESCAPE_KEY = "esc"
ESCAPE_VK = 0x1B
POLL_S = 0.03


def _escape_was_pressed():
  """True when Escape was pressed since the previous check.

  The keyboard hook only fires when it can be installed; polling the key
  state works in every session, including an elevated console where the hook
  is silently ignored.
  """
  try:
    import ctypes

    state = ctypes.windll.user32.GetAsyncKeyState(ESCAPE_VK)
  except Exception:  # pragma: no cover - not on Windows
    return False
  return bool(state & 0x0001)


class VoiceInterface:
  """Interactive voice session bound to one microphone and one SAPI voice."""

  def __init__(self, settings, respond, tts=None):
    self.settings = settings
    self.voice_config = settings.voice
    self._respond = respond
    self._tts = tts
    self._events = queue.Queue()
    self._ptt = threading.Event()
    self._interrupt = threading.Event()
    self._mic = None
    self._stt = None
    self._wake_listener = None
    self._wake_enabled = bool(self.voice_config.wake_enabled)
    self._hotkeys = []
    self._running = False
    self._speaking = False
    self._listening = False
    self._stop_watch = threading.Event()

  # -- setup ------------------------------------------------------------

  def start(self):
    """Prepare audio, transcription and hotkeys; raises :class:`VoiceError`."""
    device = resolve_input_device(self.voice_config.input_device)
    self._mic = Microphone(device)
    self._mic.probe()

    self._stt = VoskTranscriber(self.voice_config.resolved_stt_model)
    if self._wake_enabled:
      self._ensure_wake_listener()
    self._install_hotkeys()
    threading.Thread(
        target=self._watch_escape, name="jarvis-escape", daemon=True
    ).start()
    threading.Thread(
        target=self._read_console, name="jarvis-console", daemon=True
    ).start()
    log.info("Voice interface ready on %s", self._mic.describe())

  def _install_hotkeys(self):
    try:
      import keyboard
    except ImportError:
      log.warning(
          "keyboard module missing; press Enter on an empty line to talk"
      )
      return
    try:
      self._hotkeys.append(
          keyboard.add_hotkey(self.voice_config.ptt_key, self._on_ptt)
      )
    except Exception as exc:
      log.warning(
          "Could not register the push-to-talk key %r: %s",
          self.voice_config.ptt_key,
          exc,
      )
    try:
      self._hotkeys.append(keyboard.add_hotkey(ESCAPE_KEY, self._on_escape))
    except Exception as exc:
      log.info("Escape hotkey unavailable (%s); polling instead", exc)

  def _on_ptt(self):
    self._ptt.set()

  def _on_wake(self, detection):
    self._events.put(("wake", detection))

  def _ensure_wake_listener(self):
    """Create the passive listener without making voice startup mandatory."""
    if self._stt is None or self._mic is None:
      return None
    if self._wake_listener is not None:
      return self._wake_listener
    try:
      detector = VoskWakeDetector(
          self._stt,
          phrase=self.voice_config.wake_phrase,
          cooldown_ms=self.voice_config.wake_cooldown_ms,
      )
      self._wake_listener = WakeWordListener(
          self._mic, detector, self._on_wake
      )
      self._wake_listener.start()
      log.info(
          "Wake-word listener ready for %r", self.voice_config.wake_phrase
      )
    except Exception as exc:
      log.warning("Wake-word listener unavailable: %s", exc)
      self._wake_listener = None
    return self._wake_listener

  def _pause_wake(self):
    if self._wake_listener is not None:
      self._wake_listener.pause()

  def _resume_wake(self):
    if self._wake_enabled and self._wake_listener is not None:
      self._wake_listener.resume()

  def _wake_command(self, text):
    parts = text.split(maxsplit=1)
    action = parts[1].strip().lower() if len(parts) > 1 else "status"
    if action in {"on", "enable", "start"}:
      self._wake_enabled = True
      if self._ensure_wake_listener() is None:
        return "Wake-word listening is unavailable, sir."
      self._resume_wake()
      return (
          f"Passive wake word enabled for {self.voice_config.wake_phrase!r}, sir."
      )
    if action in {"off", "disable", "stop"}:
      self._wake_enabled = False
      self._pause_wake()
      return "Passive wake word disabled, sir. Push-to-talk is still available."
    if action in {"status", "?", "help"}:
      if not self._wake_enabled:
        return "Passive wake word is off, sir."
      if self._wake_listener is None:
        return "Passive wake word is enabled but unavailable, sir."
      if self._wake_listener.last_error:
        return (
            "Passive wake word is enabled but the microphone failed: "
            + self._wake_listener.last_error
        )
      return (
          f"Passive wake word is listening for "
          f"{self.voice_config.wake_phrase!r}, sir."
      )
    return "Usage: /wake on|off|status"

  def _wake_ack(self):
    if self.voice_config.wake_ack == "none":
      return
    if self.voice_config.wake_ack == "voice":
      self._say("Yes, sir?")
      return
    try:
      import winsound

      winsound.Beep(880, 100)
    except Exception:
      log.debug("Wake-word chime unavailable", exc_info=True)

  def _on_escape(self):
    if self._speaking or self._listening:
      self._interrupt.set()

  def _watch_escape(self):
    """Turn a fresh Escape press into an interrupt, without a keyboard hook."""
    while not self._stop_watch.is_set():
      if _escape_was_pressed() and (self._speaking or self._listening):
        log.info("Escape pressed; stopping speech and audio input")
        self._interrupt.set()
      time.sleep(POLL_S)

  def _read_console(self):
    """Read typed input in a daemon thread; an empty line starts recording."""
    while True:
      try:
        line = input("You: ")
      except (EOFError, KeyboardInterrupt):
        self._events.put(("eof", None))
        return
      except Exception:
        log.debug("Console reader stopped", exc_info=True)
        self._events.put(("eof", None))
        return
      line = line.strip()
      if line:
        self._events.put(("text", line))
      else:
        self._on_ptt()

  # -- main loop --------------------------------------------------------

  def run(self):
    self._running = True
    print(
        "\n[Voice active] Press %s or Enter on an empty line to talk."
        % self.voice_config.ptt_key
    )
    print("[Voice active] Escape stops speech. Type a message or 'exit'.")
    if self._wake_enabled and self._wake_listener is not None:
      print(
          "[Wake word] Say %r while idle, or press Ctrl+Space.\n"
          % self.voice_config.wake_phrase
      )
    else:
      print("[Wake word] Passive listening is off; use push-to-talk.\n")
    # Do not start wake inference until the greeting has finished, otherwise
    # JARVIS can hear its own name from the speaker.
    self._say("Jarvis online, sir. Ready when you are.")
    self._resume_wake()
    try:
      while self._running:
        try:
          kind, payload = self._events.get(timeout=0.1)
        except queue.Empty:
          if self._ptt.is_set():
            self._pause_wake()
            try:
              if not self._handle_voice():
                break
            finally:
              self._resume_wake()
          continue

        if kind == "eof":
          break
        if kind == "wake":
          self._pause_wake()
          try:
            if not self._handle_voice(from_wake=True):
              break
          finally:
            self._resume_wake()
          continue
        if kind == "text":
          command = payload.lower()
          if command in EXIT_COMMANDS:
            self._say("Shutting down systems, sir. Goodbye.")
            break
          if command == "/stop":
            self._interrupt.clear()
            continue
          if command == "/voices":
            print("Available voices: " + "; ".join(self._voice_names()))
            continue
          self._pause_wake()
          try:
            if command.startswith("/wake"):
              response = self._wake_command(payload)
              # /wake on resumes the listener; keep it paused through the
              # spoken confirmation so it cannot hear JARVIS saying the phrase.
              self._pause_wake()
              self._say(response)
            else:
              self._say(self._respond(payload))
          finally:
            self._resume_wake()
    except KeyboardInterrupt:
      print("\nJarvis: Shutting down.")
    finally:
      self._close()
    return 0

  def _handle_voice(self, from_wake=False):
    """Record, transcribe and answer one spoken request."""
    self._ptt.clear()
    self._interrupt.clear()
    if from_wake:
      self._wake_ack()
    print("[Listening...]")
    try:
      self._listening = True
      utterance = listen(
          self._mic,
          self.voice_config,
          abort=self._interrupt.is_set,
          start_timeout_s=(
              self.voice_config.wake_arm_timeout_s if from_wake else 0.0
          ),
      )
    except AudioError as exc:
      log.error("Microphone failure: %s", exc)
      self._say("I could not access the microphone, sir.")
      return True
    finally:
      self._listening = False

    if not utterance.pcm:
      print("Jarvis: I didn't catch any speech.")
      return True

    try:
      text = self._stt.transcribe(utterance.pcm)
    except SttError as exc:
      log.error("Transcription failure: %s", exc)
      self._say("I could not transcribe that, sir.")
      return True

    if not text:
      print("Jarvis: I couldn't understand that.")
      return True

    print(f"You (voice): {text}")
    if text.lower().strip(" .!?") in EXIT_COMMANDS:
      self._say("Shutting down systems, sir. Goodbye.")
      return False

    self._say(self._respond(text))
    return True

  # -- output -----------------------------------------------------------

  def _say(self, text):
    print(f"Jarvis: {text}")
    if self._tts is None:
      return
    self._interrupt.clear()
    self._speaking = True
    try:
      self._tts.speak(
          text,
          should_stop=lambda: (
              self._interrupt.is_set()
              or self._ptt.is_set()
              or not self._events.empty()
          ),
      )
    except SpeechError as exc:
      log.error("Speech failure: %s", exc)
    finally:
      self._speaking = False

  def notify(self, text):
    """Speak an asynchronous timer/briefing without letting wake infer TTS."""
    self._pause_wake()
    try:
      self._say(text)
    finally:
      self._resume_wake()

  def _voice_names(self):
    return WindowsVoice.available_voices() or ["none"]

  def _close(self):
    self._running = False
    self._stop_watch.set()
    self._interrupt.set()
    if self._wake_listener is not None:
      self._wake_listener.stop()
      self._wake_listener = None
    for handle in self._hotkeys:
      try:
        import keyboard

        keyboard.remove_hotkey(handle)
      except Exception:
        log.debug("Could not remove hotkey", exc_info=True)
    self._hotkeys.clear()
    # The TTS object is owned by the caller (main), which may reuse it for
    # the text-mode fallback, so it must outlive this interface.
    if self._tts is not None:
      self._tts.stop()
    if self._stt is not None:
      self._stt.close()
      self._stt = None
    log.info("Voice interface stopped")


def build_voice(settings, respond, tts=None):
  """Create and start a voice interface, raising :class:`VoiceError` if unable."""
  interface = VoiceInterface(settings, respond, tts=tts)
  try:
    interface.start()
  except VoiceError:
    interface._close()
    raise
  return interface
