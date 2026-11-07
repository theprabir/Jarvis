# Changelog

All notable changes to JARVIS are recorded here. Versions follow the project
convention: each completed feature set increments the patch number; completing
all feature sets in a phase increments the middle number and resets the patch.

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
