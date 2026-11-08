# Changelog

All notable changes to JARVIS are recorded here. Versions follow the project
convention: each completed feature set increments the patch number; completing
all feature sets in a phase increments the middle number and resets the patch.

## v0.1.3 — 2026-11-08

**Feature set 4.3 — Basic Intelligence** (Phase 3; implemented and verified),
plus two conversation additions on top of 4.2.

### Implemented functionality

- **Deterministic intent recognition** — `core/intents.py` classifies clock,
  calendar, arithmetic and slash-command requests before any model call. The
  router returns nothing for open-ended or ambiguous input, so the LLM remains
  the fallback for anything it cannot answer exactly.
- **Local answers** — current time, current/relative dates and pure arithmetic
  are answered by Python instantly and exactly ("what is 17 times 24?" → "That
  is 408." with zero model calls).
- **Safe calculator** — `core/calculator.py` evaluates arithmetic with a
  restricted AST walker: numbers, parentheses and `+ - * / // % **` only, with
  length, exponent and magnitude limits. Names, calls and attribute access are
  rejected, so nothing is executed.
- **Entity extraction and date interpretation** — `core/dates.py` resolves
  today/tomorrow/yesterday, "in N days", "N weeks ago", "next/last weekday"
  and ISO dates into a target date and offset; entities travel with the intent.
- **Structured JSON with validation** — `Intent` serializes to JSON
  (`intent`, `confidence`, `entities`, `source`) and `Intent.from_dict`
  validates untrusted payloads (known intent name, confidence 0-1, flat
  primitive entities, known source). `/intent TEXT` exposes it for inspection.
- **Confidence gating and logging** — intents below 0.7 confidence are not
  answered locally, and every local answer logs its intent and confidence.
- **Session resume** — with `resume_last_session = true` the latest SQLite
  session is reloaded at startup (up to `max_turns` exchanges) and continued,
  so context and session id survive a restart.
- **Confirmed `/forget`** — reports the number of saved exchanges, requires
  `/forget confirm`, then deletes every saved exchange, resets the in-memory
  conversation and starts a new session. A second confirmation without a
  pending request deletes nothing.

### Files created

`core/intents.py`, `core/calculator.py`, `core/dates.py`,
`tests/test_intelligence.py`.

### Files modified

`core/assistant.py` (intent routing before clarification, local answers,
resume, `/forget`, `/intent`), `core/history.py` (`last_session_id`,
`session_turns`, `clear`), `core/config.py` (+`resume_last_session`),
`config/settings.toml`, `README.md`, `CHANGELOG.md`.

### Bug fixes

- **Valid short questions were mistaken for missing context** — "what time is
  it?" contains the word "it", so the clarification gate replied "I don't have
  earlier context" instead of answering. Deterministic intents are now matched
  before the clarification check.
- **Context was lost on every restart** — history exists for the audit trail
  only; resume now reloads the previous session's turns into the prompt.

### Testing status

- 105 unit tests pass (`python -m unittest discover -s tests -v`): calculator
  limits and rejection of code-like input, date phrases (including
  day-after-tomorrow precedence and ISO dates), router coverage and fallbacks,
  JSON validation failures, local answers, `/intent`, resume (including
  `max_turns` limiting and the disabled case) and `/forget` confirmation rules.
- End-to-end run 1 (fresh database): introduction, "what is 17 times 24?" →
  "That is 408.", clock and tomorrow's date answered locally, `/intent` printed
  `{"confidence": 0.92, "entities": {"date": "2026-11-09", ...}, "intent":
  "date", ...}`, and an open question answered by the model.
- End-to-end run 2 (restart): resumed the previous session, answered "Your name
  is Alex." from context, then `/forget` + `/forget confirm` removed 6 saved
  exchanges; the database reported `count= 0` afterwards.

## v0.1.2 — 2026-11-08

**Feature set 4.2 — Conversation Intelligence** (Phase 3; implemented and
verified).

### Implemented functionality

- **Multi-turn conversation** — `core/assistant.py` sends the recent exchanges
  with every request through `core/conversation.py`, so follow-ups, pronouns
  and references resolve against what was already said. Verified end to end:
  "My name is Alex." then "What is my name?" answered "Your name is Alex."
- **Context compression** — the oldest turns are summarized by the model once
  the transcript passes `summarize_after`, with an extractive fallback when the
  summary call fails. `max_context_chars` caps the transcript and `max_turns`
  caps the verbatim exchanges, keeping requests inside the 2048-token context.
- **Answer modes** — `short` (50 tokens), `detailed` (120) and `technical`
  (160); each mode changes both the token budget and the system instruction.
  Switch at runtime with `/mode`.
- **Persistent conversation history** — `core/history.py` writes every completed
  exchange to SQLite (`data/jarvis.db`, table `conversations` with session id,
  messages, mode and timestamp), and `/history` reads the current session back.
- **Clarification and uncertainty** — the system prompt makes the model ask one
  short question instead of inventing details, and a short request containing a
  dangling reference ("what about that?") with no history gets a deterministic
  clarification question without calling the model.
- **Graceful failures** — connection, timeout and malformed-response errors
  produce spoken-safe sentences; failed exchanges are reported but never stored
  as answers.
- **Commands** — `/new` (fresh conversation and session), `/mode`, `/history`
  and `/help`, available in both text and voice mode.
- **Config** — new validated `[conversation]` section (mode, turn and char
  budgets, per-mode token limits, history path, on/off switch).

### Files created

`core/conversation.py`, `core/history.py`, `core/assistant.py`,
`tests/test_conversation.py`.

### Files modified

`core/config.py` (+`ConversationConfig`, validation, settings wiring),
`config/settings.toml` (+`[conversation]`), `main.py` (uses `Assistant` for text
and voice), `voice/interface.py` (takes a reply callable instead of owning the
prompt), `README.md`, `.gitignore` (history database already ignored),
`CHANGELOG.md`.

### Bug fixes

- **Context-dependent questions were answered blind** — every request used to be
  a single independent prompt, so follow-ups like "what is my name?" lost the
  earlier exchange. History now travels with each request.
- **Context overflow** — transcripts are capped by turn count and character
  budget before they are sent, instead of silently eating the reply budget.

### Testing status

- 66 unit tests pass (`python -m unittest discover -s tests -v`); the new suite
  covers context assembly, mode instructions and budgets, context trimming,
  summarization and its fallback, commands, clarification, SQLite persistence,
  disabled-conversation mode and the three failure messages.
- End-to-end `main.py --text --no-tts` run: introduced a name, asked a follow-up
  (correct answer), switched to detailed mode, printed `/history`, and persisted
  both exchanges to `data/jarvis.db` under one session id.
- Not verified by ear in voice mode; `/mode`, `/history` and `/new` reach the
  model through the same `Assistant.reply` path the voice interface calls.

## v0.1.1 — 2026-11-08

**Feature set 4.1 — Voice Interface** (Phase 2; implemented and verified).

### Implemented functionality

- **Push-to-talk capture** — `voice/audio.py` records 16 kHz mono 16-bit PCM in
  30 ms frames through `sounddevice`, resampling linearly when the device only
  opens at 44.1/48 kHz. A configurable global hotkey (`ctrl+space`),
  Enter on an empty console line, or typing works at any time.
- **VAD segmentation** — WebRTC VAD (`vad_aggressiveness` 0–3) starts a request
  on speech, keeps a 300 ms pre-roll so the first syllable is not clipped, ends
  it after `silence_ms`, rejects clicks under `min_speech_ms` and caps capture
  at `max_record_s`.
- **Offline transcription** — `voice/stt.py` loads the Vosk small English model
  once and transcribes each utterance locally; no audio leaves the machine.
- **Interruptible speech** — `voice/tts.py` drives `SAPI.SpVoice` directly
  through COM instead of creating a `pyttsx3` engine per reply. One persistent
  voice object, asynchronous `Speak`, and purge-based cancellation: Escape or
  a new push-to-talk stops playback (measured interrupt at 0.77 s) for
  barge-in.
- **Voice conversation loop** — `voice/interface.py` owns all audio, speech and
  LLM calls on one thread; the hotkey and console reader only set flags or
  queue text, so nothing is used from two threads. Spoken `exit`/`quit`/`bye`
  shut down, `/stop` and `/voices` are supported, and typed input is accepted
  in the same session.
- **Graceful degradation** — a missing Vosk model, unavailable microphone, SAPI
  failure or missing `keyboard` package is logged and JARVIS falls back to text
  mode (or console-Enter recording) instead of crashing.
- **CLI flags** — `--voice` (force voice), `--text` (microphone off) and
  `--no-tts` (listen without speaking), plus the new `[voice]` config section
  with validation for every value.

### Files created

`voice/__init__.py`, `voice/errors.py`, `voice/audio.py`, `voice/stt.py`,
`voice/tts.py`, `voice/interface.py`, `tests/test_voice.py`.

### Files modified

`core/config.py` (+`VoiceConfig`, validation, settings wiring),
`config/settings.toml` (+`[voice]`), `main.py` (shared SAPI voice, voice/text
mode selection, fallbacks), `requirements.txt` (+comtypes, numpy, sounddevice,
webrtcvad-wheels, vosk, keyboard; −pyttsx3), `.gitignore` (+Vosk model and
model archives), `README.md`.

### Bug fixes

- **Speech start-up latency** — the old `speak()` initialized a fresh pyttsx3
  engine per utterance (~3.5 s before the first word) and could not be
  interrupted. The shared SAPI voice removes the per-reply cost and supports
  barge-in.
- **Ambient noise misread as speech** — utterance boundaries come from VAD plus
  a minimum speech duration and pre-roll, so a door click or breath no longer
  sends an empty request to the model.

### Testing status

- 41 unit tests pass (`python -m unittest discover -s tests -v`), including
  VAD silence/max-duration/no-speech/pre-roll cases, 44.1→16 kHz resampling,
  device resolution, speech text cleanup, SAPI rate mapping and a real Vosk
  transcription of silence.
- Live checks on the target machine: default microphone probes and opens at
  16 kHz; 2 s of ambient audio correctly produced `no_speech`; Vosk model load
  raised the voice process to 172 MB RSS (195 MB with SAPI and the audio
  stack); SAPI voices David and Zira detected; a spoken test sentence completed
  in 3.5 s and was interrupted on demand in 0.77 s.
- End-to-end `main.py --text --no-tts` run starts llama-server, prints replies
  and exits cleanly (exit code 0).
- Not verified: speech audio quality by ear and real spoken commands (they
  require the user at the microphone).

## v0.1.0 — 2026-11-07

**Phase 1 — Existing System Stabilization** (feature sets 1.1–1.4: configuration,
logging, LLM client, server lifecycle; completed and verified).

### Implemented functionality

- **Configuration system** — `config/settings.toml` with typed, validated
  loading (`core/config.py`). Unknown keys and out-of-range values fail fast;
  path override via `--config` or `JARVIS_CONFIG`. Removes the hardcoded port,
  model path, token limit, voice settings and server flags.
- **Logging** — `core/logger.py`: console plus bounded rotating file log
  (`logs/jarvis.log`, 1 MB × 3). Third-party HTTP request logs are suppressed.
  Replaces all diagnostic `print()` calls.
- **Hardened LLM client** — `brain/llm_client.py`: one persistent HTTPX client
  per process, split connect/read timeouts, connection and 503 retries, and a
  full error taxonomy (`brain/errors.py`). `brain/output_parser.py` validates
  chat completions and exposes per-request timings.
- **Server lifecycle** — `core/server_manager.py`: launches `llama-server.exe`
  with the configured flags, waits for real `/health` readiness with a deadline,
  reports the last log lines on failure, writes server output to
  `logs/llama-server.log`, verifies settings of a reused server, and stops only
  the child it started.
- **Corrected launch flags** — `-np 1 -tb 2 -b 64 -ub 64 -ngl 0 --host
  127.0.0.1` added to `-c 2048 -t 2`. Verified: `n_slots = 1`,
  `n_ctx_slot = 2048` (previously 4 slots → 512 tokens per request).
- **Benchmark module** — `python -m core.benchmark` reports Python and
  llama-server working sets, system availability, startup time, prefill and
  generation speed, and total latency.
- **Failure handling** — connection refused, timeouts, HTTP errors, invalid
  JSON, empty messages and missing model now produce logged, spoken-safe
  messages and correct exit codes (1 = server unavailable, 2 = bad config).
- **Project hygiene** — `README.md` written, `.gitignore` added, tests added.

### Files created

`config/settings.toml`, `core/config.py`, `core/logger.py`,
`core/server_manager.py`, `core/benchmark.py`, `brain/__init__.py`,
`brain/errors.py`, `brain/output_parser.py`, `brain/llm_client.py`,
`tests/test_phase1.py`, `CHANGELOG.md`, `README.md`, `.gitignore`,
`PROJECT_AUDIT.md` (audit).

### Files modified

`main.py` (thin bootstrap, config-driven server and TTS, `--no-tts`),
`core/brain.py` (wrapper over the HTTPX client), `requirements.txt`
(+httpx, +psutil; removed unused `ctransformers` and `huggingface-hub`).

### Bug fixes

- Removed the always-search path from the request flow. It ran on nearly every
  input (substring or >3 words), relied on a scraper returning no or irrelevant
  results, and caused the model to answer from stale weights. Search is rebuilt
  in Phase 4; `core/tools.py` is untouched and unused.
- Server stderr is no longer discarded; startup failures are diagnosable.
- Readiness is confirmed by `/health`, not by a fixed 10-second sleep.
- Read timeout raised from 30 s to 120 s. The previous limit was shorter than
  the measured 31.9 s prefill for a 301-token prompt.
- Requests now reuse a kept-alive connection instead of opening a new one per
  turn.
- Model path and port exist in one place instead of duplicated modules.

### Testing

- `python -m unittest discover -s tests -v` → **16 tests, all passing**
  (config validation, response parsing, client error mapping, retry on 503,
  argv construction). No network required.
- End-to-end CLI run (piped input, `--no-tts`): server started in 2.6 s, two
  replies produced, clean shutdown, no orphan process, port released.
- Failure paths verified: server down → friendly message, no crash; invalid
  config → exit 2; missing model → exit 1 with clear error.
- Speech path verified with volume muted.
- Server verified: `n_ctx = 2048`, `total_slots = 1`, listening on
  `127.0.0.1:8080`.

### Measurements (i3-3240, 2048 ctx, 1 slot, 2 threads, batch 64/64)

| Metric | Before (4 slots, defaults) | After (Phase 1) |
| --- | --- | --- |
| Model startup | not measured (10 s blind wait) | 2.6–4.2 s |
| llama-server working set | 787 MB steady / 931.5 MB peak | 908.2 MB peak |
| llama-server private bytes | 236.9 MB | 166.7 MB |
| Assistant total (Python + server) | ~852 MB | 943.6 MB |
| Measured peak total | ~960 MB | 953 MB (target 1300 MB) |
| Prefill (≈56-token prompt) | 3.65 tok/s (10 tokens) | 11.5 tok/s |
| Generation | 3.7–4.2 tok/s | 5.3–10.6 tok/s (varies with system load) |
| Effective context per request | 512 tokens | 2048 tokens |

### Known issues / next

- No microphone, STT, VAD or push-to-talk (Phase 2).
- No conversation history; each turn is independent (Phase 3).
- No tool registry, permissions or deterministic routing (Phase 3).
- Web search is unreliable and unused; needs provider rebuild (Phase 4).
- TTS still creates an engine per utterance and cannot be interrupted (Phase 2).
- System RAM pressure is real (≈2.1 GB free): cold model starts can still stall
  on paging during the first request.
