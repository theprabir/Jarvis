# JARVIS

A local-first, text-and-voice personal AI assistant for Windows 11, built around
a small local LLM (Llama 3.2 1B Instruct Q4_K_M) served by the existing
`llama-server.exe` from llama.cpp. The design keeps the model as the language
interface while deterministic Python modules do the actual work.

**Current status:** Phase 1 (existing system stabilization) complete. There is
no microphone input yet; the assistant is text-in / voice-out. See
`PROJECT_AUDIT.md` for the full assessment and `CHANGELOG.md` for history.

## Requirements

- Windows 11, 64-bit Python (3.11+)
- `llama-server.exe` on `PATH` (existing llama.cpp installation; do **not**
  replace it with `llama-cpp-python`)
- `models/Llama-3.2-1B-Instruct-Q4_K_M.gguf`
- 8 GB RAM target machine; measured assistant footprint ~0.95 GB

## Installation

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Configuration

All runtime settings live in `config/settings.toml`:

| Section | Purpose |
| --- | --- |
| `[app]` | Log level and log directory |
| `[llama]` | Executable, model path, host/port, context, threads, batch sizes, slots, startup timeout |
| `[llm]` | Connect/read timeouts, max tokens, temperature, retries |
| `[tts]` | Enabled flag, rate, volume, voice |

Unknown keys are rejected so typos fail fast. Override the file location with
`--config PATH` or the `JARVIS_CONFIG` environment variable.

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

The assistant reuses a healthy server on the configured port or starts one,
resolves the model path, waits for real `/health` readiness, and writes server
output to `logs/llama-server.log`. If the server cannot start, the last log
lines are shown.

Type commands at the `You:` prompt. `exit`, `quit` or `bye` shut down (and stop
the server this process started). Ctrl-C also shuts down cleanly.

Text-only mode (useful for testing without audio):

```bat
.venv\Scripts\python main.py --no-tts
```

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

## Project layout

```text
main.py                 CLI bootstrap: config, server, speech
config/settings.toml    Runtime configuration
core/config.py          Typed, validated TOML loader
core/logger.py          Console + rotating file logging
core/server_manager.py  llama-server lifecycle and readiness
core/brain.py           Single-shot prompt -> spoken-safe reply
core/benchmark.py       RAM and latency baseline
brain/llm_client.py     Persistent HTTPX client
brain/output_parser.py  Structured chat completion parsing
brain/errors.py         LLM error hierarchy
core/tools.py           Legacy helpers, pending Phase 4 rebuild
models/                 GGUF model
data/                   Local data (empty in Phase 1)
logs/                   Runtime logs (git-ignored)
```

## Known limitations

- No microphone, speech-to-text, VAD or push-to-talk yet (Phase 2).
- No conversation history: each typed line is an independent request (Phase 3).
- No tool registry, calculator, filesystem or system tools yet (Phase 3).
- Web search in `core/tools.py` is the legacy scraper; it returns unreliable
  results and is no longer used by `main.py`. It is rebuilt in Phase 4.
- TTS creates a fresh SAPI engine per utterance, so speech cannot be
  interrupted yet (Phase 2).
- `max_tokens` is 50: measured generation speed on this CPU is ~3.7-4.2 tok/s,
  so longer answers are impractically slow.
- Peak memory has been measured for the single-model design only; optional
  models (Piper, embeddings, OCR, wake word) must be strictly on-demand.
