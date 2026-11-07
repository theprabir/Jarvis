# JARVIS — Project Audit

**Audit date:** 2026-11-07
**Auditor scope:** Read-only inspection + live measurements. No existing file was modified, deleted, or installed. `PROJECT_AUDIT.md` is the only file created.
**Target:** Local-first voice assistant, Windows 11, Python, Llama 3.2 1B Q4_K_M via existing `llama-server.exe`, 1.3 GB peak RAM ceiling.

---

## 1. Executive Summary

The project is at a **very early prototype stage**: 4 small Python files (~270 lines total), no voice input, no memory database, no configuration, no logging, no tests, no README, not under Git.

What genuinely works:

- `llama-server.exe` launches, loads the model, and answers over the OpenAI-compatible HTTP API.
- Windows SAPI TTS (pyttsx3) speaks correctly.
- A blocking CLI loop accepts typed input and speaks replies.

What is materially broken or misleading:

1. **Web search returns nothing or unrelated spam.** `duckduckgo_search` 8.1.1 scraping produced 0 results for one query and 3 adult-spam results for another. Since the assistant is designed to *always* search, most answers are generated without usable evidence.
2. **The LLM fabricates facts when search is empty.** Query "who is the prime minister of Japan" → "Fumio Kishida" (stale) after search returned no usable results. This directly violates the spec's "never fabricate results" rule.
3. **Throughput is ~3.7–4.2 tokens/s** on this i3-3240. A 300-token web context costs **31.9 s** of prefill alone. The current 30 s HTTP timeout is shorter than a realistic search-grounded request → guaranteed `Timeout` exceptions, which the code does not catch → crash.
4. **The server runs with 4 parallel slots** (llama.cpp default), so each conversation effectively gets **512 tokens of the 2048 context**.
5. **No conversation history** is sent, so follow-up questions cannot work.
6. **No microphone/STT exists at all** — the "voice assistant" is text-in, voice-out.
7. `requirements.txt` is out of sync with the venv (`ctransformers` not installed, unused; `ddgs` referenced by code is not installed).

RAM verdict: **1.3 GB is achievable** with the current single-model design (measured assistant footprint ≈ **1.0 GB peak**), but only if optional models (Piper, embeddings, OCR, wake word) stay unloaded. It is not achievable with any additional resident neural model.

---

## 2. Inspection Method

- Read every project file: `main.py`, `core/brain.py`, `core/tools.py`, `core/tts.py`, `core/__init__.py`, `requirements.txt`, `data/todo.txt`, `README.md`, `AGENTS.md`, `PLANS-STACK-FEATURES.md`.
- Inspected the installed venv (35 packages, Python 3.11.9 x64), the GGUF file, the llama.cpp build and its loaded backend DLLs.
- Measured the **live** `llama-server` already listening on `127.0.0.1:8080` (PID 13744) rather than starting a new one: health, `/props`, working set, peak working set, prompt/generation timings, cold vs warm behaviour.
- Measured Python/dependency RSS, TTS engine initialization, DuckDuckGo result quality, and end-to-end `process_with_brain()` latency.
- No process was started or stopped; the pre-existing orphaned server was left running untouched.

**Limitations:** TTS was verified with volume muted (no audio listened to). Voice input cannot be tested (not implemented). No 1-hour soak test. Search quality is network-dependent and was sampled, not exhaustively benchmarked.

---

## 3. Current Architecture

```
main.py                      CLI loop, server lifecycle, heuristic "should I search?", speaks reply
├── core/brain.py            ask_brain(): one-shot POST /v1/chat/completions (requests, 30 s)
└── core/tools.py            search_duckduckgo(), JSON memory helpers (unused), open_app (unused),
                             get_schedule (unused)
core/tts.py                  EMPTY (0 bytes)
core/__init__.py             EMPTY
data/todo.txt                EMPTY (unused placeholder)
models/Llama-3.2-1B-Instruct-Q4_K_M.gguf   807,694,368 bytes (770 MiB)
```

Runtime topology: one blocking CLI process → spawns `llama-server.exe` (hidden, stdout/stderr discarded) → HTTP requests → pyttsx3 SAPI5 speech. No threads, no asyncio, no scheduler, no database.

**Request path:**

1. `input()` reads a line (typed only).
2. `process_with_brain()` decides "factual?" by substring matching against `who/what/where/when/why/how/which/search/host/cup/match/score` **or** more than 3 words.
3. If factual → `search_duckduckgo()` (live scrape, 3 results) → results are pasted into the prompt.
4. `ask_brain()` posts system+user messages, `max_tokens=50`, `temperature=0.2`, 30 s timeout.
5. `speak()` creates a **new** pyttsx3 engine, speaks, stops, discards.

---

## 4. Existing Working Features (verified)

| Feature | Status | Evidence |
| --- | --- | --- |
| llama-server launch | Works | Live process command line matches `main.py`'s argv exactly |
| Model load + health | Works | `/health` → `{"status":"ok"}`, `model_ftype: Q4_K - Medium` |
| HTTP chat completions | Works | 200 OK, coherent replies (8 tokens for "capital of France") |
| Correct AVX CPU backend | Works | `ggml-cpu-ivybridge.dll` loaded; no Vulkan backend loaded (important: no AVX2/FMA on this CPU) |
| SAPI5 TTS | Works | Engine init + speak completed in 3.5 s muted; voices `David` and `Zira` present |
| Model path resolution | Works | Absolute path derived from `__file__`; file exists |
| CLI loop + text fallback | Works | Typed input, `exit/quit/bye`, Ctrl-C handling |
| Graceful handling of "server down" | Partly | Only `ConnectionError` is caught, returns a spoken message |
| Reuse of an already-running server | Works | `is_server_running()` prevents a second model load |

`main.py` and `core/brain.py` are the only files that are functionally connected. Everything else is dead code or empty.

---

## 5. Incomplete / Broken Features

| Item | Severity | Detail |
| --- | --- | --- |
| Web search quality | **Critical** | `duckduckgo_search` 8.1.1 scrape: `capital of Japan` → 0 results; `python 3.11 release date` → 3 completely unrelated adult-spam results. Library is renamed to `ddgs` and emits a deprecation warning. No retry, no fallback provider, no validation of relevance. |
| Fabricated answers | **Critical** | With empty search context the 1B model still asserts facts (stale "Fumio Kishida"). System prompt demands an answer "based on the provided search context", so it invents one. |
| Voice input | Missing | No microphone, VAD, STT, push-to-talk or hotkey. `sounddevice`, `vosk`, `webrtcvad` not installed. |
| Conversation history | Missing | Every request is a fresh system+user pair; no history, no follow-ups, no summarization. |
| Intent routing | Broken by design | Substring heuristic: `"who" in "whole"`, `"how" in "show"`; **any** sentence >3 words triggers a web search. Includes junk keywords `host/cup/match/score`. Result: nearly every query hits the broken search path. |
| Tool integration | Not wired | `open_app`, `update_memory`, `get_schedule` exist but are never called. No registry, no schemas, no permissions. |
| Memory | Half-built, unused | `data/memory.json` helpers exist but no caller; non-atomic writes; silent `except: return {}`; no SQLite, no FTS5. |
| TTS robustness | Weak | New engine per utterance (~1–2 s overhead, COM/handle churn), cannot be interrupted, blocks the loop, no volume/voice config, no barge-in. |
| Error handling | **High** | `requests.exceptions.Timeout` and JSON/KeyError are not caught → crash or silent nonsense. 30 s timeout < 31.9 s measured for a realistic prompt. |
| Server diagnostics | High | `stdout/stderr=DEVNULL`; startup failures (bad path, port in use, missing DLL) are invisible. |
| Startup readiness | Medium | Waits max 10 s (20 × 0.5 s) then returns **without** checking success. Cold model load can exceed that. |
| Configuration | Missing | Port `8080` duplicated in `main.py` and `brain.py`; model path, voice, rate, tokens hardcoded. No TOML. |
| Logging / monitoring | Missing | `print()` only; no `logging`, no `psutil`, no latency/RSS measurement. |
| Project hygiene | Medium | `README.md` empty; no `.gitignore` (required by AGENTS.md); no `CHANGELOG.md`; no `tests/`; no Git repo at all (`.freebuff/` only). |
| Empty modules | Low | `core/tts.py` and `data/todo.txt` are empty placeholders. |
| Latent command injection | Medium | `os.system(f"start {app_name}")` would execute arbitrary shell text (not reachable today, but must never be wired up as-is). |

---

## 6. Technology Stack & Dependencies

**Installed (venv, Python 3.11.9 x64):** requests 2.34.2, pyttsx3 2.99, pywin32 312, comtypes 1.4.17, duckduckgo-search 8.1.1, numpy 2.4.6, lxml, primp, huggingface_hub 2.4.0, httpx2 2.16.0 + httpcore2, diskcache, packaging, pip 26.3, etc.

`requirements.txt` declares: `ctransformers>=0.2.27`, `duckduckgo-search>=6.0.0`, `pyttsx3>=2.90`, `huggingface-hub>=0.20.0`, `requests>=2.32.0`.

**Dependency problems:**

- `ctransformers` — **unused and not installed**; a heavyweight C++ inference binding. Directly contradicts the "no alternate inference engine" constraint. Must be removed from requirements.
- `huggingface-hub` — unused; it is what pulls in `httpx2`, `hf-xet`, `jinja2`, `tqdm`, `fsspec`, `filelock`. Pure bloat for this project.
- `duckduckgo-search` — renamed upstream; the code's preferred import `ddgs` is **not installed**, so it always falls back to the deprecated module and prints a `RuntimeWarning`.
- `httpx2` is *not* the `httpx` the spec requires. Do not build on it; the spec's HTTP client (`httpx`) is not installed.
- Missing for the roadmap: `httpx`, `psutil`, `vosk`, `sounddevice`, `webrtcvad`, `pydantic`, `ddgs` (or a working search provider).
- SQLite 3.45.1 with **FTS5 available** — verified; no install needed for the planned memory layer.

Net: the declared stack is roughly half of what the spec calls for, plus one forbidden-weight library.

---

## 7. LLM Integration Assessment

**Server (live process):**

```
llama-server -m D:\jarvis\models\Llama-3.2-1B-Instruct-Q4_K_M.gguf --port 8080 -c 2048 -t 2
```

Build: llama.cpp `0.9.0-dev`, build 12121 (`e56f006ed`), Clang 20.1.8, x86_64. CPU backend `ggml-cpu-ivybridge` (correct for Ivy Bridge/AVX). `ggml-vulkan.dll` ships with the package but was **not** loaded — good, no GPU offload attempted.

`/props` reports: `n_ctx: 2048`, **`total_slots: 4`**, `chat_format: "Content-only"`.

**Issues:**

1. **4 slots for 1 user.** llama.cpp's default `-np -1` (auto) produced 4 slots, so the 2048-token context is divided into **512 tokens per slot**. Combined with a 300-token search context, the effective conversation budget is nearly exhausted. Spec requires `parallel slots = 1`.
2. **Unset recommended flags:** no `-np 1`, no `-tb 2`, no `-b/-ub 64` (defaults 2048/512 inflate compute buffers), no `-ngl 0`, no `--host` (default is loopback, acceptable but implicit), no `--no-warmup`/warmup strategy, no `--threads-http` control. Only `-c 2048` and `-t 2` match the spec.
3. **No streaming.** Full response must finish before the first spoken word — 12 s+ of dead air for a 50-token answer.
4. **No prompt-cache strategy.** System prompt is re-prefilled on a random slot.
5. **Client is `requests`, one-shot, no session.** Spec mandates `httpx` with a persistent client. No retries, no connect/read timeout split, no cancellation.
6. **`model: "Llama-3.2-1B"` in the payload** is ignored by a single-model server (harmless, but misleading).
7. **`max_tokens: 50`** contradicts the spec's 256. Given measured throughput, 50 is the *correct* choice — the spec's 256 would take ~65 s of generation. The spec value should be revised, not the code.
8. **Timeouts mismatch:** 30 s client timeout vs 31.9 s measured for a 301-token prompt; cold start measured 14 s of prefill for only 23 tokens.
9. **Failure modes uncaught:** only `ConnectionError`. `Timeout`, HTTP 4xx/5xx body errors, `JSONDecodeError`, and missing `choices[0]` all propagate.

**Measured performance (live, 2 threads, 2048 ctx):**

| Scenario | Prefill | Generation | Wall |
| --- | --- | --- | --- |
| Cold first request (23 prompt tokens) | 14.06 s (1.64 tok/s) | 2.56 tok/s | 17.2 s |
| Warm, short (10 prompt tokens) | 2.74 s (3.65 tok/s) | 3.82 tok/s | 5.9 s |
| Warm, cached prefix (1 token) | 0.36 s | 4.20 tok/s | 3.2 s |
| **Realistic web context (301 tokens)** | **30.77 s (9.78 tok/s)** | 3.70 tok/s | **31.9 s** |

Cold-start penalty (14 s for a 23-token prompt) indicates the 770 MiB mmap'd weights were **paged out** — only 2.1 GB of 7.9 GB system RAM is free. This will recur whenever another app pressures memory.

---

## 8. RAM & CPU Assessment

**Measured (live):**

| Component | Working set | Notes |
| --- | --- | --- |
| llama-server, steady (warm) | **787 MB** | 2048 ctx, 4 slots, mmap'd weights |
| llama-server, **peak** | **931.5 MB** | PeakWorkingSet64 — the budget-relevant number |
| llama-server, private bytes | 237 MB | Weights are shared file mapping, not private |
| Python + requests + pyttsx3 + SAPI init + ddgs | **64.8 MB** peak | Measured in-process |
| TTS (SAPI, in-process) | included above | ~32 MB for engine + COM |
| Vosk small English model (Phase 2, not installed) | ~50–70 MB est. | Must be measured after install |
| Audio buffers + WebRTC VAD | ~10–20 MB est. | |

**Projected assistant peak after Phase 2:** ~**1.05–1.15 GB** → inside 1.3 GB, with ~150–250 MB headroom.

**Verdict:** the architecture **can** meet 1.3 GB because it keeps exactly one neural model resident and uses SAPI (OS-provided) for speech. It **cannot** meet 1.3 GB if Piper, wake-word models, embeddings or OCR are loaded concurrently; those must be strictly on-demand per spec §6.

**Caveats that undermine the number:**

- System has only **2.13 GB free** of 7.9 GB. The assistant's own footprint fits, but the model's mmap pages get trimmed under pressure, producing 14 s cold stalls.
- Peak (931 MB) is ~160 MB above the 770 MiB file size: KV cache (~67 MB at 2048 ctx) + compute buffers (inflated by default `-b 2048 -ub 512`).
- 4 slots waste/partition context; `-np 1` plus `-b/-ub 64` should reduce both peak memory and prompt overhead (to be measured, not assumed).

**CPU:** i3-3240, 2 cores / 4 threads, AVX only. `-t 2` is correct (do **not** raise to 4 — SMT gives little and steals from the UI). At 3.7–4.2 tok/s generation and ~9.8 tok/s prefill, the current "search then answer 50 tokens" flow is **20–40 s** per query. Voice UX requires: deterministic routing for common commands, ≤ 80 generated tokens, capped context (~250 tokens), streaming TTS, and no search for non-factual input.

---

## 9. Gap vs. Specification

Level 1 coverage is roughly **10–15%** (TTS, typing, LLM call, a broken search). Nothing from Levels 2–3 exists.

| Spec area | Status |
| --- | --- |
| 4.1 Voice interface | **Absent** except TTS. No mic, PTT, hotkey, VAD, STT, barge-in, stop-playback, voice/speed/volume config |
| 4.2 Conversation intelligence | Absent: no history, context, follow-ups, summarization, response modes, clarification |
| 4.3 Basic intelligence | Absent: no intent detection, entity extraction, JSON tool output, validation, confidence |
| 4.4 Computer interaction | 1 of ~27 items (`open_app`, unwired, unsafe) |
| 4.5 Internet search | Partial and broken: no abstraction, dedup, ranking, extraction, caching, citations, fallback |
| 4.6 Basic memory | JSON stub, unused; no SQLite, no search/edit/delete UI |
| 4.7 Utilities | 1 of ~11 (`get_schedule`, fake/hardcoded; unwired) |
| 4.8 Advanced memory | Absent |
| 4.9 Task planning | Absent |
| 4.10 Document intelligence | Absent (no pypdf/python-docx/openpyxl, no FTS5 RAG) |
| 4.11 Productivity | Absent (todo.txt empty, no scheduler/APScheduler) |
| 4.12 Developer assistant | Absent |
| 4.13 Advanced research | Absent |
| 4.14 Application intelligence | Absent |
| 4.15–4.20 (Level 3) | Absent |
| §11 Security | Absent: no permissions, allowlists, confirmations, audit log, injection defenses |
| §6 ModelManager | Absent |
| §14 Benchmarking | Absent |

---

## 10. Technical Problems & Risks (ranked)

1. **Unreliable search + unconditional search + no grounding guard** → confidently wrong answers. Highest user-visible risk.
2. **Timeout shorter than real latency, with uncaught `Timeout`** → hard crashes exactly when a search context is supplied.
3. **512-token effective context (4 slots)** → silent truncation once history/search is added.
4. **No conversation history** → the assistant cannot function as an assistant, and every turn re-prefills everything (slow).
5. **TTS engine per utterance** → COM/handle churn, ~1–2 s overhead, no interruption; also blocks timers/background work.
6. **No diagnostics** (DEVNULL stderr, print-only logging) → every future failure is blind.
7. **Cold-start paging** on a 2.1 GB-free machine → intermittent 14 s stalls; needs monitoring and possibly `--mlock`/smaller ctx decisions based on measurement.
8. **Dependency drift** (`ctransformers`, `huggingface-hub`, `ddgs` missing, `httpx` absent) → `pip install -r` would produce a working-but-wrong environment and add an unused inference framework.
9. **Unsafe latent helpers** (`os.system(f"start {app_name}")`, non-atomic JSON writes, bare `except`) → must be rewritten before any wiring.
10. **Project hygiene gaps** (README empty, no `.gitignore`/`CHANGELOG`/tests/Git) → violates AGENTS.md and blocks the changelog/version protocol.

---

## 11. Recommended Implementation Order

Follow the spec roadmap, one feature set at a time, with measurement gates. Do not start Phase 2 before Phase 1's numbers exist.

**Phase 1 — Stabilization (next)**
1. `config/settings.toml` + `core/config.py` (typed loader, no new framework beyond stdlib `tomllib`); move port, model path, flags, tokens, temperature, voice, rate.
2. `core/logger.py` — `logging` to `logs/jarvis.log` + console; stop using `print`.
3. `brain/llm_client.py` — persistent **httpx** client, split connect/read timeouts (≈5 s / 120 s), retries with backoff, streaming, full error taxonomy, JSON-safe parsing.
4. Rewrite server launch: explicit `-np 1 -t 2 -tb 2 -b 64 -ub 64 -ngl 0 -c 2048 --host 127.0.0.1`, log stderr to `logs/llama-server.log`, readiness poll up to 120 s with real failure reporting, PID tracking, cleanup of orphans.
5. Remove the always-search heuristic (route to search only in Phase 4); add a grounding guard: no evidence → say so.
6. Fix `requirements.txt` (drop `ctransformers`/`huggingface-hub`; add `httpx`, `psutil`; resolve the `ddgs` question in Phase 4).
7. Benchmark: idle, loaded, inference, peak RSS, tok/s; record in `PROJECT_AUDIT`/`CHANGELOG`.

**Phase 2 — Voice:** `sounddevice` + `webrtcvad` + Vosk small + PTT hotkey; single reusable TTS engine on its own thread with stop/interrupt; text fallback. Measure incremental RSS.

**Phase 3 — Intelligence & tools:** router/intent (deterministic first, LLM fallback), `ToolRegistry` + Pydantic schemas + permission levels, calculator, safe app launcher (allowlist), filesystem, system info, SQLite memory + conversation history + FTS5.

**Phase 4 — Internet:** provider abstraction (`ddgs` package and/or Brave), dedup/rank/extract (trafilatura), bounded cache, citations, aggressive context budget (~250 tokens), grounding + "I don't know" path.

**Phases 5–7:** productivity/documents → planner/agent → wake word, proactive briefings, developer assistant, visual intelligence.

---

## 12. File Disposition

**Preserve as-is**
- `models/Llama-3.2-1B-Instruct-Q4_K_M.gguf` (do not re-download/move)
- `AGENTS.md`, `PLANS-STACK-FEATURES.md` (requirements baseline)
- The `llama-server.exe` inference architecture and its AVX/ivybridge build

**Modify (in place, incrementally)**
- `main.py` — becomes thin bootstrap → orchestrator; remove heuristic search, move `speak()` out
- `core/brain.py` — hardened client (or superseded by `brain/llm_client.py`, keeping a compatible `ask_brain`)
- `core/tools.py` — split into `tools/*`; memory helpers → SQLite; remove duplicate imports and `os.system`
- `requirements.txt` — prune/add as above
- `README.md` (currently empty), add `.gitignore`, add `CHANGELOG.md`

**Create (planned)**
- `config/settings.toml`, `core/config.py`, `core/logger.py`
- `brain/llm_client.py`, `brain/prompt_manager.py`, `brain/output_parser.py`
- `voice/{microphone,vad,speech_to_text,text_to_speech,interruption}.py`
- `core/{orchestrator,router,tool_registry}.py`
- `memory/{database,short_term,long_term,memory_search}.py`
- `tools/{web_search,filesystem,applications,system,calculator}/…`
- `logs/`, `cache/`, `tests/`, `data/jarvis.db`

**Decide with owner**
- `core/tts.py` (0 bytes) — either becomes the TTS implementation or stays dormant; nothing references it
- `data/todo.txt` (0 bytes) — becomes productivity storage or is retired
- `data/memory.json` — migrate to SQLite, then retain as legacy import source

---

## 13. Assumptions & Open Questions

1. **The running server is orphaned** (PID 13744; parent 12572 no longer exists) — started with the same argv as `main.py`. It will be reused by `is_server_running()`. Confirm whether it should be terminated before Phase 1 benchmarks.
2. Measured throughput is from that live instance under current system load (2.1 GB free). A quiet-machine benchmark may differ; re-measure in Phase 1.
3. Spec's `max_tokens = 256` appears unachievable for interactive use on this CPU (~65 s). Assumed the spec is guidance, not a hard requirement, and that ~50–80 is acceptable.
4. `ddgs` vs Brave API is an open decision for Phase 4; the current scraper cannot be trusted for grounded answers.
5. No Git repository exists; the AGENTS.md changelog protocol has never been started (no `CHANGELOG.md`).

---

## Appendix — Raw Measurements

```
OS RAM:            7.9 GB total, 2.13 GB free at audit time
CPU:               Intel Core i3-3240 @ 3.40 GHz, 4 logical processors
Python:            3.11.9 (64-bit), .venv, SQLite 3.45.1 (FTS5 OK)
Model file:        807,694,368 bytes, Q4_K - Medium
llama.cpp:         0.9.0-dev, build 12121 (e56f006ed), Clang 20.1.8
Backend loaded:    ggml-cpu-ivybridge.dll (no Vulkan)
Server argv:       llama-server -m D:\jarvis\models\...gguf --port 8080 -c 2048 -t 2
/props:            n_ctx=2048, total_slots=4
RSS:               steady 787 MB, peak 931.5 MB, private 237 MB
TTS:               3.5 s init+speak+stop (voices: David, Zira)
Python RSS:        12.2 base → 27.5 requests → 59.2 SAPI → 64.8 ddgs (peak)
Search sample:     "capital of Japan" → 0 results; "python 3.11 release date" → irrelevant spam
E2E:               "hello jarvis" 8.4 s; "who is the prime minister of Japan" 11.0 s → stale fact
Side effect:       importing modules created __pycache__/ (safe to delete, unmodified sources)
```
