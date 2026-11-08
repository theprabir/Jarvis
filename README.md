# JARVIS

A local-first, voice-and-text personal AI assistant for Windows 11, built
around a small local LLM (Llama 3.2 1B Instruct Q4_K_M) served by the existing
`llama-server.exe` from llama.cpp. The model is the language interface;
deterministic Python modules do the audio, routing and tool work.

**Current status:** Phase 1 (stabilization), feature set **4.1 Voice
Interface** and feature set **4.2 Conversation Intelligence** complete. JARVIS
listens through the microphone with push-to-talk, transcribes offline with
Vosk, answers with the local model using multi-turn context, and speaks through
an interruptible Windows SAPI voice. Tools and skills follow in feature sets
4.3+ (see `PLANS-STACK-FEATURES.md`, `CHANGELOG.md`).

## Requirements

- Windows 11, 64-bit Python (3.11+)
- `llama-server.exe` on `PATH` (existing llama.cpp installation; do **not**
  replace it with `llama-cpp-python`)
- `models/Llama-3.2-1B-Instruct-Q4_K_M.gguf`
- A microphone and speakers; speech output needs a Windows SAPI voice
  (Microsoft David or Zira ship with Windows)
- 8 GB RAM target machine. Measured: ~0.95 GB total with the LLM loaded, and
  ~195 MB for the Python voice process (Vosk model, SAPI, audio stack)

## Installation

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Download the offline speech model once (41 MB, extracted to
`models/vosk-model-small-en-us-0.15`):

```bat
curl -L -o models\vosk-model-small-en-us-0.15.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
powershell -Command "Expand-Archive -Path models\vosk-model-small-en-us-0.15.zip -DestinationPath models"
```

If the model directory is missing, JARVIS logs a warning and starts in text
mode instead of failing.

## Configuration

All runtime settings live in `config/settings.toml`:

| Section | Purpose |
| --- | --- |
| `[app]` | Log level and log directory |
| `[llama]` | Executable, model path, host/port, context, threads, batch sizes, slots, startup timeout |
| `[llm]` | Connect/read timeouts, max tokens, temperature, retries |
| `[tts]` | Speech flag, rate (words/minute), volume, voice name substring |
| `[voice]` | Voice mode flag, microphone, push-to-talk key, VAD, silence/record limits, Vosk model path |
| `[conversation]` | Multi-turn context, answer mode, token limits, summarization, history database |

Unknown keys are rejected so typos fail fast. Override the file location with
`--config PATH` or the `JARVIS_CONFIG` environment variable.

Voice defaults: `ctrl+space` push-to-talk, VAD aggressiveness 2, a request ends
after 900 ms of silence, clicks shorter than 250 ms are discarded, recording is
capped at 15 s. `input_device = ""` means the system default microphone; set it
to a name substring (for example `"USB"`) to pick another device. `ptt_key`
takes any `keyboard` hotkey string.

### Conversation behaviour

Every request carries the recent exchanges (up to `max_turns`), so follow-ups,
pronouns and references resolve against what was already said. Older exchanges
are compressed into a short summary instead of being dropped; if that summary
request fails, an extractive summary is used. Only `max_context_chars` of
transcript is sent, which keeps requests inside the 2048-token context.

Answer styles are set by `mode`, or at runtime with `/mode`:

| Mode | Reply limit | Style |
| --- | --- | --- |
| `short` | 50 tokens | Two short sentences (default) |
| `detailed` | 120 tokens | Complete answer with a short conclusion |
| `technical` | 160 tokens | Precise terminology and mechanisms |

Completed exchanges are written to `history_db` (`data/jarvis.db`, table
`conversations`). A short request that only refers to something ("what about
that?") without any history asks for clarification instead of guessing, and a
model failure produces a spoken sentence without being stored as an answer.

The server is launched with:

```text
llama-server -m models/...gguf --host 127.0.0.1 --port 8080 \
  -c 2048 -t 2 -tb 2 -b 64 -ub 64 -np 1 -ngl 0
```

`-np 1` matters: llama.cpp otherwise defaults to several parallel slots, which
splits the 2048-token context and leaves each request far less room.

## Run

```bat
.venv\Scripts\python main.py
```

Voice mode is the default. JARVIS prints a short greeting, then waits:

- Press **Ctrl+Space** (or Enter on an empty console line) and speak. Audio is
  captured until you pause, transcribed offline, answered by the local model
  and spoken aloud.
- **Escape** stops speech immediately; pressing push-to-talk while JARVIS talks
  also cuts it off and starts listening.
- You can type at the `You:` prompt at any time; typed and spoken requests share
  the same conversation state.
- `/stop` clears a pending interrupt and `/voices` lists installed SAPI voices.
- `/new` starts a fresh conversation, `/mode short|detailed|technical` changes
  how much JARVIS says, `/history` shows recent exchanges and `/help` lists the
  commands.
- `exit`, `quit`, `bye` (typed or spoken) shut down; Ctrl-C also exits cleanly.

Flags:

| Flag | Effect |
| --- | --- |
| `--voice` | Force voice mode even if disabled in the config |
| `--text` | Disable the microphone; keyboard-only session |
| `--no-tts` | Keep listening but mute speech output |
| `--config PATH` | Use an alternative settings file |

The assistant reuses a healthy server on the configured port or starts one,
resolves the model path, waits for real `/health` readiness, and writes server
output to `logs/llama-server.log`. On any voice failure (missing model, busy or
absent microphone, SAPI error) it logs the reason and falls back to text mode
instead of crashing.

## Benchmark

```bat
.venv\Scripts\python -m core.benchmark
```

Reports working set for Python and `llama-server`, system available RAM, model
startup time, prefill and generation speed, and total latency.

## Tests

```bat
.venv\Scripts\python -m unittest discover -s tests -v
```

The suite runs offline and needs no microphone or speaker: configuration,
conversation context, context trimming, summarization, answer modes, commands,
clarification, SQLite history, VAD segmentation, resampling, device resolution,
speech text cleanup and SAPI rate mapping. The Vosk transcription test is
skipped when the model is absent.

## Project layout

```text
main.py                 Bootstrap: config, server, voice or text loop
config/settings.toml    Runtime configuration
core/config.py          Typed, validated TOML loader
core/logger.py          Console + rotating file logging
core/server_manager.py  llama-server lifecycle and readiness
core/brain.py           Single-shot prompt -> spoken-safe reply
core/assistant.py       Conversation-aware replies, modes, history, commands
core/conversation.py    Turns, compression summary and answer modes
core/history.py         SQLite conversation history
core/benchmark.py       RAM and latency baseline
brain/llm_client.py     Persistent HTTPX client
brain/output_parser.py  Structured chat completion parsing
brain/errors.py         LLM error hierarchy
voice/audio.py          Microphone capture, resampling, VAD segmentation
voice/stt.py            Vosk offline transcription
voice/tts.py            Interruptible Windows SAPI speech
voice/interface.py      Push-to-talk loop, barge-in, text fallback
voice/errors.py         Voice error hierarchy
core/tools.py           Legacy helpers, pending Phase 4 rebuild
models/                 GGUF model and Vosk model
data/                   SQLite conversation history (git-ignored)
logs/                   Runtime logs (git-ignored)
```

## Known limitations

- Conversation context lives in memory for the current session: a restart keeps
  the saved history rows but does not reload earlier turns into the prompt yet
  (long-term memory arrives in feature set 4.6).
- Summarization costs one extra model call once the transcript grows past
  `summarize_after`, which adds a few seconds on this CPU.
- No tool registry, calculator, filesystem or system tools yet (feature set 4.3+).
- Speech recognition uses the small English Vosk model: English only, and
  noisy rooms or strong accents reduce accuracy. Raise `vad_aggressiveness`
  or set a specific `input_device` if the microphone picks up background noise.
- Web search in `core/tools.py` is the legacy scraper; it returns unreliable
  results and is no longer used by `main.py`. It is rebuilt in Phase 4.
- `max_tokens` is 50: measured generation speed on this CPU is ~3.7-4.2 tok/s,
  so longer answers are impractically slow.
- Speech output is verified functionally (SAPI speaks, interruption at ~0.8 s),
  but audio quality and volume must be confirmed by ear on the target machine.
