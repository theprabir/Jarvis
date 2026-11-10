# JARVIS

A local-first, voice-and-text personal AI assistant for Windows 11, built
around a small local LLM (Llama 3.2 1B Instruct Q4_K_M) served by the existing
`llama-server.exe` from llama.cpp. The model is the language interface;
deterministic Python modules do the audio, routing and tool work.

**Current status:** Phase 1 (stabilization) and feature sets **4.1 Voice
Interface** through **4.10 Document Intelligence** complete, released as
**v0.2.3** in the moderate-advanced feature line. JARVIS listens through the
microphone with push-to-talk, transcribes offline with Vosk, keeps multi-turn
context across restarts, remembers explicitly saved preferences, facts, paths
and folders, learns high-confidence first-person facts with provenance and
expiry, summarizes substantive exchanges as episodic memory, answers simple
questions deterministically through Python, operates the computer through
validated tools, plans and executes durable multi-step tasks with approval,
checkpoints and verification, reads and indexes local PDF, DOCX, TXT,
Markdown, CSV and XLSX documents, answers document questions from retrieved
passages, extracts dates, tables and action items, compares and converts
documents, searches the web and answers only from the fetched evidence with
sources, converts units and text, reports the weather, runs timers and
persisted reminders, falls back to the local model for everything else, and
speaks through an interruptible Windows SAPI voice. Feature set 4.11 is next
(see `PLANS-STACK-FEATURES.md`, `CHANGELOG.md`).

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
| `[tasks]` | Durable task database, step limit, retries, parallel workers and approval |
| `[documents]` | Document database, file/text limits, chunking, retrieval and folder-index limits |
| `[web]` | Search provider, result and query limits, page extraction, evidence budget, cache |
| `[memory]` | Enable memory, its SQLite file, automatic learning, retention and how many memories reach the prompt |
| `[weather]` | Weather flag, metric or imperial units, default location and timeout |

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

### Task planning and execution

Multi-step requests become durable plans instead of one-shot actions. The
clearest built-in workflow is folder organization: `organize my downloads
folder` inspects the folder, groups files by type, proposes the destination
folders, asks for approval, moves the files, verifies each destination and
reports the result. Add `in the background` to run the approved plan without
blocking the conversation.

| Capability | Behaviour |
| --- | --- |
| Planning | The organizer decomposes a folder into inspect, create-folder, move and verify steps |
| Dependencies | A step waits until every prerequisite has completed; skipped or failed prerequisites skip their dependants |
| Conditions | A step can run only when an earlier result matches a field comparison, such as a non-empty folder |
| Execution | Steps run sequentially by default; independent lightweight steps marked parallel use the configured workers |
| Approval | Destructive plans wait for `/confirm`; a step can also require its own approval checkpoint |
| Checkpoints | Task and step state is saved to SQLite after every change, with a visible current step and progress count |
| Retries | A step can retry a configurable number of times before it fails the task |
| Replanning | A failed move is retried with a collision-safe name; custom engines can install a replanner for other recovery |
| Cancellation | `/tasks cancel ID` stops at the next checkpoint; pending approvals can be cancelled with `/cancel` |
| Resumption | Interrupted or failed tasks resume from completed steps with `/tasks resume ID` |
| Verification | A verify step lists the destination and requires every expected file name to be present |
| History | Every plan, approval, attempt, retry, completion and failure is recorded in `task_events` |
| Background | Approved background tasks run in a daemon thread and notify when they finish; approved background plans resume after a restart |

| Control | How |
| --- | --- |
| Plan | `organize my downloads folder`, `organize my downloads folder in the background`, `organize C:/temp/inbox` |
| Durable file plan | `as a task, create a folder named drafts and in that folder write hello to file notes.txt`, `/tasks plan ...` |
| List | `/tasks`, `list tasks`, `what tasks are running`, `task status` |
| Detail | `/tasks show ID`, `show task ID` |
| Approve | `/confirm`, `/tasks approve ID`, `approve task ID` |
| Approve a step | `/tasks approve-step ID` when a step checkpoint pauses the plan |
| Resume | `/tasks resume ID`, `resume task ID` |
| Cancel | `/tasks cancel ID`, `cancel task ID` |
| History | `/tasks history`, `task history` |

Tasks run through the same validated tools and `[tools] root` sandbox as
direct actions, so the folder must be inside that root. Set `root` to your
home folder to organize the real `Downloads` directory. The task database
shares `data/jarvis.db`; task tables are separate from conversation and
memory tables.

```toml
[tasks]
enabled = true
db = "data/jarvis.db"
max_steps = 5000
max_retries = 1
retry_delay_s = 0.2
parallel_workers = 2
approval_required = true
auto_resume = true
history_items = 20
```

`max_steps` also sets the folder-inspection and verification limit, so a
single organization run can handle thousands of files. `auto_resume` restarts
approved background tasks that were still running when JARVIS stopped;
unapproved tasks stay paused until `/confirm`.

### Document intelligence

JARVIS reads local documents without uploading them. Extraction turns PDF,
DOCX, TXT, Markdown, CSV and XLSX content into text and tables; the chunk
index lives next to the other assistant tables in SQLite, and retrieval uses
bundled FTS5 with a scored scan fallback. There is no vector database and no
embedding model in this feature set. Only the retrieved passages, not a whole
document, are sent to the local model for question answering or explanation.

| Capability | Behaviour |
| --- | --- |
| Read | Text and tables from PDF, DOCX, TXT, Markdown, CSV and XLSX; `[Page N]` markers remain in PDF text |
| Index | One file or a whole folder; unchanged files are skipped on a second scan; hidden, model, cache and virtual-environment folders are ignored |
| Search | FTS5 keyword retrieval over overlapping passages, with document title, path and passage number |
| Ask | The model answers only from the retrieved passages and must cite them; when no passage matches, JARVIS says so instead of guessing |
| Summarize | Local extractive summary; no model call and no document text leaves the machine |
| Compare | Line similarity, added/removed lines, and key terms unique to each document |
| Dates and actions | Explicit dates with surrounding context, checkbox/TODO items and bullets under action headings |
| Tables | Extracted cell rows rendered as Markdown, bounded by row and column limits |
| Explain | A model explanation of purpose, structure, terms, decisions and risks using only the document text |
| Convert | Save a document as another supported format; table targets require extracted tables |
| Generate | Create a PDF, DOCX, TXT, Markdown, CSV or XLSX from a request after confirmation |
| Report | Combine summaries, dates and action items from several documents into one report |

| Command | Purpose |
| --- | --- |
| `/documents list` | Show indexed documents, formats and passage counts |
| `/documents index [PATH]` | Index one file or a folder (default `Documents`) |
| `/documents search QUERY` | Search indexed passages |
| `/documents ask QUESTION` | Answer from indexed passages with sources |
| `/documents read PATH` | Show extracted text (the reply is capped) |
| `/documents summarize PATH` | Show a local summary |
| `/documents dates PATH`, `/documents actions PATH` | Extract dated context or action items |
| `/documents tables PATH` | Render extracted tables |
| `/documents explain PATH` | Explain a technical document through the local model |
| `/documents compare A and B` | Compare two documents (a pipe separator also works) |
| `/documents convert SOURCE` | Convert to a target path or format |
| `/documents create PATH` | Create a document directly |
| `/documents report [OUTPUT]` | Build a report from documents |
| `/documents remove PATH` | Remove one document from the index (the file stays) |
| `/documents last` | Show the last document used |

For `convert`, `create` and `report`, separate additional arguments with a
vertical bar (`|`); `compare` also accepts `and` between the two paths.

Natural requests also work: `index report.pdf`, `search documents for budget`,
`ask documents about revenue`, `summarize report.pdf`, `find dates in
report.pdf`, `list action items from report.pdf`, `show tables from
report.pdf`, `compare a.pdf and b.pdf`, `convert report.pdf to docx`, `create a
pdf document named weekly.pdf about the budget`, and `generate a report from
a.pdf and b.pdf`. File writing, conversion and generation still ask for
confirmation. Paths stay inside `[tools] root`.

```toml
[documents]
enabled = true
db = "data/jarvis.db"
max_file_bytes = 20000000
max_chars = 2000000
chunk_chars = 900
chunk_overlap = 120
search_limit = 5
context_chars = 4000
max_files = 500
max_chunks = 20000
```

`max_file_bytes` and `max_chars` bound extraction; `chunk_chars` and
`chunk_overlap` control passage boundaries; `context_chars` is the evidence
budget for document answers; `max_files` and `max_chunks` bound folder
indexing. Increasing these raises RAM and request size on the 8 GB target.

### Memory

Memory is layered rather than a transcript. Explicit memories are exactly what
you ask JARVIS to remember. A deterministic learner also keeps a small set of
high-confidence first-person statements (`my name is Alex`, `I prefer dark
mode`, `I work at Acme`, `my projects folder is C:/dev`) as **learned**
semantic memories, with a confidence score and an expiry date. Substantive
exchanges become one-line **episodic** summaries, and a bounded session buffer
reconstructs the current conversation. Questions, commands, greetings and
assistant-directed text (`I like you`) are never stored, and the conversation
is never saved wholesale as permanent memory.

| Layer | Contents | Lifetime |
| --- | --- | --- |
| Short-term | recent exchanges in this session (`short_term_items`, 6) | session only |
| Semantic | explicit and learned preferences, facts, paths and folders | explicit: permanent; learned: `learned_ttl_days` (180) |
| Episodic | summaries of substantive requests with keywords | `episodic_ttl_days` (30), max `episodic_items` (200) |
| Long-term | semantic and episodic rows in `data/jarvis.db` | across restarts |

| Action | How |
| --- | --- |
| Save | `remember my name is Alex`, `remember the office is in Kolkata`, `remember this folder is C:/dev` |
| Temporary | `remember for now that the meeting is at five` (deleted when JARVIS restarts) |
| Learn | `my name is Alex`, `I prefer dark mode`, `I work at Acme`, `my projects folder is C:/dev` (automatic, high confidence only) |
| Inspect | `what do you remember`, `show your memories`, `/memory` |
| Learn list | `what have you learned`, `/memory learned` |
| Episodes | `what did we talk about`, `/memory episodes`, then `/memory episodes clear confirm` |
| Explain | `/memory explain ID` — source, category, importance, confidence, expiry and replacement |
| Search | `what do you remember about Alex`, `search your memory for Kolkata`, `/memory search TEXT` |
| Edit | `/memory edit ID new text` |
| Delete | `forget memory about Kolkata`, `/memory delete ID` |
| Bulk delete | `/memory clear` then `/memory clear confirm`; `/memory clear temporary` |
| Frequent | `/memory frequent [app\|folder\|website]`, fed by app launches, opened folders and URLs |

When two memories describe the same stable attribute — `name`, `residence`,
`employer`, `job`, `age`, `birthday`, `favorite:<thing>` or a named folder —
the older row is expired and linked through `superseded_by` instead of both
competing in the prompt. The history stays available for transparency.
Automatic learning never overwrites an explicit memory; use `remember ...` to
correct it, which also records the old value as superseded.

Categories are inferred (`preference`, `fact`, `path`, `folder`, `website`,
`task`, `general`) and can be set explicitly with `/memory add category text`.
Importance is scored from 1 to 5, and retrieval ranks by term overlap,
importance, recency and a preference boost. The best `inject_items` memories
and the two most relevant episodes are combined into a bounded prompt block.
Memories live in `data/jarvis.db` next to the conversation history, with SQLite
FTS5 search when the bundled SQLite provides it and a scored scan otherwise.
Duplicate persistent memories are refreshed instead of duplicated.

```toml
[memory]
enabled = true
db = "data/jarvis.db"
inject_items = 6
# Learn stable first-person facts; questions and commands are ignored.
auto_learn = true
learn_min_confidence = 0.75
# Learned semantic memories expire; explicit memories do not.
learned_ttl_days = 180
# Episodic summaries of substantive exchanges and the session buffer.
episodic_ttl_days = 30
episodic_items = 200
short_term_items = 6
```

### Utilities, weather and timers

Small everyday requests are handled by Python instead of the model, so they
answer instantly and the same way every time.

| Area | Examples |
| --- | --- |
| Unit conversion | `convert 10 km to miles`, `convert 25 celsius to fahrenheit`, `how many pounds in 5 kg` — length, mass, volume, speed, data, time and temperature |
| Text | `uppercase hello world`, `title case the text ...`, `reverse ...`, `how many words in ...` |
| File sizes | `how big is 1048576 bytes` — binary KB, MB, GB and TB |
| Weather | `what's the weather in Kolkata`, `weather in London` — current conditions, feels-like, humidity, wind and today's high/low through Open-Meteo (no API key) |
| Timers | `set a timer for 5 minutes`, `set a timer for 10 minutes named pasta`, `list timers`, `cancel timer 2` |
| Reminders | `remind me in 20 minutes to stretch`, `remind me to call mom at 5 pm`, `remind me to submit the report tomorrow at 9` |

Timers live for the session; reminders are written to `data/jarvis.db` and
survive a restart. When one is due, JARVIS prints and speaks the notification
through the same voice used for replies, and a reminder that came due while
JARVIS was off is reported on the next start.

```toml
[weather]
enabled = true
units = "metric"      # or "imperial" for °F and mph
default_location = ""  # used when the question names no city
timeout_s = 10.0
```

### Internet search

Questions that need current information are routed to the search subsystem in
`web/`. The model never sees the open internet: it receives a numbered evidence
block and may only answer from it.

| Step | Behaviour |
| --- | --- |
| Query variants | The question plus a keyword form (`max_queries`, default 2), capped and deduplicated |
| Providers | `ddgs`/DuckDuckGo by default; Brave when `BRAVE_API_KEY` is set; an optional self-hosted SearXNG via `[web] searx_url`; Wikipedia through the MediaWiki API for `wikipedia X` |
| Ranking | Duplicate URLs merged, results sharing no term with the question dropped, title matches ranked first; news questions also prefer recently published results |
| Extraction | Up to `extract_pages` readable pages are fetched (extra candidates are tried when a site blocks the fetch) and reduced to main text (`trafilatura` if installed, otherwise `lxml`); each source contributes the passage that best matches the question |
| Budget | At most `context_chars` (1000, roughly 250 tokens) of evidence reaches the model; the retry after an ungrounded answer uses double that |
| Grounding | The answer must cite an existing source, and its figures or words must appear in the evidence; one retry is allowed, then JARVIS says the sources did not answer instead of guessing |
| Sources | The reply ends with a numbered `Sources:` list |
| Cache | Results cached in `data/search_cache.db` for `cache_ttl_s`; stale entries (up to 7 days) are used when the network fails |

Ask with `search the web for …`, `look up …`, `google …`, `find information
about …`, `latest news about …` (dated news results), `wikipedia …` (the
article introduction from the MediaWiki API, not a search hit), or explicitly
with `/web QUESTION`. A plain `search for notes in drafts` still searches
files, not the web. Without a network connection the reply is a clear failure message, never
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
  the detected intent as JSON, `/tools` lists computer tools, `/documents`
  manages local document indexing, search and generation, `/confirm` and
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
web search ranking, caching and grounded answers, explicit memory storage,
search and prompt injection, automatic candidate extraction and importance,
conflict superseding and history, ranked retrieval, expiry, episodic
summaries and controls, short-term session context, auto-learning disabled,
durable task planning, dependencies, conditions, approvals, retries,
dynamic replanning, parallel and background execution, cancellation,
resumption, verification and folder organization, PDF/DOCX/TXT/Markdown/CSV/
XLSX extraction and tables, document chunking, FTS5 search and scan fallback,
folder indexing and unchanged-file skipping, summaries, dates, action items,
comparison, conversion, report and document generation, document commands and
tool confirmations, unit conversions, text and size formatting,
Open-Meteo response parsing, timer polling and reminder persistence, Escape
interruption, VAD segmentation,
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
core/memory.py          Layered memory store, FTS5 search, episodes, ranking
core/learning.py        Candidate extraction, importance, episodes, short-term buffer
core/tasks.py           Durable plans, execution, approval, checkpoints, history
core/documents.py       Extraction, chunking, FTS5 index, retrieval and generation
tools/documents.py      Document tool handlers for the validated registry
core/utilities.py       Unit conversion, text, size, duration and reminder parsing
core/timers.py          In-memory timers, persisted reminders and notifications
core/weather.py         Open-Meteo geocoding and current conditions
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
- Multi-step file plans cover explicit file actions only (create, write,
  read, list, search, rename, copy, move, delete). A request that needs
  follow-up reasoning, such as "write the file location in it", is not
  planned: the sequence stops at that clause and the confirmation shows what
  will run. Organization and `as a task` / `/tasks plan` requests become
  durable tasks; open-ended reasoning still goes to the model.
- Document extraction reads text and tables, not images. Scanned PDFs without
  a text layer, charts, embedded images and Office drawing objects are not
  OCRed or described. PDF generation is text-only with Helvetica and Latin-1
  characters; complex layout, fonts and non-Latin scripts are not reproduced.
  Document search is lexical FTS5 with no embeddings, so a question that uses
  different words from the document can miss a relevant passage. Indexing is
  bounded by `max_files` and `max_chunks`, and CSV/XLSX conversion requires
  extracted tables. DOCX/XLSX/PDF libraries are imported only when a matching
  file is opened; opening the document service added about 5 MiB RSS and the
  three libraries together added about 30 MiB when first imported on this
  machine.
- Organization is capped at `[tasks] max_steps` (5000 by default) and
  inspects and verifies at most that many items. A single run therefore covers
  thousands of files, but a folder above the cap still has to be split.
- Task steps stay inside `[tools] root`. Dynamic replanning handles move-name
  collisions, and recovery treats an existing destination folder or an
  already-completed move as success. Other step failures stop the task and
  require `/tasks resume ID` after the cause is fixed.
- An approved background task resumes after a restart when `auto_resume` is
  true; a task waiting for approval stays paused until it is confirmed.
- Web search still reads at most three sources and 1000 characters of
  evidence (2000 on the retry), so a fact split across several pages can be
  missed. Pages behind a login or a CAPTCHA cannot be read at all: such a
  source is labelled `(snippet only)` and other candidates are fetched
  instead, but its snippet is still what the answer can use. DuckDuckGo
  scraping can break without notice - set `searx_url` for a self-hosted
  SearXNG, or `BRAVE_API_KEY` for a supported API. The Wikipedia lookup sends
  a descriptive User-Agent because Wikimedia blocks anonymous ones.
- Memory matching is lexical (no embeddings) and automatic learning covers a
  narrow set of first-person patterns; unusual phrasing is missed rather than
  guessed. Learned facts expire after 180 days and episode summaries after 30,
  older conflicts remain only as superseded history, duplicate detection is
  exact text, and there is still a single user profile with no per-project
  separation.
- Frequent-usage counts cover apps, folders and URLs that went through the
  tools; a website opened in a browser by hand is not counted.
- Unit conversion covers the built-in tables (length, mass, volume, speed,
  data, time and temperature); unusual units fall back to the model. Weather
  needs an internet connection and Open-Meteo availability. Timers fire only
  while JARVIS is running; reminders catch up at the next start.
- Summarization costs one extra model call once the transcript grows past
  `summarize_after`, which adds a few seconds on this CPU.
- Speech recognition is English-only and limited by the small Vosk model:
  long sentences, names and file extensions transcribe poorly, and a short
  command is the reliable form. The lgraph model is not a win on this CPU
  (measured above). Set a specific `input_device` if the microphone picks up
  background noise, and lower `vad_aggressiveness` to keep quiet syllables
  that level 2 drops.
- Web search in `core/tools.py` is the legacy scraper; it returns unreliable
  results and is no longer used by `main.py`. The supported search lives in
  `web/`.
- Generation runs at ~5 tok/s on this CPU, so a 250-token detailed answer
  takes about 50 seconds; a reply that hits its limit is continued once,
  not generated without bound.
- Speech output is verified functionally (SAPI speaks, interruption at ~0.8 s),
  but audio quality and volume must be confirmed by ear on the target machine.
