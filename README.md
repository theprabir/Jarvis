# JARVIS

A local-first, voice-and-text personal AI assistant for Windows 11, built
around a small local LLM (Llama 3.2 1B Instruct Q4_K_M) served by the existing
`llama-server.exe` from llama.cpp. The model is the language interface;
deterministic Python modules do the audio, routing and tool work.

**Current status:** Phase 1 (stabilization) and feature sets **4.1 Voice
Interface** through **4.13 Advanced Web Research** complete, plus optional
document OCR with confidence warnings, released as **v0.2.9** in the
moderate-advanced feature line. Phase 4.12 Developer Assistant provides local
source, search, diagnostics, documentation, Git and confirmed execution tools.
JARVIS listens through the
microphone with push-to-talk, transcribes offline with Vosk, keeps multi-turn
context across restarts, remembers explicitly saved preferences, facts, paths
and folders, learns high-confidence first-person facts with provenance and
expiry, summarizes substantive exchanges as episodic memory, answers simple
questions deterministically through Python, operates the computer through
validated tools, plans and executes durable multi-step tasks with approval,
checkpoints and verification, reads and indexes local PDF, DOCX, TXT,
Markdown, CSV and XLSX documents, OCRs scanned PDFs and images, answers
document questions from retrieved
passages, extracts dates, tables and action items, compares and converts
documents, searches the web and answers only from the fetched evidence with
sources, converts units and text, reports the weather, runs timers and
persisted reminders, falls back to the local model for everything else, and
speaks through an interruptible Windows SAPI voice. The 4.11 productivity
suite adds local to-dos, deadlines, recurring tasks,
validated to-do tools, deterministic date parsing and daily or weekly
planning, local notes with quick capture and full-text search, daily habits
with streaks and recurring reminders, goals with numeric progress and target
dates, local time tracking with daily totals, local calendar events with
daily agendas, local email drafts with optional connected-account search and
sending, Pomodoro work/break phases, bounded clipboard history, and daily or
meeting summaries (see
`PLANS-STACK-FEATURES.md`, `CHANGELOG.md`).

## Requirements

- Windows 11, 64-bit Python (3.11+)
- `llama-server.exe` on `PATH` (existing llama.cpp installation; do **not**
  replace it with `llama-cpp-python`)
- `models/Llama-3.2-1B-Instruct-Q4_K_M.gguf`
- A microphone and speakers; speech output needs a Windows SAPI voice
  (Microsoft David or Zira ship with Windows)
- OCR needs Windows and an installed Windows OCR language pack; `en-US` is
  available on most English installations
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
| `[productivity]` | Local to-do database, default priority and list limits |
| `[notes]` | Local note database, content limits and search result limits |
| `[habits]` | Daily habit database and habit-count limit |
| `[goals]` | Goal database and goal-count limit |
| `[time]` | Time-entry database and time-entry limit |
| `[calendar]` | Calendar database, event limit and default event duration |
| `[email]` | Draft database, account servers, username, password env var and mail limits |
| `[pomodoro]` | State database, work, break and long-break durations and cycle count |
| `[clipboard]` | Clipboard history database, size, age and search limits |
| `[developer]` | Read-only source/project scan limits |
| `[documents]` | Document database, limits, chunking, retrieval, folder indexing and optional OCR |
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

### Productivity to-dos

Productivity items are deliberately separate from durable execution plans.
`/tasks` continues to mean multi-step file and tool plans; `/todo` manages
local to-dos, deadlines, priorities, reminders and recurrence in the
`todo_items` table. To-do reminders use the existing timer scheduler, so no
second background scheduler is started.

| Capability | Behaviour |
| --- | --- |
| Add | `/todo add TEXT [due WHEN] [priority LEVEL] [repeat daily\|weekly\|monthly]` |
| List | `/todo list`, `/todo all`, or `what's on my todo list` |
| Detail | `/todo show ID` |
| Complete | `/todo done ID`, `mark todo ID done`, `complete todo ID` |
| Change | `/todo edit ID title\|due\|remind\|priority\|repeat VALUE` |
| Snooze | `/todo snooze ID [MINUTES]` |
| Remove | `/todo remove ID` |
| Daily plan | `/plan`, `/plan today`, `/plan tomorrow`, or `plan my day` |
| Weekly plan | `/plan week`, `/plan this week`, `/plan next week`, or `plan my week` |

Natural requests also work: `add a to-do to call mom tomorrow at 5pm`,
`add a to-do to pay rent due Friday repeat monthly`, and `snooze todo 2 for
10 minutes`. Natural-language removal such as `remove todo 2` validates the
item, asks for confirmation and runs through the validated `todo_delete` tool;
`/todo remove ID` remains an immediate explicit command. Priority accepts
`low`, `normal`, `high` or `critical`. A due
date creates an automatic reminder at that time; an explicit
`remind me ...` phrase can move the reminder earlier or later. Recurring
to-dos require a due date, and completing one creates exactly one next
occurrence with a link to the previous item. Reminders and to-dos survive a
restart, and a reminder that came due while JARVIS was off is reported at the
next start.

The deterministic date parser understands `tomorrow morning`,
`this afternoon`, `tonight`, `noon`, `midnight`, `next week`,
`next month`, `this weekend`, `end of day/week/month`, `November 20`,
`the 20th`, `two weeks from today` and relative durations such as
`in two hours`. A bare clock hour is interpreted as afternoon or evening
(`at 5` means 17:00); say `5am` for the morning. Unrecognized phrases stay in
the title instead of being guessed.

The same items are exposed to durable plans and model tool calls through the
validated `todo_add`, `todo_list`, `todo_show`, `todo_update`, `todo_complete`,
`todo_snooze` and `todo_delete` tools. They use the assistant's single lazy
`ProductivityService`, so tool calls and `/todo` never open separate databases
or scheduler threads. `todo_delete` requires confirmation; the other to-do
tools are safe to run directly.

Planning is read-only and deterministic. `/plan` groups open to-dos into
overdue, today, unscheduled and upcoming work; `/plan week` covers the next
seven days, with overdue, unscheduled and later items kept separate. The
`plan_day` and `plan_week` tools expose the same agendas to durable plans and
model tool calls. Planning never creates deadlines or changes an item.

```toml
[productivity]
enabled = true
db = "data/jarvis.db"
default_priority = "normal"
max_items = 2000
```

The current milestone covers local to-dos, deadline reminders, validated
to-do tools, deterministic date parsing, daily or weekly planning and local
notes, daily habits with streaks and reminders, goals with numeric progress
and target dates, local time tracking with daily totals, local calendar
events with daily agendas, local email drafts with optional connected-account
search, reading, summarization and sending, timer-backed Pomodoro phases,
bounded clipboard history, and deterministic daily and meeting summaries.

### Notes

Notes are local records in their own `notes` table, separate from to-dos,
memories and conversation history. Capture works from typed or spoken text,
so a quick note can be saved without waiting for the model.

| Capability | Behaviour |
| --- | --- |
| Add | `/notes add TEXT`, or `/notes add TITLE \| TEXT` |
| Quick capture | `note that ...`, `take a note ...`, `note: ...`, `quick note ...`, `jot down ...` |
| List | `/notes`, `/notes list`, `list my notes`, `what are my notes` |
| Detail | `/notes show ID`, `show note ID` |
| Search | `/notes search QUERY`, `search notes for QUERY`, `notes about QUERY` |
| Change | `/notes edit ID content\|title VALUE` |
| Remove | `/notes remove ID`; natural `remove note ID` asks for confirmation |

Search uses SQLite FTS5 over note titles and content, with a scored scan
fallback when FTS5 is unavailable. Results include a short matching excerpt.
The `note_add`, `note_list`, `note_show`, `note_search`, `note_update` and
`note_delete` tools expose the same service to durable plans and model tool
calls; `note_delete` requires confirmation.

```toml
[notes]
enabled = true
db = "data/jarvis.db"
max_items = 5000
max_content_chars = 10000
search_limit = 10
```

### Habits

Habits are daily records in their own `habits` and `habit_checkins` tables.
There is one check-in per local day, so a repeated check-in is idempotent.
Current streaks count consecutive days ending today, or ending yesterday when
today has not been checked in yet; best streaks are kept separately.

| Capability | Behaviour |
| --- | --- |
| Add | `/habit add NAME [at TIME]`, or `add a habit to read every day at 9pm` |
| List | `/habit list`, `list my habits`, `what are my habits` |
| Detail | `/habit show ID`, `show habit ID` |
| Check in | `/habit done ID`, `habit ID done`, `complete habit ID` |
| Undo today | `/habit undo ID`, `undo habit ID` |
| Stats | `/habit stats`, `show my habit streaks` |
| Change | `/habit edit ID name\|remind VALUE`, or `/habit edit ID clear reminder` |
| Remove | `/habit remove ID`; natural `remove habit ID` asks for confirmation |

An optional reminder time creates one persisted daily reminder through the
existing timer scheduler. It fires while JARVIS is running; after a restart
the next occurrence is restored, and a missed occurrence is rolled forward
instead of delivered as a flood. Deleting a habit removes its check-in history
and pending reminder. The `habit_add`, `habit_list`, `habit_show`,
`habit_complete`, `habit_undo`, `habit_update` and `habit_delete` tools expose
the same service to durable plans and model tool calls; `habit_delete`
requires confirmation.

```toml
[habits]
enabled = true
db = "data/jarvis.db"
max_habits = 200
```

### Goals

Goals are local records in their own `goals` table with an optional numeric
target, a unit and a target date. Numeric progress is independent of habit
check-ins and to-do completion; a goal can also stay open-ended with no number.

| Capability | Behaviour |
| --- | --- |
| Add | `/goal add TEXT [target N UNIT] [due WHEN]`, or `add a goal to read 12 books by December 31` |
| List | `/goal list`, `/goal list completed`, `/goal list all`, `list my goals` |
| Detail | `/goal show ID`, `show goal ID` |
| Progress | `/goal progress ID VALUE`, `goal ID progress VALUE` |
| Increment | `/goal increment ID VALUE`, `add 2 to goal ID` |
| Complete | `/goal complete ID`, `complete goal ID` |
| Reopen | `/goal reopen ID`, `reopen goal ID` |
| Change | `/goal edit ID name\|target\|unit\|due VALUE`, or `/goal edit ID clear due` |
| Remove | `/goal remove ID`; natural `remove goal ID` asks for confirmation |

Active goals are listed before completed goals. Target dates show days left,
`today`, or how many days overdue; target dates do not create notifications.
The `goal_add`, `goal_list`, `goal_show`, `goal_progress`, `goal_complete`,
`goal_reopen`, `goal_update` and `goal_delete` tools expose the same service to
durable plans and model tool calls. `goal_delete` requires confirmation.

```toml
[goals]
enabled = true
db = "data/jarvis.db"
max_goals = 500
```

### Time tracking

Time entries are local records in their own `time_entries` table. Only one
entry can be active at a time, and elapsed time is derived from persisted
timestamps rather than a background timer. Daily totals split entries that
cross midnight between the two local dates.

| Capability | Behaviour |
| --- | --- |
| Start | `/time start writing`, or `start tracking writing` |
| Stop | `/time stop`, or `stop tracking` |
| Status | `/time status`, `what am I tracking` |
| Daily totals | `/time today`, `/time yesterday`, `/time report DATE`, `how much time did I spend today` |
| Weekly totals | `/time week` |
| Entries | `/time list`, `list my time entries` |
| Remove | `/time remove ID`; natural `remove time entry ID` asks for confirmation |

The `time_start`, `time_stop`, `time_status`, `time_report`, `time_week`,
`time_list` and `time_delete` tools expose the same service to durable plans
and model tool calls. `time_delete` requires confirmation; the other time
tools are safe. Reports use the computer's local date.

```toml
[time]
enabled = true
db = "data/jarvis.db"
max_entries = 20000
```

### Calendar

Calendar events are local records in their own `calendar_events` table,
separate from to-dos, notes and time entries. Each event has a title, optional
details, a start date and time, and a duration in minutes; the daily agenda
lists events whose start time falls on the requested local date.

| Capability | Behaviour |
| --- | --- |
| Add | `/calendar add TITLE at WHEN [for DURATION] [\| DETAILS]`, or `add a calendar event for lunch tomorrow at noon` |
| Today | `/calendar today`, `what's on my calendar today` |
| Tomorrow | `/calendar tomorrow`, `what's on my agenda tomorrow` |
| Agenda date | `/calendar agenda DATE` |
| Upcoming | `/calendar list`, `/calendar upcoming` |
| Detail | `/calendar show ID`, `show calendar event ID` |
| Change | `/calendar edit ID title\|start\|duration\|details VALUE` |
| Remove | `/calendar remove ID`; natural `remove calendar event ID` asks for confirmation |

Durations default to 60 minutes and must be between 1 and 1440 minutes. The
`calendar_add`, `calendar_agenda`, `calendar_list`, `calendar_show`,
`calendar_update` and `calendar_delete` tools expose the same lazy service to
durable plans and model tool calls. `calendar_delete` requires confirmation;
the other calendar tools are safe. Agenda dates and event times use the
computer's local date and time.

```toml
[calendar]
enabled = true
db = "data/jarvis.db"
max_events = 5000
default_duration_minutes = 60
```

### Email

Email drafts are local records in their own `email_drafts` table. Drafting,
listing, editing, deleting and reply drafting work without an account. Remote
search, reading and sending are enabled only when an IMAP or SMTP server is
explicitly configured in `[email]`; the password is read from the environment
variable named by `password_env`, never from the TOML file.

| Capability | Behaviour |
| --- | --- |
| Draft | `/email draft RECIPIENT [about SUBJECT] [\| SUBJECT \| BODY]`, or `draft an email to sam@example.com about lunch` |
| List | `/email list`, `list my email drafts` |
| Detail | `/email show ID`, `show email draft ID` |
| Change | `/email edit ID to\|cc\|subject\|body VALUE` |
| Remove | `/email remove ID`; natural `remove email draft ID` asks for confirmation |
| Send | `/email send ID`, `send email draft ID`; sending always asks for confirmation |
| Search | `/email search QUERY`, `search my email for invoice` |
| Read | `/email read UID`, `read email UID` |
| Summarize | `/email summarize UID`, `summarize email UID` |
| Reply | `/email reply UID`, `reply to email UID` creates a local reply draft |

The `email_draft`, `email_list`, `email_show`, `email_update`, `email_delete`,
`email_send`, `email_search`, `email_read`, `email_summarize` and
`email_reply` tools expose the same service to durable plans and model tool
calls. `email_delete` and `email_send` require confirmation. Search and
summaries are extractive and never send message content to the model.

```toml
[email]
enabled = true
db = "data/jarvis.db"
max_drafts = 1000
from_address = ""
username = ""
password_env = "JARVIS_EMAIL_PASSWORD"
imap_host = ""
imap_port = 993
imap_ssl = true
smtp_host = ""
smtp_port = 587
smtp_ssl = false
smtp_starttls = true
mailbox = "INBOX"
search_limit = 10
timeout_s = 20.0
max_body_chars = 20000
```

Set the password in the environment before starting JARVIS:

```bat
set JARVIS_EMAIL_PASSWORD=...
.venv\Scripts\python main.py
```

### Pomodoro, clipboard history and summaries

Pomodoro phases reuse the existing timer scheduler: no second thread or
service is created. A work phase is followed by a break, and every configured
cycle ends with a longer break. The active phase, due time and completed work
count are persisted in SQLite, so a session resumes with its remaining time
after JARVIS restarts. Elapsed phases are advanced instead of being replayed.

| Capability | Behaviour |
| --- | --- |
| Start | `/pomodoro start [MINUTES]`, `start a pomodoro` |
| Status | `/pomodoro status`, `pomodoro status` |
| Stop | `/pomodoro stop`, `stop the pomodoro` |
| Skip phase | `/pomodoro skip`, `skip the pomodoro` |
| Work/breaks | `[pomodoro] work_minutes`, `break_minutes`, `long_break_minutes`, `cycles_before_long_break` |

Clipboard copies are recorded in the local `clipboard_entries` table when the
`clipboard_copy` tool is used. History is bounded by item count, character
size and age; older entries are pruned automatically. The history never
stores passwords by design, so do not copy secrets into the clipboard.

| Capability | Behaviour |
| --- | --- |
| History | `/clipboard list`, `show clipboard history` |
| Search | `/clipboard search QUERY`, `search clipboard for TEXT` |
| Detail | `/clipboard show ID`, `show clipboard entry ID` |
| Remove | `/clipboard remove ID`, `remove clipboard entry ID`; asks for confirmation |
| Clear | `/clipboard clear`, `clear clipboard history`; asks for confirmation |

Daily summaries combine open to-dos, calendar events, tracked time and habit
check-ins. Meeting summaries are extractive summaries of supplied notes;
neither action calls the model or network.

| Capability | Behaviour |
| --- | --- |
| Daily | `/summary`, `/summary daily [DATE]`, `summarize my day` |
| Meeting | `/summary meeting TITLE \| NOTES`, `summarize meeting TITLE \| NOTES` |

```toml
[pomodoro]
enabled = true
db = "data/jarvis.db"
work_minutes = 25
break_minutes = 5
long_break_minutes = 15
cycles_before_long_break = 4
end_after_cycles = 0

[clipboard]
enabled = true
db = "data/jarvis.db"
max_items = 500
max_chars = 10000
retention_days = 30
search_limit = 10
```

### Developer assistant

Phase 4.12 is a local, bounded developer assistant. Source explanation uses
the standard `ast` module; search and symbol lookup cover the sandbox; error
analysis, test-failure interpretation, snippets and documentation generation
are deterministic and work offline. Git operations are read-only and only
suggest commit messages — they never commit. Formatting, documentation writes,
templates, test runs and script execution require an explicit confirmation.
Paths stay inside `[tools] root`; subprocesses use no shell and a sanitized
environment, with timeouts and output limits.

| Capability | Behaviour |
| --- | --- |
| Explain | `/dev explain PATH`, `explain source PATH` |
| Inspect | `/dev inspect [PATH]`, `/dev architecture [PATH]` |
| Search | `/dev search QUERY [\| PATH]`, `search code for QUERY` |
| Symbols | `/dev find NAME [\| PATH]`, `find class NAME` |
| Snippets | `/dev snippet python\|javascript\|typescript [\| TEXT]` |
| Errors | `/dev debug ERROR`, `/dev terminal ERROR`, `/dev failures TEXT` |
| Docs | `/dev docs [PATH \| OUTPUT]`, `/dev readme [PATH \| OUTPUT]`, `/dev docsearch QUERY` |
| Tests | `/dev tests [PATH]`, `run tests` |
| Format | `/dev format PATH` (confirmation required) |
| Git | `/dev git status\|log [N]\|changes\|message` |
| Practice | `/dev template KIND NAME [PATH]`, `/dev run PATH [ARGS]` |
| Tools | `developer_*` validated tools; writes and execution are `confirm` |

```toml
[developer]
enabled = true
max_files = 500
max_depth = 4
max_read_bytes = 200000
max_results = 50
max_output_chars = 20000
test_timeout_s = 120.0
script_timeout_s = 30.0
allow_execution = true
```

### Document intelligence

JARVIS reads local documents without uploading them. Extraction turns PDF,
DOCX, TXT, Markdown, CSV and XLSX content into text and tables; the chunk
index lives next to the other assistant tables in SQLite, and retrieval uses
bundled FTS5 with a scored scan fallback. Optional OCR uses the built-in
Windows OCR engine for scanned PDF pages and PNG, JPG, BMP, TIFF or WebP
images. There is no vector database and no embedding model in this feature
set. Only the retrieved passages, not a whole document, are sent to the local
model for question answering or explanation.

Windows OCR does not expose a native confidence score. JARVIS therefore shows
a clearly labeled **local estimate** based on how much text was recognized,
how much of the page its word boxes cover, and how clean the tokens are. A
`fair` or `partial` result, or an OCR error on a page, produces a warning in
`/documents read` and in index results; the original image is never modified.

| Capability | Behaviour |
| --- | --- |
| Read | Text and tables from PDF, DOCX, TXT, Markdown, CSV and XLSX; `[Page N]` markers remain in PDF text |
| OCR | Scanned PDF pages and image files when `ocr_enabled` is on; PDF pages are rendered at `ocr_dpi`, and only pages with fewer than `ocr_min_chars` characters are OCRed |
| OCR confidence | A local quality estimate from recognized text volume, page coverage and token cleanliness; low-confidence pages and images are flagged for review |
| Index | One file or a whole folder, including OCRable images when enabled; unchanged files are skipped on a second scan; hidden, model, cache and virtual-environment folders are ignored |
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
ocr_enabled = true
ocr_language = "en-US"
ocr_dpi = 200
ocr_min_chars = 40
ocr_max_pages = 20
```

`max_file_bytes` and `max_chars` bound extraction; `chunk_chars` and
`chunk_overlap` control passage boundaries; `context_chars` is the evidence
budget for document answers; `max_files` and `max_chunks` bound folder
indexing. `ocr_enabled` turns image reading and scanned-page OCR on or off;
`ocr_language` must match an installed Windows OCR language pack (usually
`en-US`), and `ocr_max_pages` limits OCR work per PDF. Increasing these raises
RAM, CPU time and request size on the 8 GB target.

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

### Advanced research

`/research QUESTION` runs a multi-query investigation over the configured
search providers. Reports are deterministic and local: they preserve every
source, extract numeric and question-specific facts, compare coverage and
publication dates, flag conflicting figures, assess freshness and generate
numbered citations. Each report is stored in `data/research.db` so it can be
reopened without another search. No extra model or dependency is loaded.

| Command | Purpose |
| --- | --- |
| `/research QUESTION` | Run a multi-query report |
| `/research compare QUESTION` | Compare sources and coverage |
| `/research docs QUESTION` | Documentation-focused search |
| `/research domain DOMAIN QUESTION` | Restrict results with `site:DOMAIN` |
| `/research providers` | Show the active provider chain |
| `/research history [N]` | List recent reports |
| `/research search TEXT` | Search stored reports |
| `/research saved` | List explicitly saved reports |
| `/research show ID` | Reopen a stored report and its sources |
| `/research save ID [TITLE]` | Mark a report as saved |
| `/research remove ID` | Delete one stored report |

Natural forms include `research …`, `compare sources on …`, `find
documentation for …`, `research example.com for …`, `show my research
history` and `show saved research`.

```toml
[web]
research_db = "data/research.db"
research_entries = 200
research_retention_days = 180
research_max_queries = 3
research_max_sources = 6
```

Unsaved history is pruned by age and count; saved reports remain. Research
reuses the search cache, provider fallback, article extraction and the same
read-only network path as ordinary web search. Contradiction detection is a
heuristic over extracted figures, so it points to claims worth checking; it
is not a verdict that one source is wrong.

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
  manages local document indexing, search and generation, `/research` builds
  multi-source reports with history, `/confirm` and
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
folder indexing and unchanged-file skipping, OCR of images and image-only
PDFs, OCR confidence estimates and partial-recognition warnings, OCR
disabling, summaries, dates, action items, comparison, conversion, report and
document generation, document commands and tool confirmations, unit conversions, text and size formatting,
email draft validation and optional IMAP/SMTP flows, Open-Meteo response parsing,
timer polling and reminder persistence, Escape
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
core/research.py        Multi-query reports, facts, comparison and history store
tests/test_research.py  Research, report, store, domain and assistant coverage
core/memory.py          Layered memory store, FTS5 search, episodes, ranking
core/learning.py        Candidate extraction, importance, episodes, short-term buffer
core/tasks.py           Durable plans, execution, approval, checkpoints, history
core/notes.py           Local notes, FTS5 search, scan fallback and formatting
tools/notes.py          Note tool handlers for the validated registry
core/habits.py          Daily habits, streaks, parsing and reminder scheduling
tools/habits.py         Habit tool handlers for the validated registry
core/goals.py           Goals, numeric progress, target dates and formatting
tools/goals.py          Goal tool handlers for the validated registry
core/time_tracking.py   Active entries, elapsed time, daily and weekly totals
tools/time_tracking.py  Time tool handlers for the validated registry
core/calendar.py        Local events, daily agendas, parsing and formatting
tools/calendar.py       Calendar tool handlers for the validated registry
core/mail.py            Local drafts, optional IMAP search, SMTP sending and summaries
tools/mail.py           Email tool handlers for the validated registry
core/pomodoro.py        Work/break phases over the shared timer service
tools/pomodoro.py       Pomodoro tool handlers
core/clipboard_history.py Bounded clipboard entries and search
tools/clipboard_history.py Clipboard-history tool handlers
core/summaries.py       Daily and meeting summary composition
tools/summaries.py      Summary tool handlers
core/developer.py       Developer search, diagnostics, docs, Git and sandbox execution
tools/developer.py      Developer tool handlers
core/documents.py       Extraction, OCR, chunking, FTS5 index, retrieval and generation
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
- Note search is lexical FTS5 with no embeddings; a search that uses
  different words from the note can miss it. Notes are not versioned, and
  deleting one is permanent after confirmation.
- Habits are daily-only and keep one check-in per local date. Weekly targets,
  pauses, multiple check-ins per day and custom time zones are not supported.
  Streaks use the computer's local date, so travel can shift a day boundary.
  A reminder fires only while JARVIS is running; after a restart the next daily
  occurrence is scheduled, and an old occurrence is skipped rather than
  delivered late.
- Goals have one numeric progress value and one target date. Sub-goals,
  milestones, recurring goals, target-date notifications and time-zone-aware
  deadlines are not supported. Target dates use the computer's local date.
- Time tracking keeps one active entry at a time and uses the computer's local
  date. It does not detect idle time, edit past entry boundaries, assign
  billable rates or split an active entry manually. Reports include only
  entries that overlap the requested day.
- Calendar events are local-only. There is no external calendar sync, OAuth,
  attendee or invitation support, recurrence, time-zone handling or event
  reminder. An event appears on the agenda of the date its start time falls
  on; events crossing midnight are not repeated on the next day.
- Email drafts are local until explicitly sent. Remote search and sending need
  an IMAP/SMTP account and the environment password; there is no OAuth, contact
  autocomplete, attachment handling, rich HTML composition, folder management
  or background mail sync. Summaries are extractive and use only the fetched
  message body.
- Pomodoro state is persisted in the assistant SQLite database and resumed on
  restart. A long absence advances elapsed phases; custom work/break patterns
  beyond the configured durations are not supported.
- Clipboard history records copies made through JARVIS, not changes made in
  other applications. It has no encryption at rest and no automatic secret
  detection; retention limits reduce but do not eliminate that risk.
- Daily summaries read the local services and do not create or change data.
  Meeting summaries are extractive and do not understand speaker turns,
  decisions or action items beyond the supplied text.
- Developer tools stay inside `[tools] root` and use confirmation for writes,
  tests and execution. Formatting is conservative whitespace normalization,
  not a full AST formatter; scripts run in a sanitized subprocess with `-I`,
  no shell and a timeout, which is not an operating-system isolation boundary.
  Git support is read-only and never commits.
- OCR is optional and uses the built-in Windows OCR engine, so it needs
  Windows and the matching OCR language pack. It reads scanned PDF pages up to
  `ocr_max_pages` and image files; low-resolution, rotated or unusual-font
  scans may be inaccurate. The reported confidence is a local heuristic, not
  a probability from the Windows engine; treat `fair` or `partial` results as
  a prompt to verify the scan. Charts, photos, embedded Office objects and
  images inside DOCX/XLSX are not described, and OCR does not preserve tables.
  PDF generation is text-only with Helvetica and Latin-1 characters; complex
  layout, fonts and non-Latin scripts are not reproduced. Document search is
  lexical FTS5 with no embeddings, so a question that uses different words
  from the document can miss a relevant passage. Indexing is bounded by
  `max_files` and `max_chunks`, and CSV/XLSX conversion requires extracted
  tables. DOCX/XLSX/PDF libraries are imported only when a matching file is
  opened; OCR libraries are imported only when a scanned page or image is
  processed. Opening the document service added about 5 MiB RSS, the three
  extraction libraries together added about 30 MiB when first imported. The
  OCR imports added about 16 MiB, and a small image plus OCR run brought the
  total increase to about 24 MiB on this machine. OCR page rendering uses
  additional transient memory proportional to `ocr_dpi`.
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
- Advanced research reports are deterministic, so fact and contradiction
  extraction is heuristic: it highlights conflicting figures but does not
  decide which source is correct. Publication dates are available only when a
  provider or page metadata supplies one; otherwise freshness is unknown.
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
