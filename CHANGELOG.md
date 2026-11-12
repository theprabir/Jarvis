# Changelog

All notable changes to JARVIS are recorded here. Versions follow the project
convention: each completed feature set increments the patch number; completing
all feature sets in a phase increments the middle number and resets the patch.
**v0.2.0** begins the moderate-advanced line at the user's request; subsequent
feature sets in this level increment the patch (v0.2.1, …), and completing the
level increments the middle number.

## v0.2.10 — 2026-11-12

**Feature set 4.14 Application Intelligence**

### Implemented functionality

- **Active application** — foreground-window detection through user32 and
  process inspection; `/app status` and `/app title` are read-only.
- **Context** — application category, window title and contextual suggestions
  for browsers, editors, terminals and general desktop apps.
- **Application shortcuts** — browser, editor and generic shortcut catalogs;
  `/app shortcuts` lists the active app's keys.
- **Clipboard assistance** — classification of URLs, code, text and errors;
  `/app clipboard` is confirmation-only.
- **Copied-error explanations** — deterministic guidance for Python imports,
  syntax, files, permissions, connections, timeouts, JavaScript and HTTP errors.
- **Selected-text search** — `/app search` reads the clipboard and routes the
  selected text through the grounded web-search pipeline.
- **Browser tab management** — confirmed new, close, reopen, next, previous,
  reload and address actions with validated key sequences.
- **Browser page summarization** — reads the active page URL, fetches its
  article text and returns a local extractive summary.
- **Screenshot OCR** — captures the screen and reads text through the existing
  Windows OCR/document pipeline.
- **Application documentation search** — `/app docs TOPIC` combines the active
  application with documentation-focused research.

### Important files

- `core/application.py` — active-window context, clipboard classification,
  error guidance, browser/page actions and OCR integration.
- `tools/application.py` — validated safe and confirmation-only tool handlers.
- `tools/desktop.py` — foreground window lookup and validated shortcut sender.
- `tools/__init__.py` — application tool registration.
- `core/assistant.py`, `core/intents.py` — `/app` commands, natural routing and
  confirmed pending actions.
- `core/config.py`, `config/settings.toml` — `[application]` configuration and
  validation.
- `tests/test_application.py` — active app, clipboard, browser, OCR, config,
  intent, tool and assistant coverage.
- `README.md`, `CHANGELOG.md` — v0.2.10 documentation.

### Testing

- Focused application suite: **12 tests, OK**.
- Full suite: **570 tests, OK**.
- `python -m compileall -q core tools config tests main.py` passed.
- `git diff --check` passed.

## v0.2.9 — 2026-11-12

**Feature set 4.13 Advanced Web Research**

### Implemented functionality

- **Multiple providers** — research uses the existing Brave, SearXNG,
  DuckDuckGo and Wikipedia provider chain, with fallback and provider listing.
- **Domain search** — `/research domain example.com QUERY` validates the host
  and adds a `site:` restriction.
- **Documentation search** — `/research docs QUERY` adds official-documentation
  and API-reference query variants and prioritizes known documentation hosts.
- **Multi-query research** — a bounded set of variants is merged and deduplicated
  by normalized URL.
- **Article preservation** — fetched article text and publication metadata are
  retained with each stored source.
- **Fact extraction** — numeric and question-specific claims are extracted with
  source numbers.
- **Source comparison** — domain, publication date, freshness, article text and
  fact counts are reported per source.
- **Contradiction detection** — differing figures attached to similar claims are
  surfaced for human review rather than hidden.
- **Citations and reports** — numbered citations and a Markdown research report
  include key facts, comparison, contradictions, freshness and preservation.
- **Research history** — reports are stored in `data/research.db`, with bounded
  unsaved history, explicit saving, retrieval, listing, deletion and keyword
  lookup.
- **Freshness assessment** — publication dates are classified as fresh, recent,
  stale or unknown, and the report shows the overall result.

### Report quality improvements

- **Paraphrase-aware grouping** — measurements are grouped by their normalized
  head noun and unit class, so “population was 10 million” and “population
  reached 12 million” are compared instead of being listed separately.
- **Preposition handling** — “population of Tokyo” and “Tokyo population” meet
  at the same claim, while contrasting qualifiers such as city and country stay
  separate.
- **Equivalent-value normalization** — `10 million`, `10,000,000` and unit
  scales compare numerically; percentages, distances, masses and durations do
  not produce false conflicts with counts.
- **Cross-source facts preserved** — identical facts from different sources are
  retained for agreement/conflict checks instead of being deduplicated away.
- **Actionable conflict details** — each conflicting value lists its supporting
  sources and the report includes a confidence level.

### Important files

- `core/research.py` — research orchestration, extraction, comparison,
  freshness, reports and SQLite history.
- `web/extract.py` — publication-date metadata extraction.
- `web/search.py` — provider listing and publication metadata attached to
  fetched results.
- `core/assistant.py`, `core/intents.py` — `/research` commands, natural
  routing, history and saved-report retrieval.
- `core/config.py`, `config/settings.toml` — bounded research storage settings.
- `tests/test_research.py` — offline research, report, store, intent and
  assistant coverage.

### Testing

- Focused research suite: **10 tests, OK**.
- Full suite: **558 tests, OK**.
- `python -m compileall -q core web tests main.py` passed.
- `git diff --check` passed.

## v0.2.8 — 2026-11-11

**Pomodoro end-of-session, recovery visibility and complete 4.12 Developer Assistant**

### Implemented functionality

- **Configurable end** — `[pomodoro] end_after_cycles = N` ends the session
  after N completed work phases and their final break; `0` keeps cycling.
- **Recovery details** — `/pomodoro status` shows the saved phase, remaining
  time and saved due time after a restart, while the timer continues from the
  remaining duration.
- **Source explanation** — `/dev explain PATH`, natural `explain source PATH`
  and the `developer_explain` tool parse Python with `ast` and report the
  module docstring, imports, top-level functions, classes and methods.
- **Project inspection** — `/dev inspect [PATH]`, natural `inspect project`
  and the `developer_inspect` tool report bounded file/directory counts,
  Python modules, test files, file types, top-level entries and important
  configuration files.
- **Search and symbols** — `/dev search` scans bounded source text and
  `/dev find` locates Python functions and classes with file and line numbers.
- **Architecture and snippets** — `/dev architecture` summarizes modules,
  imports and tests; `/dev snippet` generates Python, JavaScript and
  TypeScript starters.
- **Diagnostics** — `/dev debug` and `/dev terminal` explain common Python and
  terminal errors; `/dev failures` interprets unittest/pytest output.
- **Documentation** — `/dev docs` generates Markdown API docs, `/dev readme`
  drafts a README, and `/dev docsearch` searches local documentation.
- **Formatting and tests** — `/dev format` normalizes Python whitespace and
  `/dev tests` runs the project's unittest suite with a timeout.
- **Read-only Git** — `/dev git status|log|changes|message` reports status,
  history, diff stats and a suggested commit message without committing.
- **Templates and scripts** — `/dev template` creates bounded project skeletons
  and `/dev run` executes one Python script with `-I`, no shell, a sanitized
  environment and a timeout.
- **Explicit permissions and sandbox** — all paths resolve inside `[tools]`
  root; formatting, doc/README writes, templates, tests and scripts are
  confirmation-only tools.

### Files

- `core/pomodoro.py` — bounded-session completion and recovery metadata.
- `core/developer.py` — source, search, symbols, architecture, snippets,
  diagnostics, docs, formatting, tests, Git, templates and sandbox execution.
- `tools/developer.py` — validated developer tool handlers and confirmation
  boundaries.
- `core/assistant.py`, `core/intents.py`, `tools/__init__.py` — Pomodoro
  recovery replies, `/dev` commands, natural routing and tool registration.
- `core/config.py`, `config/settings.toml` — `end_after_cycles`, scan/results,
  output, timeout and execution settings.
- `tests/test_remaining_411.py` — end-of-session and recovery assertions.
- `tests/test_developer.py` — source, project, search, symbols, architecture,
  snippets, diagnostics, docs, formatting, tests, templates, sandbox, intent,
  tool and assistant coverage.
- `README.md`, `CHANGELOG.md` — behavior and version.

### Testing

- Focused remaining 4.11 suite: **14 tests, OK**.
- Focused developer suite: **10 tests, OK**.
- Full suite: **548 tests, OK** (the count includes the complete 4.12 suite).
- `python -m compileall -q core tools config tests main.py` passed.
- `git diff --check` passed.

## v0.2.7 — 2026-11-11

**Pomodoro session persistence**

### Implemented functionality

- **Restart recovery** — the active Pomodoro phase, its due time, completed
  work count and work length are stored in a single `pomodoro_state` row in
  the assistant SQLite database. Closing JARVIS leaves the row intact.
- **Remaining-time resume** — the next Pomodoro request restores the phase and
  schedules only the remaining seconds. If JARVIS was closed past one or more
  phase boundaries, elapsed work and break phases are advanced before the
  next active timer is scheduled.
- **Explicit stop still clears** — `/pomodoro stop` cancels the timer and
  removes the persisted state, while a normal application shutdown preserves
  it for restart.

### Files

- `core/pomodoro.py` — single-row state store, restart restoration, elapsed
  phase advancement and the existing timer-backed phase controller.
- `core/config.py`, `config/settings.toml` — `[pomodoro] db` setting and
  validation.
- `tests/test_remaining_411.py` — restart/remaining-time persistence coverage.
- `README.md`, `CHANGELOG.md` — persistence behavior and version.

### Testing

- Focused remaining 4.11 suite: **13 tests, OK**.
- Full suite: **537 tests, OK**.
- `python -m compileall -q core tools config tests main.py` passed.
- `git diff --check` passed.

## v0.2.6 — 2026-11-11 — 4.11 Productivity Management (M1-M11)

**Local to-dos, planning, notes, habits, goals, time tracking, calendar,
email, Pomodoro, clipboard history and summaries**

### Implemented functionality

- **Separate productivity namespace** — to-dos live in a new `todo_items`
  table and use `/todo`; the existing durable execution plans keep `tasks`
  and `/tasks` unchanged.
- **To-do management** — add, list, show, edit, complete and remove local
  items with `low`, `normal`, `high` or `critical` priority.
- **Deadlines and reminders** — a future due date schedules a notification
  through the existing timer service. Reminders survive restarts, missed
  reminders are reported on the next start, and completing or removing a
  to-do cancels its pending reminder.
- **Recurrence** — daily, weekly and monthly to-dos create exactly one next
  occurrence when completed, linked through `parent_id`; the next due date is
  always in the future.
- **Snooze and edit** — `/todo snooze ID [MINUTES]` reschedules the reminder;
  `/todo edit ID due|remind|priority|repeat|title VALUE` updates fields and
  keeps the scheduler in sync.
- **Natural language** — `add a to-do to ...`, `what's on my todo list`,
  `mark todo ID done`, `show todo ID`, `snooze todo ID for N minutes` and
  `remove todo ID` route deterministically without calling the model.
- **Validated to-do tools** — `todo_add`, `todo_list`, `todo_show`,
  `todo_update`, `todo_complete`, `todo_snooze` and `todo_delete` expose the
  same `ProductivityService` to durable plans and model tool calls. They
  validate arguments, return JSON-safe data, and share the assistant's lazy
  service instead of opening a second database or scheduler. Delete requires
  confirmation; the other to-do tools are safe.
- **Daily and weekly planning** — `/plan` builds a read-only agenda for today
  or a chosen day from open to-dos, separating overdue, scheduled,
  unscheduled and upcoming work. `/plan week` covers the next seven days and
  keeps overdue, unscheduled and later items separate. Natural requests such
  as `plan my day` and `plan my week` route without the model.
- **Planning tools** — `plan_day` and `plan_week` expose the same agendas to
  durable plans and model tool calls with structured JSON-safe items. They
  are safe, read-only and never create deadlines or change an item.
- **Local notes** — notes live in their own `notes` table and support add,
  list, show, edit and delete. `/notes add TITLE | TEXT` sets an explicit
  title; otherwise the first line is used.
- **Quick voice notes** — `note that ...`, `take a note ...`, `note: ...`,
  `quick note ...` and `jot down ...` save immediately without the model.
  Natural listing, showing, search and deletion are recognized too.
- **Note search** — SQLite FTS5 over titles and content, with a scored scan
  fallback when FTS5 is unavailable. Results include a short matching excerpt.
- **Validated note tools** — `note_add`, `note_list`, `note_show`,
  `note_search`, `note_update` and `note_delete` expose the same service to
  durable plans and model tool calls. Delete requires confirmation; the other
  note tools are safe.
- **Daily habits** — add, list, show, edit and remove habits in their own
  `habits` and `habit_checkins` tables. A check-in is stored once per local
  day, so repeating it is idempotent.
- **Streak tracking** — current streaks count consecutive days ending today
  or yesterday when today is not checked in yet; best streaks and total
  check-ins are tracked separately. `/habit undo ID` removes today's check-in.
- **Recurring habit reminders** — an optional clock time creates one persisted
  daily reminder through the existing timer scheduler. It rolls forward after
  a restart instead of delivering missed occurrences as a flood.
- **Natural habit language** — `add a habit to read every day at 9pm`,
  `list my habits`, `show habit 2`, `habit 2 done`, `undo habit 2` and
  `remove habit 2` route without the model; natural deletion asks for
  confirmation.
- **Validated habit tools** — `habit_add`, `habit_list`, `habit_show`,
  `habit_complete`, `habit_undo`, `habit_update` and `habit_delete` expose the
  same lazy service to durable plans and model tool calls. Delete requires
  confirmation; the other habit tools are safe.
- **Local goals** — goals live in their own `goals` table with optional numeric
  targets, units and target dates. `/goal progress` sets absolute progress and
  `/goal increment` adds to it.
- **Goal lifecycle** — goals can be completed, reopened and edited; active
  goals are listed first, followed by completed goals, and target dates show
  days remaining or overdue days.
- **Natural goal language** — `add a goal to read 12 books by December 31`,
  `list my goals`, `show goal 2`, `goal 2 progress 5`,
  `add 2 to goal 2`, `complete goal 2` and `remove goal 2` route without the
  model; natural deletion asks for confirmation.
- **Validated goal tools** — `goal_add`, `goal_list`, `goal_show`,
  `goal_progress`, `goal_complete`, `goal_reopen`, `goal_update` and
  `goal_delete` expose the same lazy service to durable plans and model tool
  calls. Delete requires confirmation; the other goal tools are safe.
- **Local time tracking** — start and stop one active entry, inspect elapsed
  status, list recent entries and remove mistakes. Elapsed time is derived
  from persisted timestamps, so no second timer thread is started.
- **Daily and weekly totals** — reports group tracked time by label and split
  entries that cross midnight between local dates. `/time week` shows a
  seven-day breakdown and total.
- **Natural time language** — `start tracking writing`, `stop tracking`,
  `what am I tracking`, `how much time did I spend today`, `time week`,
  `list my time entries` and `remove time entry 2` route without the model;
  natural deletion asks for confirmation.
- **Local calendar events** — events live in their own `calendar_events`
  table with a title, optional details, start date and time, and a duration
  in minutes. Durations default to 60 minutes and are validated to 1–1440.
- **Daily agenda** — `/calendar today`, `/calendar tomorrow` and
  `/calendar agenda DATE` list events ordered by start time for the local
  date. `/calendar list` shows upcoming events.
- **Natural calendar language** — `add a calendar event for lunch tomorrow at
  noon`, `what's on my calendar today`, `show calendar event 2` and
  `remove calendar event 2` route without the model; natural deletion asks for
  confirmation.
- **Validated calendar tools** — `calendar_add`, `calendar_agenda`,
  `calendar_list`, `calendar_show`, `calendar_update` and `calendar_delete`
  expose the same lazy service to durable plans and model tool calls. Delete
  requires confirmation; the other calendar tools are safe.
- **Local email drafts** — drafts live in their own `email_drafts` table with
  a recipient, optional cc, subject and body, and can be listed, shown, edited
  and deleted without an account. Header values reject line breaks to prevent
  header injection.
- **Optional connected account** — IMAP search, message reading and SMTP
  sending are enabled only when `[email]` names the servers and username. The
  password is read from `password_env`, never from the TOML file. Sending and
  deletion require confirmation.
- **Email summaries and replies** — fetched messages get a local extractive
  summary, and `/email reply UID` creates a quoted local draft addressed to the
  sender. No message content is sent to the model for these actions.
- **Natural email language** — `draft an email to ... about ...`,
  `list my email drafts`, `show email draft ID`, `send email draft ID`,
  `remove email draft ID`, `search my email for ...`, `read email UID`,
  `summarize email UID` and `reply to email UID` route without the model;
  send and delete ask for confirmation.
- **Validated email tools** — `email_draft`, `email_list`, `email_show`,
  `email_update`, `email_delete`, `email_send`, `email_search`, `email_read`,
  `email_summarize` and `email_reply` expose the same lazy service to durable
  plans and model tool calls.
- **Pomodoro phases** — work, short-break and long-break timers reuse the
  existing `TimerService`; completion callbacks advance the phase without a
  second scheduler. `/pomodoro start|status|stop|skip` and natural language
  route deterministically.
- **Validated Pomodoro tools** — `pomodoro_start`, `pomodoro_status`,
  `pomodoro_stop` and `pomodoro_skip` share the same timer-backed service.
- **Bounded clipboard history** — copies made through `clipboard_copy` are
  stored in a local table with source and timestamp. Search, show, delete and
  clear are available through `/clipboard` and natural language; delete and
  clear require confirmation.
- **Clipboard retention** — item count, character size and age limits prune
  history automatically. The history is local and does not attempt to record
  clipboard changes made by other applications.
- **Validated clipboard tools** — `clipboard_list`, `clipboard_search`,
  `clipboard_show`, `clipboard_delete` and `clipboard_clear` expose the same
  bounded store; existing `clipboard_copy` records through it.
- **Daily summaries** — `/summary` combines open to-dos, calendar events,
  tracked time and habit check-ins for the requested local day. The summary is
  read-only and deterministic.
- **Meeting summaries** — `/summary meeting TITLE | NOTES` and natural language
  produce an extractive summary of supplied notes without a model or network
  call. `summary_daily` and `summary_meeting` expose both actions as tools.
- **Validated time tools** — `time_start`, `time_stop`, `time_status`,
  `time_report`, `time_week`, `time_list` and `time_delete` expose the same
  lazy service to durable plans and model tool calls. Delete requires
  confirmation; the other time tools are safe.
- **Expanded deterministic dates** — to-do parsing now understands
  time-of-day words, bare afternoon clock hours, `next week/month`,
  `this weekend`, `end of day/week/month`, month names, month/day forms,
  `the 20th`, word-number relative dates and implicit durations such as
  `in two hours`. Past same-day weekday and calendar dates roll to their next
  real occurrence.
- **Backward-compatible reminder migration** — existing `reminders` tables
  gain `source`, `source_id` and `repeat` columns through guarded
  `ALTER TABLE` statements; old one-shot reminders keep working.

### Files

- `core/productivity.py` — SQLite to-do store, recurrence, parsing, reminder
  scheduling rules, read-only daily/weekly plans and agenda formatting.
- `core/timers.py` — source-aware reminders, daily repetition, schema
  migration and `cancel_source`, preserving the single scheduler thread.
- `core/intents.py`, `core/assistant.py` — `/todo` and `/plan` commands,
  natural intent routing, replies, planning, edit/snooze handling and the
  lazy productivity provider passed into the tool registry.
- `tools/productivity.py`, `tools/__init__.py` — validated to-do tool
  wrappers, planning tools and conditional registration through a lazy
  service provider.
- `core/notes.py`, `tools/notes.py`, `tools/__init__.py` — SQLite note store,
  FTS5 search with scan fallback, note formatting and validated tools.
- `core/habits.py`, `tools/habits.py`, `tools/__init__.py` — SQLite habit
  store, check-in streaks, natural parsing, daily reminders and validated
  tools.
- `core/goals.py`, `tools/goals.py`, `tools/__init__.py` — SQLite goal store,
  numeric progress, target dates, natural parsing and validated tools.
- `core/time_tracking.py`, `tools/time_tracking.py`, `tools/__init__.py` —
  SQLite time entries, elapsed time, daily/weekly totals and validated tools.
- `core/calendar.py`, `tools/calendar.py`, `tools/__init__.py` — SQLite
  calendar events, daily agendas, natural parsing and validated tools.
- `core/mail.py`, `tools/mail.py`, `tools/__init__.py` — local email drafts,
  optional IMAP/SMTP account actions, extractive summaries and validated
  tools.
- `core/pomodoro.py`, `tools/pomodoro.py` — timer-backed work/break phases and
  validated Pomodoro tools.
- `core/clipboard_history.py`, `tools/clipboard_history.py` — bounded local
  clipboard history, search and validated tools.
- `core/summaries.py`, `tools/summaries.py` — deterministic daily and meeting
  summaries and their tools.
- `core/config.py`, `config/settings.toml` — validated `[productivity]`,
  `[notes]`, `[habits]`, `[goals]`, `[time]`, `[calendar]` and `[email]`
  settings, plus `[pomodoro]` and `[clipboard]` limits.
- `tests/test_productivity.py` — offline tests for parsing, CRUD, recurrence,
  reminders, restart/missed recovery, planning, assistant flows and the tool
  layer.
- `tests/test_notes.py` — offline tests for note storage, search and fallback,
  quick-note intents, commands, assistant flows and tools.
- `tests/test_habits.py` — offline tests for habit parsing, storage, streaks,
  timer repetition and recovery, intents, commands, assistant flows and tools.
- `tests/test_goals.py` — offline tests for goal parsing, storage, progress,
  target dates, intents, commands, assistant flows and tools.
- `tests/test_time_tracking.py` — offline tests for entries, midnight splits,
  daily/weekly totals, intents, commands, assistant flows and tools.
- `tests/test_calendar.py` — offline tests for event parsing, CRUD, daily
  agendas, limits, intents, commands, assistant flows and tools.
- `tests/test_email.py` — offline tests for drafts, parsing, optional IMAP and
  SMTP flows, summaries, replies, commands and tools.
- `tests/test_remaining_411.py` — Pomodoro phase transitions, clipboard
  history, summaries, intents, commands and configuration.
- `README.md` — productivity, planning, note, habit, goal, time, calendar and
  email commands, configuration and current 4.11 status.

### Bug fixes

- **Date-only reminders** — a phrase such as `tomorrow` no longer rolls to
  the day after tomorrow when the default 9:00 time has already passed;
  `tonight` now means 20:00 on the current day.
- **Natural-language deletion confirmation** — `remove todo ID` validates the
  item, asks for confirmation and deletes through the validated `todo_delete`
  tool; a direct service fallback remains for tools-disabled sessions.

### Testing

- `python -m compileall -q core tools config tests main.py` passed.
- `python -m unittest discover -s tests` passed: **536 tests**.
- Focused productivity suite: **44 tests, OK**; focused notes, habits, goals
  and time suites: **17, 21, 17 and 15 tests, OK**.
- Focused calendar suite: **16 tests, OK**.
- Focused email suite: **17 tests, OK**.
- Focused remaining 4.11 suite: **12 tests, OK**.
- Measured added RSS for the service and 200 in-memory to-dos in the test
  process: **~1.0 MB**; no second scheduler thread is created. The tool
  wrappers add no new service, database or background thread.
- Building both daily and weekly plans over the 2,000-item limit allocated
  **~3.4 MB peak** in `tracemalloc`; plans hold references to existing
  `TodoItem` objects and add no database, worker or scheduler.
- Listing 5,000 notes and running a 10-result FTS5 search allocated **~2.9 MB
  peak** in `tracemalloc`; note tools reuse the same lazy service and add no
  worker or scheduler.
- Listing 200 habits with 18,000 check-ins allocated **~276 KiB peak** in
  `tracemalloc`; habit reminders reuse the existing timer thread and add no
  second scheduler.
- Listing 500 goals allocated **~449 KiB peak** in `tracemalloc`; goal tools
  use the same lazy service and add no database thread, timer or worker.
- Listing 20,000 time entries and building a daily report allocated
  **~11.5 MB peak** in `tracemalloc`; time tracking uses persisted timestamps
  and starts no timer, worker or background thread.
- Loading 500 calendar events and building a daily agenda allocated
  **~341 KiB peak** in `tracemalloc`; calendar access uses one SQLite
  connection and starts no timer, worker or background thread.
- Listing 500 email drafts allocated **~402 KiB peak** in `tracemalloc`;
  local drafts use one SQLite connection, and remote account actions open a
  connection only for the requested operation.
- Listing 500 clipboard entries allocated **~218 KiB peak** in `tracemalloc`;
  clipboard history uses one SQLite connection and prunes synchronously on
  save, with no background worker or additional thread.

## v0.2.5 — 2026-11-10

**OCR confidence and partial-recognition warnings**

### Implemented functionality

- **Local confidence estimate** — every OCRed image and scanned PDF page now
  gets a 0–100% quality estimate from recognized character volume, word-box
  page coverage and token cleanliness. Windows OCR does not expose a native
  confidence value, so this is explicitly labeled a local heuristic.
- **Page-level metadata** — scanned PDFs expose `ocr_confidence`,
  `ocr_level`, `ocr_page_details`, `ocr_partial_pages`, `ocr_warnings` and
  `ocr_errors`; images expose the same confidence/level/warning fields with
  `ocr_partial`.
- **Clear warnings** — `/documents read` shows the estimate and a warning when
  a page or image is `fair`, `partial` or empty, or when OCR fails on a page.
  Document read/index tools and single-file index results carry the same
  warning. Folder indexing collects the first affected files.
- **No false certainty** — low text volume, little page coverage, unusual
  characters and many short tokens lower the estimate; the reasons are
  returned as warnings instead of silently treating poor OCR as reliable text.

### Files

- `core/documents.py` — OCR quality scoring, word-box coverage, page details,
  image confidence metadata and warning aggregation.
- `core/assistant.py` — confidence and partial-recognition notices in document
  read and index replies.
- `tools/documents.py` — confidence and warning text in document read/index
  tool results.
- `tests/test_documents.py` — confidence metadata assertions, mocked
  low-confidence OCR and assistant warning coverage.
- `README.md`, `CHANGELOG.md` — confidence semantics, warnings and version.

### Bug fixes

- None in this enhancement.

### Testing

- `python -m compileall -q core tools config tests main.py` passed.
- `python -m unittest discover -s tests` passed: **377 tests**.
- OCR confidence and warning tests run offline against generated images and
  mocked OCR output; the real Windows OCR smoke test remains covered. A
  rotated, slightly blurred real image returned 72% (`fair`) with a
  limited-text warning, confirming the heuristic flags degraded recognition.

## v0.2.4 — 2026-11-10

**Optional OCR for scanned documents**

### Implemented functionality

- **Scanned PDF OCR** — PDF pages with fewer than `ocr_min_chars` extracted
  characters are rendered locally with `pypdfium2` and recognized through the
  built-in Windows OCR engine (`winocr`). Existing page text is kept and OCR
  text is appended, so headers and mixed pages are not lost. OCR work is
  bounded by `ocr_max_pages`; additional scanned pages are skipped and reported.
- **Image OCR** — PNG, JPG/JPEG, BMP, TIFF and WebP files can be read and
  indexed as documents when OCR is enabled. Images are OCRed locally through
  Pillow and the Windows engine.
- **Configuration and safety** — `[documents] ocr_enabled`, `ocr_language`,
  `ocr_dpi`, `ocr_min_chars` and `ocr_max_pages` are validated. Images are
  accepted only while OCR is enabled, OCR paths keep the existing `[tools]
  root` sandbox, and an image larger than 25 megapixels is rejected before
  decoding.
- **Fallbacks** — missing `winocr`, `pypdfium2`, Pillow or language packs
  produce an actionable `DocumentError`; if only OCR fails, normally extracted
  PDF text is still returned. OCR status is exposed in document metadata and
  mentioned in `/documents read` output.
- **Indexing** — OCRed PDFs and images feed the existing SQLite/FTS5 chunk
  index, so search, Q&A, summaries, dates, actions, comparison, conversion and
  reports work on scanned content without a separate pipeline.

### Files

- `core/documents.py` — OCR engine integration, PDF page rendering, image
  extraction, OCR metadata and image-aware extension handling.
- `core/config.py`, `config/settings.toml` — validated OCR settings.
- `core/assistant.py` — OCR notices in the read response.
- `requirements.txt` — Pillow, pypdfium2 and winocr.
- `tests/test_documents.py` — real generated-image and image-only PDF OCR
  tests, plus OCR-disabled coverage.
- `README.md`, `CHANGELOG.md` — OCR capabilities, setup, limits and status.

### Bug fixes

- None in this enhancement.

### Testing

- `python -m compileall -q core tools config tests main.py` passed.
- `python -m unittest discover -s tests` passed: **375 tests**.
- OCR smoke test used a generated PNG and an image-only PDF; both returned the
  expected `JARVIS` and `1200` text through the document service.
- Memory check: the OCR imports added about **16 MiB RSS**, and a small
  image plus OCR run brought the total increase to about **24 MiB**. OCR page
  rendering is transient and bounded by page size.

## v0.2.3 — 2026-11-10

**Document intelligence (feature set 4.10)**

### Implemented functionality

- **Local extraction** — PDF (`pypdf`), DOCX (`python-docx`), XLSX/XLSM
  (`openpyxl`), CSV, TXT and Markdown readers produce bounded text plus table
  rows. PDF page markers remain in the text, DOCX headings/styles are retained
  as text, and XLSX sheets become named tables. Unsupported files, oversized
  files and paths outside `[tools] root` are rejected.
- **Local index and retrieval** — `DocumentStore` keeps document metadata and
  overlapping passages in SQLite. FTS5 is used when available, with a scored
  scan fallback. `/documents index` handles one file or a folder; unchanged
  files are skipped, and virtual-environment, cache, model and hidden folders
  are ignored. `max_files` and `max_chunks` bound a scan.
- **Document Q&A** — `/documents ask` and natural requests retrieve passages,
  send only that evidence to the local model, require a citation and return a
  numbered source list. A missing or ungrounded answer falls back to the
  retrieved passage or says the documents did not contain the answer.
- **Summaries and extraction** — deterministic extractive summaries,
  explicit dates with surrounding context (including month-year references),
  checkbox/TODO action items and bullets under action headings, and extracted
  tables rendered as Markdown.
- **Comparison and explanation** — line-level similarity, added/removed lines,
  unique key terms and table counts; technical explanations use only the
  document text through the local model.
- **Conversion and generation** — convert among supported formats (table
  targets require extracted tables), create TXT/Markdown/CSV/XLSX/DOCX/PDF
  documents, and build reports that combine summaries, dates and action items
  from several documents. A small text-only PDF writer avoids another PDF
  dependency. Writes, conversions and generation ask for confirmation.
- **Commands and tools** — `/documents` and `/docs` support list, index,
  search, ask, read, summarize, dates, actions, tables, explain, compare,
  convert, create, report, remove and last. Natural document intents are routed
  before computer file intents, while explicit `read file ...` remains the
  original file tool. Document tools register only when the document service is
  available and keep SAFE/CONFIRM permissions.

### Files

- `core/documents.py` — extraction, tables, chunking, SQLite/FTS5 store,
  retrieval, summaries, dates, action items, comparison, conversion, report and
  PDF/DOCX/XLSX/TXT/Markdown/CSV writers.
- `tools/documents.py` — document tool handlers.
- `tools/__init__.py` — optional document service and eight document tools.
- `core/config.py`, `config/settings.toml` — validated `[documents]` section.
- `core/intents.py` — document intents, commands and routing precedence.
- `core/assistant.py` — document service lifecycle, commands, RAG answers,
  local extraction replies and confirmation flow for generated files.
- `requirements.txt` — `pypdf`, `python-docx`, `openpyxl`.
- `tests/test_documents.py` — 24 tests covering every format, tables, path
  rejection, indexing, unchanged-file skipping, FTS5 and scan fallback,
  summaries, dates, actions, comparison, conversion, reports, commands, tools
  and confirmations.
- `tests/test_tools.py` — registry coverage assertion accounts for optional
  document tools.
- `README.md` — status, configuration, commands, project layout and limitations.

### Bug fixes

- Document routing no longer captures `read file notes.txt` or `read the file
  notes.txt`; those stay with the existing file reader.
- XLSX rows are trimmed of trailing empty cells, and DOCX extraction falls back
  to the first heading for its title.
- CRLF text is normalized, and extractive summaries split on paragraph breaks
  instead of joining headings and bullets into one sentence.
- DOCX conversion no longer duplicates table content as both paragraphs and
  tables.

### Testing

- `python -m compileall -q core tools config tests main.py` passed.
- `python -m unittest discover -s tests` passed: **373 tests**.
- Document tests are offline and use generated files; no microphone, speaker or
  internet access is required.
- Memory check in the existing `.venv`: opening the document service added
  about **5 MiB** RSS; importing the PDF, DOCX and XLSX libraries together
  added about **30 MiB** only when first used. No new background process was
  added.

## v0.2.2 — 2026-11-09

**Task planning reliability fixes (feature set 4.9)**

### Implemented functionality

- **Durable file-action plans** — `TaskEngine.create_file_plan` turns the
  existing explicit file-action planner into a persisted sequential task with
  dependencies and approval. `as a task, ...` and `/tasks plan ...` reuse the
  planner, so a multi-file request survives a restart and reports progress
  instead of running as one opaque assistant turn.
- **Large folders** — `[tasks] max_steps` now defaults to 5000 (validated
  3–10000), and organization passes that limit to both folder inspection and
  verification. A run is no longer capped at the previous 20-item display
  limit or the 50-step default.
- **Idempotent recovery** — a failed `create_folder` whose folder already
  exists, or a failed `move_path` whose source is gone and destination is
  present, is recorded as recovered instead of failing the task. This makes a
  resumed plan safe after a partial attempt.
- **Background auto-resume** — approved background tasks that were running at
  shutdown are restarted when the task engine opens. `[tasks] auto_resume`
  (default `true`) controls it; tasks awaiting approval remain paused.
- **Generic task reports** — completed non-organization tasks report their
  file-step count and verification count.

### Files

- `core/tasks.py` — file-plan factory, recovery, auto-resume, full-limit
  verification.
- `core/assistant.py` — `as a task` / `/tasks plan` handling and generic
  completion reports.
- `core/intents.py` — `task_plan` intent.
- `core/config.py`, `config/settings.toml` — `max_steps = 5000`,
  `auto_resume = true`.
- `tests/test_tasks.py` — seven tests for durable plans, a 25-file
  organization run, idempotent recovery and restart auto-resume; the
  background notification test now waits for the notification itself.
- `README.md` — task controls, configuration and limitations.

### Testing

- `python -m compileall -q core tools config tests main.py` passed.
- `python -m unittest discover -s tests` passed: **348 tests**.

## v0.2.1 — 2026-11-09

**Task planning and execution (feature set 4.9)**

### Implemented functionality

- **Durable task model** — a task is a goal plus ordered steps with tool
  arguments, dependencies, conditions, retries, verification and status,
  persisted in the `tasks` table. `task_events` records every plan, approval,
  retry, replan, completion, cancellation and failure.
- **Planning and decomposition** — `organize my downloads folder` inspects the
  folder, groups files into Documents, Images, Audio, Video, Archives, Code,
  Installers and Other, and builds inspect, create-folder, move and verify
  steps. Files already in a destination receive a collision-safe name.
- **Dependencies and conditions** — steps wait for prerequisites to complete.
  Failed or skipped prerequisites skip dependants, and a step can run only
  when an earlier result matches a declared comparison.
- **Sequential and parallel execution** — steps run in order by default;
  independent steps marked parallel run through `parallel_workers` threads.
- **Approval checkpoints** — destructive plans wait for `/confirm` (or
  `/tasks approve ID`). Individual steps can also pause for
  `/tasks approve-step ID`.
- **Status and checkpoints** — task and step state are saved after every
  change. `/tasks show ID` reports progress, current status, attempts and
  errors; interrupted runs are recovered as paused checkpoints.
- **Retries and error recovery** — each step can retry before failing. A
  failed organization move is dynamically replanned with a collision-safe
  target, and custom engines can install their own replanner.
- **Cancellation and resumption** — `/tasks cancel ID` stops at the next
  checkpoint; `/tasks resume ID` resets incomplete steps, preserves completed
  work and continues. Cancelled tasks can be resumed explicitly.
- **Execution verification** — verify steps list the destination folder and
  require every expected file name to be present; a failed check fails the
  task instead of reporting success.
- **Execution history** — `/tasks history` shows recent task events from
  `task_events`, bounded by `history_items`.
- **Background execution** — an approved background plan runs in a daemon
  thread, returns immediately to the conversation and notifies through the
  configured speaker when it reaches a terminal state.
- **Natural controls and commands** — `organize ...`, `list tasks`, `show task
  ID`, `approve task ID`, `resume task ID`, `cancel task ID`, `task history`,
  plus `/tasks` list/show/approve/approve-step/resume/cancel/history.
- **Configuration** — `[tasks]` adds `enabled`, `db`, `max_steps`,
  `max_retries`, `retry_delay_s`, `parallel_workers`, `approval_required` and
  `history_items`, all validated at load time.
- **Folder inspection** — `list_files` accepts a `limit` argument and returns
  the full requested item list in its structured data while keeping the
  spoken preview short, so the planner sees beyond the display cap.

### Files created or modified

New `core/tasks.py` and `tests/test_tasks.py`. Modified `core/assistant.py`
(task replies, approval flow, `/tasks`, `/cancel`, `/new`, help),
`core/intents.py` (task intents and recognizers), `core/config.py`
(`TasksConfig`, settings and validation), `config/settings.toml`, `tools/files.py`
and `tools/__init__.py` (folder inspection limit and registry argument),
`README.md`, `CHANGELOG.md`.

### Bug fixes found while verifying

- Folder inspection originally returned only the display preview in
  structured data, so an organization plan silently missed files beyond
  `search_limit`. `list_files` now accepts a limit and returns the full
  requested list while keeping the text preview short.
- Interrupted-task recovery marked tasks that were waiting for step approval
  as paused. It now recovers only tasks that were actually running, leaving
  approval checkpoints intact.
- Parallel step execution needed checkpoint writes to remain on the main
  thread; tool execution is parallel but task state is saved serially.

### Testing status

All tests pass offline with `.venv\Scripts\python -m unittest discover -s
tests`: **342 tests**, including 25 new task tests covering dependencies,
conditions, sequential and parallel execution, retries, approvals,
verification, cancellation, resumption, dynamic replanning, background
notification, persistence, organization and assistant controls. `compileall`
passes. No model, microphone or desktop action is needed by the suite.

### Known limitations

- The conversational planner currently supports folder organization. Other
  multi-step requests use the explicit file-action planner or the model.
- Organization is capped at `[tasks] max_steps` and inspects at most that many
  items; very large folders must be split.
- Steps stay inside `[tools] root`. Only move-name collisions are replanned
  automatically; other failures stop the task and need `/tasks resume ID`.
- A background task pauses at its next checkpoint when JARVIS exits and is
  resumed explicitly with `/tasks resume ID` after the next start.

## v0.2.0 — 2026-11-09

**Automatic memory learning (feature set 4.8) — moderate-advanced line begins**

### Implemented functionality

- **Layered memory** — short-term (session), semantic (explicit and learned),
  episodic (expiring summaries) and long-term (SQLite across restarts) memory
  now work together. The conversation itself is never saved wholesale as
  permanent memory.
- **Automatic candidate extraction** — a deterministic learner recognizes a
  narrow set of high-confidence first-person statements, including name,
  preferences, favourites, residence, employer, job, age, birthday, folders
  and named personal attributes. Questions, commands, greetings, short
  replies and assistant-directed text such as `I like you` are ignored.
  `learn_min_confidence` (default 0.75) is enforced.
- **Importance and categorisation** — candidates are scored from 1 to 5 using
  category, importance cues, length and preference signals, then stored with
  a category and optional conflict subject.
- **Provenance and retention** — learned memories carry `source`, `confidence`
  and `expires_at`; they expire after `learned_ttl_days` (180). Explicit
  memories remain permanent, and `short_term_items` (6) bounds the session
  buffer.
- **Conflict detection** — `name`, `residence`, `employer`, `job`, `age`,
  `birthday`, `favorite:<thing>` and named folders are treated as stable
  attributes. A newer value expires the old row and links it through
  `superseded_by`, so the prompt uses one current value while history remains
  inspectable.
- **Episodic memory** — substantive exchanges (six words or more, excluding
  commands and greetings) become one-line summaries with keywords. Episodes
  expire after `episodic_ttl_days` (30), are capped at `episodic_items` (200),
  and can be ranked by relevance.
- **Ranked retrieval and context reconstruction** — search reranks term
  overlap, importance, recency and preference hints. `context_block` combines
  the best semantic memories and recent episodes inside a bounded prompt
  block; with conversation history disabled, the session buffer adds a
  `Session so far` summary.
- **Cross-session memory** — explicit and learned memories and episodes are
  reloaded after a restart, while the short-term buffer resets with the
  session.
- **Transparency and controls** — `/memory learned` lists automatic memories,
  `/memory episodes` shows summaries, `/memory explain ID` prints source,
  importance, confidence, expiry and replacement, and the existing edit,
  delete and clear commands work on learned memories too. `/memory episodes
  clear` requires `confirm`. `/new` resets session state.
- **Explicit corrections** — automatic learning never overwrites an explicit
  memory; `remember ...` corrects it and records the previous value as
  superseded.
- **Configuration** — `[memory]` gains `auto_learn`, `learn_min_confidence`,
  `learned_ttl_days`, `episodic_ttl_days`, `episodic_items` and
  `short_term_items`, all validated at load time.

### Files created or modified

New `core/learning.py` and `tests/test_learning.py`. Modified `core/memory.py`
(schema migration, episodes, subjects, conflicts, ranked retrieval, context
block, expiry), `core/assistant.py` (learning wrapper, episode recording,
learned/episode replies, `/memory explain`, `/new` reset, explicit conflict
handling), `core/intents.py` (`learned_memory`, `memory_episodes`),
`core/config.py` (`MemoryConfig` fields and validation),
`config/settings.toml`, `README.md`, `CHANGELOG.md`.

### Bug fixes found while verifying

- The FTS path converted rows to dicts and then reranked them as if they were
  rows, so `importance` became the string `"importance"` and search crashed.
  `_search_fts` now returns raw rows and selects the new provenance columns.
- The assistant-directed guard only matched `" you "`, so `I like you` was
  learned as a preference; it now uses a word boundary.
- `I am 34 years old` was not extracted because the age pattern required a
  trailing clause; the age and `my age is` forms are now matched separately.
- The session summary only appeared after two prior exchanges; it now starts
  with the first prior turn.

### Testing status

All tests pass offline with `.venv\Scripts\python -m unittest discover -s
tests`: **317 tests**, including 27 new learning tests covering extraction,
importance, subjects, conflicts and history, ranking, expiry, episodes,
short-term memory, cross-session persistence, auto-learning disabled,
transparency and the assistant commands. `compileall` passes. No live model,
microphone or speaker was needed.

### Known limitations

- Learning is lexical and pattern-based, so unusual phrasing is missed rather
  than guessed; there are no embeddings.
- Learned values expire after 180 days and episodes after 30; superseded rows
  remain as history but are excluded from normal retrieval.
- Still a single user profile with no per-project memory separation.

## v0.1.11 — 2026-11-09

**Utilities (feature set 4.7)**

### Implemented functionality

- **Unit conversion** — `convert 10 km to miles`, `convert 25 celsius to
  fahrenheit`, `convert 500 mb to gb` and `how many pounds in 5 kg` cover
  length, mass, volume, speed, data sizes, time and temperatures in both
  directions (including metric/imperial aliases and plurals).
- **Text utilities** — `uppercase hello world`, `lowercase ...`, `title case
  ...`, `reverse ...`, `capitalize ...`, and `how many words|characters|letters
  in ...` operate on Python strings without a model call.
- **File-size formatting** — `how big is 1048576 bytes` and `format 2 gb as
  human readable` print binary units (B, KB, MB, GB, TB).
- **Weather** — `what's the weather in Kolkata` and `weather in London`
  geocode the place and fetch current conditions plus today's high/low from
  Open-Meteo, which needs no API key. `[weather]` adds `enabled`, `units`
  (`metric`/`imperial`), `default_location` and `timeout_s`. The client is
  created on first use and closed on shutdown.
- **Timers** — `set a timer for 5 minutes`, an optional `named` label, `list
  timers` and `cancel timer ID|all`. A daemon thread checks due entries every
  500 ms; notifications are printed and spoken through the same interface
  voice.
- **Reminders** — `remind me in 20 minutes to stretch`, `remind me to call
  mom at 5 pm` and `remind me to submit the report tomorrow at 9`. Reminders
  are stored in the `reminders` table of `data/jarvis.db`, reloaded after a
  restart, and a reminder that expired while JARVIS was off is reported at
  the next start.
- **Configuration and wiring** — the new intents (`unit_convert`,
  `text_transform`, `format_size`, `weather`, `timer_set`, `reminder_set`,
  `timer_list`, `timer_cancel`) route after memory and before time/date/math,
  with `/help` updated and `main.py` registering the spoken notifier.

### Files created or modified

New `core/utilities.py`, `core/timers.py`, `core/weather.py` and
`tests/test_utilities.py`. Modified `core/config.py` (`WeatherConfig`,
`Settings.weather`, validation and loading), `config/settings.toml`
(`[weather]`), `core/intents.py` (recognizers, intent names, routing),
`core/assistant.py` (utility/weather/timer replies, lazy clients, notifier,
lifecycle), `main.py`, `README.md`, `CHANGELOG.md`.

### Bug fixes found while verifying

- `format_size` mixed decimal conversion factors with binary output, so
  `format 2 gb as human readable` returned `1.863 GB`. It now uses binary
  byte factors and returns `2 GB`.

### Testing status

All tests pass offline with `.venv\Scripts\python -m unittest discover -s
 tests`: 290 tests, including the new conversion, text, size, duration,
reminder, weather, timer, notifier and assistant-reply cases. Live Open-Meteo
responses were probed successfully during development; the suite itself uses
recorded payloads.

## v0.1.10 — 2026-11-09

**Basic memory (feature set 4.6)**

### Implemented functionality

- **Explicit memories only** — `remember my name is Alex`, `remember the
  office is in Kolkata`, `remember this folder is C:/dev` and the trailing
  form `my name is Alex, remember that` save a memory. Nothing is promoted
  from the conversation automatically; that stays reserved for feature set
  4.8. Duplicate persistent memories refresh their timestamp instead of
  appearing twice.
- **Categories** — `preference`, `fact`, `path`, `folder`, `website`, `task`
  and `general`, inferred from the wording (`my … is …` becomes a preference,
  URLs a website, drive paths a path/folder) or set explicitly with
  `/memory add category text`.
- **Temporary and persistent memory** — `remember for now that …` writes a
  row with `persistent = 0`; temporary rows are deleted the next time JARVIS
  starts, so nothing accumulates on disk.
- **Inspection, search, editing and deletion** — `what do you remember`,
  `show your memories`, `what do you remember about Alex`, `forget memory
  about Kolkata`, and `/memory list|search|add|temporary|edit|delete|
  frequent|clear`. Bulk deletion needs `/memory clear` followed by
  `/memory clear confirm`; `/memory clear temporary` targets session rows.
- **Prompt use** — persistent memories are appended to the system prompt,
  preferences first then the newest facts, bounded by `inject_items` (6) and
  never blocking a reply. The conversation itself is not used as memory.
- **Frequent usage** — application launches, opened folders/paths and URLs
  that pass through the tools are counted in `memory_usage`; `/memory
  frequent [app|folder|website]` lists them by use count.
- **SQLite storage** — `core/memory.py` adds the `memories` and
  `memory_usage` tables to `data/jarvis.db`, with FTS5 search when the bundled
  SQLite supports it and a scored scan otherwise. Expired rows are filtered
  and purged on open.
- **Configuration** — `[memory] enabled`, `db` and `inject_items`, validated
  on load.

### Files created or modified

New `core/memory.py` and `tests/test_memory.py`. Modified `core/config.py`
(`MemoryConfig`, `Settings.memory`, validation, loading),
`core/intents.py` (memory recognizer and routing), `core/assistant.py`
(store lifecycle, prompt block, intent handling, `/memory`, usage counting),
`config/settings.toml`, `README.md`, `CHANGELOG.md`.

### Bug fixes found while verifying

- The memory recognizer matched on lowercased text and saved that form, so
  `remember my name is Alex` stored `my name is alex`. Content is now sliced
  from the original text with its casing and punctuation preserved, while
  matching stays case-insensitive.
- `[memory]` had to be added to settings loading, otherwise an existing
  `settings.toml` without the section would fail to load; the section defaults
  are now applied and validated.

### Testing

- 255 unit tests pass (26 new: store CRUD, dedupe, categories, FTS/scan
  search, expiry, temporary purge, prompt ordering and bounds, usage ranking,
  clear targets, classification, config validation, five intent shapes,
  commands, confirmation, prompt injection and the disabled path).
  `compileall` clean.
- Persistence verified across processes: an assistant saved memories, closed,
  and a new assistant on the same database recalled them
  (`recalled = True`); the temporary memory was gone
  (`temporary_gone = True`).
- Measured on this machine: store open adds 0.9 MB RSS,
  100 saved memories take 20.66 ms each, and a search averages
  0.38 ms; all 100 memories survived reopening
  (100 rows).

### Known limitations

- Explicit memories only: no automatic candidate extraction, importance
  scoring, expiration policy or conflict detection (feature set 4.8).
- Search matches words, not meaning; there are no embeddings.
- Duplicate detection compares exact text, so paraphrases are stored twice.
- A single user profile is assumed; there is no per-project separation.
- Usage counts cover tool-driven app/folder/URL activity only, not actions
  taken in a browser by hand.

## v0.1.9 — 2026-11-09

**Search fixes — every v0.1.8 limitation addressed**

### Implemented functionality

- **Wikipedia lookup through the MediaWiki API** — `wikipedia X` no longer
  becomes a `site:en.wikipedia.org` search. `web/wikipedia.py` calls
  `action=query&generator=search&prop=extracts` and returns article
  introductions as fetched text, preferring an exact title match so
  `wikipedia tokyo` cannot drift to `Tokyo!` (a film). If the lookup fails the
  normal web search is used and reported as such.
- **Passage-based evidence** — `best_passage` scores windows of consecutive
  sentences and sends the part of each page that best matches the question,
  instead of the first N characters. A fact buried deep in a page is now
  reachable without raising the token budget.
- **Wider fetching** — up to `extract_pages + 3` candidates are tried until
  `extract_pages` readable sources exist; video, social and binary URLs are
  skipped. A page that cannot be fetched still contributes its snippet, now
  labelled `(snippet only)` so snippets and fetched text stay distinguishable.
- **Recency-aware ranking** — `is_fresh` detects news-shaped questions
  (`latest`, `news`, `today`, `weather`, the current year). Those use the
  provider's news results with publication dates (DuckDuckGo news, Brave
  `freshness=pw`/`page_age`, SearXNG `time_range`/`publishedDate`), and dated
  results receive a 0.1–0.3 bonus by age.
- **Provider resilience** — optional self-hosted SearXNG through
  `[web] searx_url` (`web/searxng.py`), tried before DuckDuckGo in `auto` mode
  and usable alone with `provider = "searxng"`; `provider` is validated, and
  `searx_url` must be an HTTP(S) URL.
- **Wider retry** — the single grounded retry now uses double the evidence
  (2000 characters), so a first refusal has more material to work with.

### Bug fixes found while verifying

- Wikimedia returned **HTTP 403** for the first User-Agent. Four forms were
  tested: agents without contact information are refused, an agent with an
  email or URL is accepted. The provider now sends a descriptive User-Agent
  linking to the API etiquette page.
- A failed Wikipedia lookup silently fell back to DuckDuckGo and cached the
  web results under the Wikipedia key; the provider label then claimed
  `wikipedia`. The label now reflects what actually answered, and the cache
  version was raised (v5) so stale entries are never reused.
- The model echoed instructions: the first prompt produced the
  `NOT_FOUND`-style over-refusal measured earlier, and the retry prompt leaked
  `The specific fact or figure is:` into answers, while a later prompt was
  quoted back as `Here are the answers to the question …`. The prompts are now
  neutral, the `NOT_FOUND` instruction is gone (deterministic citation and
  support checks still reject unsupported answers), and `_strip_preamble`
  removes a repeated instruction paragraph before the answer is spoken.

### Files created or modified

New `web/wikipedia.py`, `web/searxng.py`. Modified `web/base.py` (published
date, date parsing), `web/extract.py` (`best_passage`), `web/search.py`
(Wikipedia routing, freshness, source selection, wider fetching, cache v5),
`web/duckduckgo.py` (news results, dates), `web/brave.py` (freshness, dates),
`core/assistant.py`, `core/intents.py`, `core/config.py`,
`config/settings.toml`, `tests/test_web.py`, `README.md`, `CHANGELOG.md`.

### Testing

- 229 unit tests pass (14 new: passage selection, freshness detection and
  ranking, Wikipedia and SearXNG parsing, exact-title filtering, fetch
  overscan, snippet labelling, preamble cleanup, wider-evidence retry,
  SearXNG config validation). `compileall` clean.
- Live provider checks: `wikipedia tokyo` returned the Tokyo article with
  3760 characters of text in under a second on a cache miss path, with no
  snippet-only marker; `latest news about artificial intelligence` returned
  results dated 2026-11-07 to 2026-11-09; a population query produced 1000
  characters of normal evidence and exactly 2000 on the retry.
- End to end with `llama-server`: `wikipedia tokyo` answered
  `The capital of Japan is Tokyo, as stated in [1]` with one source in 36.6 s
  (no preamble, no extra sources). `search the web for the population of Japan`
  answered `approximately 122,225,684 … [1]` in 47.5 s, including the grounded
  retry.

### Known limitations

- Evidence is still capped at three sources and 1000 characters (2000 on the
  retry); a fact split across many pages can be missed.
- Login- and CAPTCHA-gated pages cannot be read: they contribute a labelled
  snippet, and other candidates are fetched instead.
- DuckDuckGo scraping can break without notice; `searx_url` (self-hosted
  SearXNG) or `BRAVE_API_KEY` is the dependable path.
- Only English Wikipedia is queried, and recency ranking depends on the
  provider returning publication dates (plain web results often do not).

## v0.1.8 — 2026-11-09

**Internet search (feature set 4.5)**

### Implemented functionality

- **Search intents** — `web_search` is recognized from `search the web for …`,
  `look up …`, `google …`, `find information about …`, `latest news about …`
  and `wikipedia X` (routed as `X site:en.wikipedia.org`), plus `/web QUERY`.
  File searches (`search for notes in drafts`) still go to the file tools.
- **Provider abstraction** — `web/base.py` defines the result type and URL
  helpers; `web/duckduckgo.py` uses the `ddgs` client with the DuckDuckGo HTML
  endpoint as a fallback; `web/brave.py` uses the Brave API when
  `BRAVE_API_KEY` is set. `[web] provider = "auto"` picks Brave only when that
  key exists; provider errors are mapped to readable `SearchError`s.
- **Query generation and ranking** — two variants (the question and a
  stopword-free keyword form), URL normalization and dedupe, relevance scoring
  (title matches first), and removal of results that share no term with the
  question.
- **Page fetching and extraction** — the top three pages are fetched with
  `primp` (browser TLS fingerprint; plain `httpx` is refused with HTTP 403 by
  many sites) and reduced to main text with `trafilatura`, falling back to a
  compact `lxml` paragraph extractor. Snippets stay distinguishable from
  fetched text.
- **Evidence budget** — at most 1000 characters (~250 tokens) from at most
  three sources reach the model, numbered `[1]`…`[3]`.
- **Grounded answers** — the prompt requires an answer from that evidence with
  a citation; `_has_citation` and `_supported` then verify the citation exists
  and that the answer's figures or terms actually occur in the evidence. One
  retry is allowed before JARVIS says `I found sources but none of them
  actually answered that, sir.` A failed search is reported, never answered
  from model memory.
- **Sources** — every successful reply ends with a numbered `Sources:` list
  matching the citations.
- **Caching and offline fallback** — results are cached in
  `data/search_cache.db` for `cache_ttl_s` (1 h) with a bounded entry count and
  versioned keys; entries up to seven days old are used when the network fails.
- **Configuration** — `[web]` in `config/settings.toml` (enabled, provider,
  `max_results`, `max_queries`, `timeout_s`, `max_page_bytes`, `extract_pages`,
  `context_chars`, `cache_ttl_s`, `cache_entries`, `cache_db`,
  `brave_api_key_env`) with validation for every value.

### Files created or modified

New package `web/` (`base.py`, `search.py`, `duckduckgo.py`, `brave.py`,
`extract.py`, `cache.py`, `__init__.py`) and `tests/test_web.py`.
Modified `core/config.py`, `core/intents.py`, `core/assistant.py`,
`config/settings.toml`, `requirements.txt` (`ddgs>=9.0.0`), `.gitignore`
(search cache), `README.md`, `CHANGELOG.md`.

### Bug fixes found while verifying

- The first grounding prompt ordered the model to reply `NOT_FOUND` and the 1B
  model over-refused: with Worldometer's 122,225,684 in the evidence it still
  answered `NOT_FOUND` (50.7 s). A research-style prompt answered correctly
  (`122.4 million … [2]`, 32.4 s), so grounding is now a positive prompt plus
  deterministic citation/support checks and one retry.
- The support check treated the citation number itself as a figure; citation
  markers are stripped before comparing numbers.
- `_evidence` could include a fourth source while `Sources:` listed three;
  blocks are now capped to the same three results.
- Cache keys are versioned (`v2`) so entries written before extraction worked
  cannot return text-less results.

### Testing

- 212 unit tests pass, including ranking, dedupe, evidence budget, cache TTL
  and stale fallback, provider errors, query variants, intent routing, config
  validation, and grounded/uncited/fabricated-answer handling. `compileall`
  clean.
- Live search: `capital of Japan` returned 3 results; `python asyncio
  tutorial` returned 6 results in 9.3 s with two pages extracted (6000
  characters each).
- End-to-end with `llama-server`: "search the web for the population of Japan"
  answered `approximately 122,225,684 … [1]` with three sources in 28.8 s,
  Python RSS +6.9 MB for the search; a cached repeat took 3.0 s.

### Known limitations

- Only the top three pages are read and only 1000 characters of evidence are
  sent, so a fact buried deeper in a page is not found.
- Pages behind a login, a CAPTCHA or heavy JavaScript may contribute a snippet
  only; some sites still refuse automated fetching.
- Results are ranked by term overlap, not by publication date.
- The DuckDuckGo HTML fallback is inherently fragile; Brave is the stable
  option but needs a key.
- The Wikipedia REST API refused automated requests (HTTP 403), so Wikipedia
  lookups use the `site:` filter through the search provider instead.

## v0.1.7 — 2026-11-09

**Multi-step file plans, and a measured speech-model comparison**

### Implemented functionality

- **One confirmation for a sequence** — `plan_file_actions` splits a request
  into explicit file actions (create, write, read, list, search, rename, copy,
  move, delete). Two or more parseable actions become a plan: JARVIS shows a
  short summary (`Create the folder "drafts" and write 5 characters to
  "drafts/notes.txt". Please confirm to continue.`), executes the steps in
  order after one confirmation, and stops at the first failure with
  `Step N failed: …` instead of continuing blindly.
- **Folder context** — after a folder is created, later relative paths in the
  same sentence stay inside it; `in that folder` / `in it` is also accepted.
- **File lists** — `create two files - readme.md & main.py` (also `three`,
  `four`, `five` or a digit) expands into one empty-file action per name.
- **Safe stop on ambiguity** — a clause that does not parse as a file action
  ends the plan there, so nothing is guessed and the confirmation only lists
  what will actually run. `/cancel` and any unrelated request drop the plan.
- **Speech model comparison** — `vosk-model-en-us-0.22-lgraph` was downloaded,
  installed in `models/` and measured against the bundled small model with
  SAPI-synthesized speech (10 sentences, David and Zira voices, 20 clips):
  small 3.4 % word error, 181 MB, ~2.1 s per 3 s clip; lgraph 4.4 % error,
  396 MB, ~10.5 s per clip. The large model is kept on disk but **is not**
  the default: it would push the assistant to 1305 MB, over the 1.3 GB target.

### Files modified

`core/intents.py` (`FILE_INTENTS`, clause splitting, file-list expansion,
`plan_file_actions`), `core/assistant.py` (pending plan, `_propose_plan`,
`_run_plan`, `/cancel`), `tools/base.py` (`shorten` exported),
`config/settings.toml` (model note), `tests/test_tools.py` (5 plan tests),
`README.md`, `CHANGELOG.md`. New model directory
`models/vosk-model-en-us-0.22-lgraph/` (204 MB, optional).

### Testing

- 179 unit tests pass (5 new: plan ordering and folder context, file-list
  expansion, stop at an unclear clause, one-action requests stay single).
- Plan flow verified through `Assistant`: one short prompt (under 130 chars),
  both steps run after `yes`, `Step 1 failed` on a missing file, `/no` cancels
  without touching disk.
- STT benchmark: 20 synthesized clips per model, transcribed with
  `voice/stt.py`; word error computed by edit distance. Model load 1.9 s
  (small) vs 3.7 s (lgraph). Working-set baseline re-measured:
  `llama-server` 909 MB + Python 37 MB = 946 MB total.

## v0.1.6 — 2026-11-09

**Fixes — usable confirmations, correct paths and a working Escape key**
(on top of feature set 4.4; implemented and verified).

### Implemented functionality

- **Short spoken confirmations** — every destructive tool now has a concise
  preview ("Create the folder \"reports\"", "Write 19 characters to
  \"notes.md\"", "Close 2 chrome windows") and `Tool.describe` caps a
  confirmation at 180 characters. A pending action asks
  "Please confirm to continue." instead of reading the whole request aloud.
- **Paths stop at the next instruction** — folder, file, rename, copy, move
  and delete paths end at the first clause. "create a folder named prabir's
  docs on the desktop & in that folder, create two files…" now proposes one
  folder instead of using the entire sentence as a folder name (the
  `WinError 123` failure in `logs/jarvis.log`).
- **Desktop/Documents/Downloads targets** — a location in the request becomes
  `~/Desktop/...`, which is refused with the reason and the exact setting to
  change (`[tools] root`), rather than creating a folder with a nonsense name.
- **Folder inside a folder** — "create a folder named reports in drafts"
  creates `drafts/reports`.
- **Escape works without the keyboard hook** — `voice/interface.py` polls
  `GetAsyncKeyState(VK_ESCAPE)` every 30 ms while listening or speaking, so
  Escape interrupts speech in elevated or hook-restricted consoles too. The
  interrupt is logged, and the hook is still installed when available.
- **Longer voice capture** — `silence_ms` 900 → 1200 and `max_record_s`
  15 → 20, so requests are not cut at the first natural pause.
- **STT model fallback** — a missing configured Vosk path now uses the largest
  `models/vosk-model-*` folder, so installing a more accurate model needs no
  configuration; the loaded model and microphone sample rate are logged.

### Files modified

`core/intents.py` (clause-aware path extraction, location and parent folders),
`core/assistant.py` (confirmation wording), `tools/base.py` (preview cap),
`tools/__init__.py` (per-tool previews), `tools/files.py` (sandbox message),
`voice/interface.py` (Escape polling, listening/speaking flags),
`voice/stt.py` (model fallback), `voice/audio.py` (sample-rate log),
`config/settings.toml`, `tests/test_tools.py`, `tests/test_voice.py`,
`README.md`, `CHANGELOG.md`.

### Bug fixes

- **A spoken file request became a 300-character folder name** — the same
  intent swallowed the rest of the sentence and the create failed with
  `WinError 123`. The path is now the first clause only.
- **Confirmations took over a minute to speak** — the prompt echoed the whole
  request; it is now one short sentence (72 characters for the reported case,
  about 3 seconds of speech).
- **Escape did not stop speech** — the hotkey callback was not fired in that
  session; a key-state poll now sets the same interrupt that purges SAPI.

### Testing

- 172 unit tests pass (5 new: clause-aware folder parsing, parent folder,
  short confirmation, Escape interrupt on/off).
- Live smoke test against a temporary root created folders, wrote/read/lists/
  searched, renamed, copied, moved and deleted files, reported CPU/memory/
  disk/battery/processes, captured a screenshot, showed a notification, listed
  windows, ran `hostname`, rejected a disallowed command, opened and closed
  Notepad, and refused an outside path with the new hint.
- Vocal confirmation of the reported request measured 72 characters (~3 s at
  175 wpm). Escape still needs a real key press on the machine to confirm.

## v0.1.5 — 2026-11-08

**Fixes — complete replies and precise app closing** (on top of feature set
4.4; implemented and verified).

### Implemented functionality

- **Reply budgets raised** — `short` 90, `detailed` 250 and `technical` 320
  tokens (previously 50/120/160), so detailed answers reach their conclusion
  instead of stopping mid-sentence. Validated range is 20-600 tokens.
- **Automatic continuation** — `brain/output_parser.py` now reports the
  server's `finish_reason`. When a reply ends because of the token limit,
  JARVIS asks the model to continue exactly where it stopped
  (`max_continuations`, default 1, range 0-2), joins the fragments with a
  single space and drops sentences the model repeated. The finish reason is
  logged with every reply.
- **Named-only closing** — `close_app` now affects only the application named
  in the request. It asks that application's visible windows to close with
  `WM_CLOSE` (so unsaved work prompts instead of being destroyed) and only
  terminates an instance when the named application has no window at all.
- **Close-all** — new `close_all_apps` tool and intent for "close all apps",
  "close all programs", "close everything", "close all", "quit all
  applications" and "shut down all windows". It targets only applications with
  a visible top-level window, once per process, and never background, startup,
  system, shell or JARVIS's own processes. It always requires confirmation.
- **Live confirmations** — `Tool.preview` renders what a destructive action
  will actually touch (application names and window counts) instead of raw
  arguments. `close_all_apps` is listed in `/tools`.

### Files modified

`core/assistant.py` (continuation loop, repeat dedupe, preview text),
`core/config.py` (`ConversationConfig.max_continuations`, wider limits),
`core/intents.py` (close-all intent, CPU phrase gate), `brain/output_parser.py`
(`ChatResult.finish_reason`), `tools/base.py` (`Tool.preview`),
`tools/apps.py` (close policy, `select_close_targets`, `close_all_apps`,
previews, protected processes), `tools/desktop.py` (window PID enumeration,
`request_close` via `WM_CLOSE`), `tools/__init__.py` (registrations, `preview`
argument), `config/settings.toml`, `tests/test_conversation.py`,
`tests/test_intelligence.py`, `tests/test_tools.py`, `README.md`,
`CHANGELOG.md`.

### Bug fixes

- **Replies were cut mid-sentence** — the per-mode cap ended answers before
  the conclusion and the truncated text was stored as if complete. Limits are
  higher, truncation is detected and continued, and repeated continuation text
  is removed.
- **"Close notepad" terminated every instance** — including windows opened by
  the user and any unsaved document. Closing now targets the named
  application's windows and lets the application decide about unsaved work.
- **"Explain what a CPU cache does" answered with CPU usage** — the word CPU
  alone no longer triggers the system tool; a usage/load/status word is
  required too.
- Protected system, shell and JARVIS's own processes are refused or excluded,
  so a close-all request cannot shut down Explorer, the terminal running
  JARVIS or the assistant itself.

### Testing status

- 167 unit tests pass (`python -m unittest discover -s tests -v`): continuation
  after truncation, no continuation after `stop`, configurable limit, disabled
  continuation, repeat-sentence dedupe, parser `finish_reason`, updated mode
  limits, close-all route phrases, close-all confirmation, and close-target
  selection (only open apps, system/shell/own-process exclusion, one window per
  process).
- Real-model check: with `short_max_tokens` forced to 20, a reply hit the limit,
  JARVIS requested one continuation and logged `length` for both calls; the
  continuation repeated the first sentence, which the dedupe removed.
- Hardware check: `close_app("notepad")` asked exactly one Notepad window to
  close and the process exited; the `close_all_apps` preview listed only the
  open Sticky Notes window and excluded VS Code, the shell and JARVIS's own
  processes. The close-all path was previewed only, so no user application was
  closed.
- `python -m compileall` is clean; memory use is unchanged by these fixes
  (token limits and window messages only).

## v0.1.4 — 2026-11-08

**Feature set 4.4 — Computer Interaction** (Phase 3; implemented and verified).

### Implemented functionality

- **Tool registry** — `tools/base.py`: every tool declares argument names and
  types, a permission level (`safe` or `confirm`) and a timeout. The registry
  validates arguments, runs handlers, and converts missing/unknown/wrong-type
  arguments, refused actions and unexpected exceptions into spoken-safe
  `ToolResult` sentences.
- **System tools** — CPU, memory, disk and battery status plus top processes by
  memory, all from `psutil`.
- **Application and web tools** — open allowlisted applications, open files and
  folders, open http(s) websites (scheme and host validated), and close
  allowlisted applications.
- **Terminal tools** — allowlisted commands (`ipconfig`, `whoami`, `systeminfo`,
  `tasklist`, `hostname`, `ping`) run without a shell; arguments are split
  safely, length-limited and rejected when they contain shell metacharacters.
- **File tools** — read, write, create folders, rename, copy, move, delete,
  list and search inside `[tools] root`. Paths are resolved and refused when
  they escape that folder, reads are size-limited, search skips heavy folders
  (`.git`, `.venv`, `models`, …) and deletion of the root itself is refused.
- **Desktop tools** — screenshots saved under `screenshots/`, Windows tray
  notifications, and window listing/focus/minimize/maximize through user32
  with a PowerShell/.NET screenshot path (no new dependencies).
- **Clipboard tools** — copy and read through tkinter; reading requires
  confirmation.
- **Confirmation flow** — assistant answers destructive requests with the tool
  description and "Say confirm to proceed, or cancel"; yes/no, `/confirm` and
  `/cancel` execute or drop it, and any other request clears the pending action.
- **Intent routing** — natural-language patterns for every tool (system status,
  open/close, files, search, clipboard, notifications, screenshots, windows,
  commands) feed the 4.3 router; unrecognized phrasing falls back to the model.
- **Commands** — `/tools` lists every tool with its permission level; `/confirm`
  and `/cancel` drive pending actions.
- **Config** — new validated `[tools]` section: sandbox root, read and search
  limits, command timeout, allowed applications and allowed commands.

### Files created

`tools/__init__.py`, `tools/base.py`, `tools/system.py`, `tools/files.py`,
`tools/apps.py`, `tools/desktop.py`, `tools/clipboard.py`,
`tests/test_tools.py`.

### Files modified

`core/intents.py` (tool intents, entity extraction for apps/paths/files/
commands/windows, router allowlists), `core/assistant.py` (tool dispatch,
confirmation state, `/confirm`, `/cancel`, `/tools`), `core/config.py`
(+`ToolsConfig`), `config/settings.toml` (+`[tools]`), `.gitignore`
(+`screenshots/`), `README.md`, `CHANGELOG.md`.

### Bug fixes and safety notes

- **Ambiguous paths were unvalidated** — file, screenshot and clipboard paths
  are resolved before use, so `../` traversal outside the sandbox is refused.
- **Command execution avoided the shell** — arguments containing `&`, `|`, `>`,
  `%` and similar characters are rejected, so a permitted command cannot be
  chained into an unpermitted one.
- `close_app` terminates every running instance of an allowlisted application;
  this is documented in the README and the confirmation text now says so.

### Testing status

- 156 unit tests pass (`python -m unittest discover -s tests -v`): registry
  validation and failure containment, file read/write/list/search/rename/copy/
  move/delete, traversal and root-deletion refusal, read-size limits, URL
  validation, app and command allowlists, shell-character rejection, real
  `hostname` execution, PowerShell quoting, window listing, intent coverage for
  every tool phrase, confirmation/cancel flows, `/tools`, disabled tools and
  six new config validation cases.
- Hardware verification on the target machine: system info, top processes,
  window listing, clipboard copy/read round-trip, screenshot saved to
  `screenshots/screenshot-20261108-085253.png`, tray notification shown,
  Notepad opened and closed through the tools.
- End-to-end run: CPU query answered locally, a file write confirmed then
  cancelled, confirmed again and written, read back, delete confirmed and the
  file removed; exit code 0 with no model calls.

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
