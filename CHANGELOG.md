# Changelog

All notable changes to JARVIS are recorded here. Versions follow the project
convention: each completed feature set increments the patch number; completing
all feature sets in a phase increments the middle number and resets the patch.

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
