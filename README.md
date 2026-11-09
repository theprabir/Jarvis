# JARVIS

A local-first, voice-and-text personal AI assistant for Windows 11, built
around a small local LLM (Llama 3.2 1B Instruct Q4_K_M) served by the existing
`llama-server.exe` from llama.cpp. The model is the language interface;
deterministic Python modules do the audio, routing and tool work.

**Current status:** Phase 1 (stabilization) and feature sets **4.1 Voice
Interface**, **4.2 Conversation Intelligence**, **4.3 Basic Intelligence**,
**4.4 Computer Interaction** and **4.5 Internet Search** complete. JARVIS
listens through the microphone with push-to-talk, transcribes offline with
Vosk, keeps multi-turn context across restarts, answers simple questions
deterministically through Python, operates the computer through validated
tools, searches the web and answers only from the fetched evidence with
sources, falls back to the local model for everything else, and speaks
through an interruptible Windows SAPI voice. Basic memory follows in feature
set 4.6 (see `PLANS-STACK-FEATURES.md`, `CHANGELOG.md`).

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
| `[tools]` | File sandbox root, read/search limits, allowed applications and commands |
| `[web]` | Search provider, result and query limits, page extraction, evidence budget, cache |

Unknown keys are rejected so typos fail fast. Override the file location with
`--config PATH` or the `JARVIS_CONFIG` environment variable.

Voice defaults: `ctrl+space` push-to-talk, VAD aggressiveness 2, a request ends
after 1200 ms of silence, clicks shorter than 250 ms are discarded, recording
is capped at 20 s. A shorter `silence_ms` cuts a request at its first pause and
produces fragmented transcripts. `input_device = ""` means the system default
microphone; set it to a name substring (for example `"USB"`) to pick another
device. `ptt_key` takes any `keyboard` hotkey string.

Transcription accuracy is set by the Vosk model. Both models were measured
here with SAPI-synthesized sentences (two voices, 20 clips):

| Model | Disk | Python process | Decode per ~3 s clip | Word error |
| --- | --- | --- | --- | --- |
| `vosk-model-small-en-us-0.15` (default) | 68 MB | 181 MB | ~2.1 s | 3.4 % |
| `vosk-model-en-us-0.22-lgraph` | 204 MB | 396 MB | ~10.5 s | 4.4 % |

The large model triples memory (assistant total would be 1305 MB, over the
1.3 GB target) and is about five times slower on this CPU without being more
accurate, so the small model stays the default. `stt_model` accepts any Vosk
model folder from <https://alphacephei.com/vosk/models>; if the configured
folder is missing, JARVIS loads the largest `vosk-model-*` in `models/` and
logs which one it used.

### Conversation behaviour

Every request carries the recent exchanges (up to `max_turns`), so follow-ups,
pronouns and references resolve against what was already said. Older exchanges
are compressed into a short summary instead of being dropped; if that summary
request fails, an extractive summary is used. Only `max_context_chars` of
transcript is sent, which keeps requests inside the 2048-token context.

Answer styles are set by `mode`, or at runtime with `/mode`:

| Mode | Reply limit | Style |
| --- | --- | --- |
| `short` | 90 tokens | Two short sentences (default) |
| `detailed` | 250 tokens | Complete answer with a short conclusion |
| `technical` | 320 tokens | Precise terminology and mechanisms |

If a reply still reaches its token limit, JARVIS asks the model to
continue where it stopped (`max_continuations`, default 1), joins the
fragments and drops any sentence the model repeated, so an answer is not
left half-written. The finish reason is logged with every reply. On this
CPU generation runs at about 5 tokens/s, so a full detailed answer takes
roughly 50 seconds.

Completed exchanges are written to `history_db` (`data/jarvis.db`, table
`conversations`). A short request that only refers to something ("what about
that?") without any history asks for clarification instead of guessing, and a
model failure produces a spoken sentence without being stored as an answer.

With `resume_last_session = true` the most recent saved session is reloaded at
startup, so JARVIS picks up the same conversation and session id after a
restart. `/forget` reports how many exchanges are saved and deletes them only
after `/forget confirm`.

### Deterministic answers

Questions Python can answer never reach the model: current time, current or
relative date ("tomorrow", "in 3 days", "next monday", ISO dates) and pure
arithmetic ("what is 17 times 24?") are recognized by `core/intents.py` and
answered locally, which is instant and exact. Arithmetic runs through a
restricted AST walker (`core/calculator.py`) that accepts only numbers,
parentheses and operators, so no Python code is executed. `/intent TEXT` prints
the detected intent as validated JSON (`intent`, `confidence`, `entities`,
`source`); anything ambiguous or open-ended falls back to the model.

### Computer interaction

Recognized computer requests are dispatched to the tools in `tools/` instead of
the model:

| Area | What JARVIS can do |
| --- | --- |
| System | CPU, memory, disk and battery status; top processes by memory |
| Applications | Open allowlisted applications, close the windows of a named app, close all open application windows on request, open files, folders and http(s) websites |
| Files | Read, write, create folders, rename, copy, move, delete, list and search — all inside `[tools] root` |
| Terminal | Run allowlisted commands (`ipconfig`, `hostname`, `ping`, …) without a shell, with validated arguments |
| Desktop | Screenshots into `screenshots/`, Windows notifications, list/focus/minimize/maximize windows |
| Clipboard | Copy text, and read it after confirmation |

Every tool declares argument names and types, a permission level and a
timeout. The registry validates arguments and turns failures into spoken
sentences. Anything that changes the machine — writing, deleting, moving,
running a command, closing an application, reading the clipboard — asks first
with one short sentence that names exactly what will be affected, for example
`Create the folder "reports". Please confirm to continue.` (or `/confirm` and
`/cancel`). Confirmations are deliberately brief because they are spoken
aloud; a long request is summarised by its actions, never read back.

A sentence that names several file actions is run as one plan: `create a
folder named drafts and in that folder write hello to file notes.txt and copy
notes.txt to backup.txt` asks once, then runs the steps in order and stops at
the first failure, reporting which step failed. Later relative paths stay
inside the folder just created, `in that folder` can be said explicitly, and
`create two files - a.txt & b.txt` expands to one action per name. A clause
that is not understood ends the plan there, so nothing is guessed. Reads, status queries, opening things, screenshots
and notifications run immediately. `close_app` affects only the
application named in the request: its visible windows are asked to close,
so unsaved work prompts instead of being discarded, and a windowless
instance is terminated only when that application was named explicitly.
"Close all apps" (or "close all programs", "close everything") closes
only applications with a visible window, never background, startup,
system, shell or JARVIS processes.
Destructive file actions cannot escape `[tools] root`, and applications and
commands must be listed in `config/settings.toml`.

### Internet search

Questions that need current information are routed to the search subsystem in
`web/`. The model never sees the open internet: it receives a numbered evidence
block and may only answer from it.

| Step | Behaviour |
| --- | --- |
| Query variants | The question plus a keyword form (`max_queries`, default 2), capped and deduplicated |
| Providers | `ddgs`/DuckDuckGo by default; Brave when `BRAVE_API_KEY` is set, selected by `[web] provider` |
| Ranking | Duplicate URLs merged, results sharing no term with the question dropped, title matches ranked first |
| Extraction | Top `extract_pages` pages are fetched and reduced to main text (`trafilatura` if installed, otherwise `lxml`) |
| Budget | At most `context_chars` (1000, roughly 250 tokens) of evidence reaches the model |
| Grounding | The answer must cite an existing source, and its figures or words must appear in the evidence; one retry is allowed, then JARVIS says the sources did not answer instead of guessing |
| Sources | The reply ends with a numbered `Sources:` list |
| Cache | Results cached in `data/search_cache.db` for `cache_ttl_s`; stale entries (up to 7 days) are used when the network fails |

Ask with `search the web for …`, `look up …`, `google …`, `find information
about …`, `latest news about …`, `wikipedia …`, or explicitly with `/web
QUESTION`. A plain `search for notes in drafts` still searches files, not the
web. Without a network connection the reply is a clear failure message, never
a made-up answer. Page fetching prefers `primp` (browser TLS fingerprint)
because many sites reject plain `httpx` with HTTP 403; `httpx` is the fallback.

`BRAVE_API_KEY` is read from the environment, never from the config file.

```bat
set BRAVE_API_KEY=...
.venv\Scripts\python main.py
```

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
- **Escape** stops speech immediately. The key is polled directly, so it works
  even when the global keyboard hook cannot be installed; pressing
  push-to-talk while JARVIS talks also cuts it off and starts listening.
- You can type at the `You:` prompt at any time; typed and spoken requests share
  the same conversation state.
- `/stop` clears a pending interrupt and `/voices` lists installed SAPI voices.
- `/new` starts a fresh conversation, `/mode short|detailed|technical` changes
  how much JARVIS says, `/history` shows recent exchanges, `/intent TEXT` shows
  the detected intent as JSON, `/tools` lists computer tools, `/confirm` and
  `/cancel` approve or drop a pending action, `/forget` deletes saved history
  after confirmation, and `/help` lists the commands.
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
clarification, SQLite history/resume/deletion, intent routing, arithmetic and
date parsing, JSON validation, tool argument validation, sandboxed file
operations, app/command allowlists, confirmation flows, multi-step file plans,
web search ranking, caching and grounded answers, Escape interruption, VAD
segmentation,
resampling, device resolution, speech text cleanup and SAPI rate mapping. The
Vosk transcription test is skipped when the model is absent and desktop tools
are only validated, never exercised, by the suite.

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
core/intents.py         Deterministic intent routing and JSON validation
core/calculator.py      Restricted arithmetic evaluation
core/dates.py           Date phrase interpretation
tools/base.py           Tool schema, permissions, validation and registry
tools/system.py         CPU, memory, disk, battery and process information
tools/files.py          Sandboxed file and folder operations
tools/apps.py           Allowlisted apps, URLs, paths and terminal commands
tools/desktop.py        Screenshots, notifications and window control
tools/clipboard.py      Clipboard read/write
web/base.py             Search result type, URL and token helpers
web/search.py           Query variants, ranking, evidence budget, orchestration
web/duckduckgo.py       DuckDuckGo provider (ddgs client plus HTML fallback)
web/brave.py            Brave Search API provider
web/extract.py          Page fetching (primp/httpx) and main-content extraction
web/cache.py            Bounded SQLite search cache with stale fallback
screenshots/            Screen captures (git-ignored)
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

- Only the most recent session is resumed (`resume_last_session`); older
  sessions stay in SQLite until `/forget` deletes them.
- Intent recognition is deterministic pattern matching for time, dates,
  arithmetic, computer actions and commands. Unusual wording falls back to the
  model, which cannot call tools yet.
- `close_app` closes the windows of the named application and can leave a
  background tray process running; `close_all_apps` only closes
  applications with a visible window, so background and startup programs
  stay untouched.
- File tools are sandboxed to `[tools] root` (the project folder by default).
  Set it to your Desktop, for example `root = "C:/Users/ADMIN/Desktop"`, to
  create and edit files there; a request for a location outside the root is
  refused with that hint instead of failing obscurely.
- Multi-step plans cover explicit file actions only (create, write, read,
  list, search, rename, copy, move, delete). A request that needs follow-up
  reasoning, such as "write the file location in it", is not planned: the
  sequence stops at that clause and the confirmation shows what will run.
- Web search reads only the top three pages and at most 1000 characters of
  evidence. Pages behind a login, a CAPTCHA or heavy JavaScript may contribute
  a snippet only, and the answer is limited to what those pages said - the
  model is instructed to answer `NOT_FOUND` rather than fill gaps from
  memory. Results are ranked by term overlap, not by publication date.
- Summarization costs one extra model call once the transcript grows past
  `summarize_after`, which adds a few seconds on this CPU.
- Speech recognition is English-only and limited by the small Vosk model:
  long sentences, names and file extensions transcribe poorly, and a short
  command is the reliable form. The lgraph model is not a win on this CPU
  (measured above). Set a specific `input_device` if the microphone picks up
  background noise, and lower `vad_aggressiveness` to keep quiet syllables
  that level 2 drops.
- Web search in `core/tools.py` is the legacy scraper; it returns unreliable
  results and is no longer used by `main.py`. It is rebuilt in Phase 4.
- Generation runs at ~5 tok/s on this CPU, so a 250-token detailed answer
  takes about 50 seconds; a reply that hits its limit is continued once,
  not generated without bound.
- Speech output is verified functionally (SAPI speaks, interruption at ~0.8 s),
  but audio quality and volume must be confirmed by ear on the target machine.
