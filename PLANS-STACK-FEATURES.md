# JARVIS — Complete Feature & Technology Stack Specification

**Project:** Local-First Intelligent AI Voice Assistant\
**Platform:** Windows 11\
**Programming Language:** Python\
**LLM:** Llama 3.2 1B Instruct — Q4\_K\_M\
**Inference Engine:** `llama-server.exe` (llama.cpp)\
**System RAM:** 8 GB\
**CPU:** Intel Core i3-3240\
**GPU:** Intel HD Graphics 2500 (integrated)\
**Target Assistant RAM:** Maximum 1.3 GB\
**Architecture:** Local-first, modular, tool-driven, resource-efficient

---

# 1\. Project Vision

Build a lightweight, intelligent, local-first AI voice assistant inspired by Iron Man's JARVIS.

The assistant should be capable of understanding natural-language requests, retrieving information, remembering user preferences, interacting with the computer, managing productivity, performing research, executing multi-step workflows, and providing natural voice-based conversations.

The assistant must prioritize:

1. Low memory consumption.
2. Low CPU overhead.
3. Fast response times.
4. Reliable tool execution.
5. Persistent memory.
6. Offline functionality wherever possible.
7. Modular and extensible architecture.
8. Security and user control.

## Core architectural philosophy

The LLM must NOT be responsible for performing every operation itself.

Llama 3.2 1B should act as the central language interface, intent interpreter, decision-maker and task coordinator.

External Python modules should perform deterministic operations.

Examples:

- Mathematics → Python calculator.
- Web information → Web search subsystem.
- Local files → Filesystem subsystem.
- Personal facts → SQLite memory.
- Reminders → Scheduler.
- Document reading → Document processing subsystem.
- Computer interaction → Validated OS tools.
- Complex requests → Task planner and orchestrator.

The goal is to make a small LLM capable of controlling a much larger collection of capabilities.

---

# 2\. Hardware Constraints

The application must be designed around the following hardware:

| Component | Specification |
| --- | --- |
| Operating system | Windows 11 |
| Total system RAM | 8 GB |
| CPU | Intel Core i3-3240 |
| CPU cores | 2 physical cores / 4 threads |
| CPU architecture | Ivy Bridge |
| Instruction support | AVX, SSE4.1, SSE4.2 |
| AVX2 | Not supported |
| GPU | Intel HD Graphics 2500 |
| Programming language | Python |
| Main LLM | Llama 3.2 1B Q4\_K\_M |
| LLM runtime | llama-server.exe |

## Hardware-specific requirements

- Use 64-bit Python.
- Use a llama.cpp build compatible with AVX.
- Do not assume AVX2 or FMA support.
- Use CPU inference.
- Set GPU layers to zero.
- Limit inference threads.
- Avoid simultaneous heavy neural-network inference.
- Avoid unnecessary background services.
- Avoid heavyweight AI frameworks.
- Avoid unnecessary local servers.

**Important:** Preserve the existing working `llama-server.exe` integration. Do not replace it with `llama-cpp-python`.

---

# 3\. Main LLM Configuration

## Model

Llama 3.2 1B Instruct — Q4\_K\_M GGUF.

## Inference architecture

Run the model through the existing `llama-server.exe` executable.

Python must communicate with the server using its local HTTP API.

Recommended initial configuration:

| Setting | Value |
| --- | --- |
| Model | Existing Llama 3.2 1B Q4\_K\_M GGUF |
| Context size | 2048 tokens |
| Maximum generated tokens | 256 |
| CPU threads | 2 |
| Batch threads | 2 |
| Batch size | 64 |
| Micro-batch size | 64 |
| Parallel slots | 1 |
| GPU layers | 0 |
| Host | 127.0.0.1 |
| Port | 8080 |
| Temperature | 0.4 for tool decisions |

These are starting values, not fixed requirements. Benchmark performance and memory usage before increasing context or batch sizes.

## Python-to-LLM communication

Use:

- Python `httpx`.
- Local llama-server HTTP endpoint.
- OpenAI-compatible chat completions endpoint.
- Request timeouts.
- Error handling.
- Structured JSON output when requesting tool decisions.

Maintain a single persistent HTTP client rather than creating a new client for every request.

## LLM responsibilities

The LLM should:

- Understand natural-language requests.
- Interpret conversational context.
- Classify ambiguous user intentions.
- Select appropriate tools.
- Generate structured tool arguments.
- Create task plans.
- Explain results.
- Generate natural-language responses.
- Summarize retrieved information.
- Ask clarification questions when necessary.

The LLM should not:

- Directly execute shell commands.
- Directly access unrestricted filesystems.
- Invent tool results.
- Pretend failed tasks succeeded.
- Be used for calculations that Python can perform.
- Be used as a substitute for web search.
- Be expected to remember everything without persistent memory.

---

# 4\. Complete Feature List

## LEVEL 1 — BASIC FEATURES

These features form the initial usable assistant.

### 4.1 Voice Interface

- Microphone input.
- Push-to-talk.
- Keyboard shortcut activation.
- Speech-to-text.
- Text-to-speech.
- Voice activity detection.
- Silence detection.
- Automatic recording termination.
- Voice interruption / barge-in.
- Stop speech playback.
- Text input fallback.
- Audio playback management.
- Error handling for failed transcription.
- Configurable voice.
- Configurable speech speed.
- Configurable volume.
- Basic noise tolerance.

### 4.2 Conversation Intelligence

- Natural-language conversations.
- Conversation history.
- Context-aware responses.
- Follow-up question understanding.
- Pronoun and reference resolution.
- Conversation summarization.
- Context compression.
- Configurable response length.
- Short-answer mode.
- Detailed-answer mode.
- Technical explanation mode.
- Clarification system.
- Uncertainty handling.
- Graceful failure responses.

### 4.3 Basic Intelligence

- Intent detection.
- Command classification.
- Entity extraction.
- Date and time interpretation.
- Tool selection.
- Structured JSON output.
- Input validation.
- Output validation.
- Basic confidence estimation.
- Deterministic command recognition.
- LLM fallback for ambiguous requests.

### 4.4 Computer Interaction

- Open applications.
- Close applications.
- Open websites.
- Open files.
- Open folders.
- Search files.
- Search folders.
- Create files.
- Create folders.
- Rename files.
- Copy files.
- Move files.
- Delete files with confirmation.
- Read text files.
- Write text files.
- Launch permitted terminal commands.
- Inspect running processes.
- Get CPU usage.
- Get RAM usage.
- Get disk usage.
- Get battery information when available.
- Take screenshots.
- Show Windows notifications.
- Copy text to clipboard.
- Read clipboard contents with permission.
- Manage basic application windows.

### 4.5 Internet Search

- Web search.
- Search query generation.
- Search provider abstraction.
- Multiple search queries.
- Search result extraction.
- URL deduplication.
- Result relevance filtering.
- Webpage fetching.
- Main content extraction.
- Source URL preservation.
- Search result caching.
- Current information retrieval.
- Documentation search.
- News search.
- Wikipedia lookup.
- Offline fallback.
- Search error recovery.

### 4.6 Basic Memory

- Persistent conversation history.
- Explicitly saved memories.
- User preferences.
- Frequently used application paths.
- Frequently used folders.
- Frequently used websites.
- Recent task history.
- Memory search.
- Memory editing.
- Memory deletion.
- Memory inspection.
- Temporary memory.
- Persistent memory.

### 4.7 Utilities

- Calculator.
- Unit conversion.
- Date and time.
- Timers.
- Reminders.
- Basic weather lookup through an external provider.
- Text manipulation.
- String operations.
- File-size conversion.
- System information.
- Basic formatting utilities.

---

## LEVEL 2 — MODERATE FEATURES

These features turn the assistant into a practical personal AI system.

### 4.8 Advanced Memory

- Short-term memory.
- Long-term memory.
- Episodic memory.
- Semantic memory.
- User preference learning.
- Automatic memory candidate extraction.
- Memory importance scoring.
- Memory categorization.
- Memory expiration.
- Memory conflict detection.
- Memory retrieval ranking.
- Context reconstruction.
- Cross-session memory.
- Memory editing.
- Memory deletion.
- Memory transparency.
- Explicit memory controls.

Do not automatically save every conversation as permanent memory.

### 4.9 Task Planning and Execution

- Multi-step task planning.
- Task decomposition.
- Sequential execution.
- Conditional execution.
- Task dependency management.
- Parallel execution of independent lightweight tasks.
- Task status tracking.
- Task checkpoints.
- Task retries.
- Task cancellation.
- Task resumption.
- Dynamic replanning.
- Error recovery.
- Execution verification.
- Execution history.
- User approval checkpoints.
- Background task execution.

Example:

"Organize my Downloads folder."

Expected workflow:

1. Inspect the folder.
2. Categorize files.
3. Prepare an organization plan.
4. Show the proposed changes.
5. Obtain approval.
6. Execute permitted file operations.
7. Verify the resulting file structure.
8. Report the outcome.

### 4.10 Document Intelligence

Support:

- PDF.
- DOCX.
- TXT.
- Markdown.
- CSV.
- XLSX.

Features:

- Read documents.
- Extract text.
- Extract tables.
- Summarize documents.
- Answer questions about documents.
- Compare documents.
- Extract important dates.
- Extract action items.
- Generate reports.
- Generate documents.
- Search document collections.
- Explain technical documents.
- Document conversion.
- Local document indexing.

Implement retrieval-augmented generation (RAG) using local storage.

Initial recommendation:

- SQLite metadata.
- SQLite FTS5 keyword search.
- Document chunking.
- Relevant passage retrieval.
- Optional embedding support in a later phase.

Do not introduce a large vector database initially.

### 4.11 Productivity Management

- To-do management.
- Task prioritization.
- Daily planning.
- Weekly planning.
- Calendar integration.
- Reminder management.
- Deadline tracking.
- Habit tracking.
- Notes management.
- Voice-based quick notes.
- Meeting summaries.
- Daily summaries.
- Pomodoro timer.
- Time tracking.
- Goal tracking.
- Recurring tasks.
- Email drafting.
- Email search through explicitly connected accounts.
- Email summarization.
- Reply drafting.
- Clipboard history.

### 4.12 Developer Assistant

- Explain source code.
- Generate code snippets.
- Debug errors.
- Inspect project directories.
- Search codebases.
- Find functions and classes.
- Explain project architecture.
- Generate documentation.
- Generate README files.
- Run tests.
- Interpret test failures.
- Format source code.
- Git status.
- Git log.
- Git change summaries.
- Commit message generation.
- Programming documentation search.
- Terminal error explanations.
- Project template generation.
- Isolated script execution.

Code execution must use a restricted environment and explicit permissions.

### 4.13 Advanced Web Research

- Multiple search providers.
- Search by domain.
- Documentation-specific search.
- Multi-query research.
- Article extraction.
- Source comparison.
- Contradiction detection.
- Fact extraction.
- Citation generation.
- Research report generation.
- Source preservation.
- Publication-date extraction where available.
- Research history.
- Saved research.
- Research retrieval.
- Information freshness assessment.

### 4.14 Application Intelligence

- Detect active application.
- Read active window title.
- Application-specific shortcuts.
- Clipboard-aware assistance.
- Contextual actions.
- Browser tab management.
- Browser page summarization.
- Search selected text.
- Explain copied errors.
- OCR on screenshots.
- Application documentation search.

---

## LEVEL 3 — ADVANCED FEATURES

These should be implemented after the basic architecture is reliable.

### 4.15 Autonomous Task Agent

- Goal interpretation.
- Automatic planning.
- Tool selection.
- Multi-stage execution.
- Dynamic replanning.
- Failure diagnosis.
- Alternative strategy selection.
- Persistent task state.
- Human approval checkpoints.
- Background execution.
- Result verification.
- Task completion summaries.
- Long-running task management.
- Automatic recovery from recoverable errors.

### 4.16 Specialized Agent Roles

Implement logical agent roles sharing the same primary LLM.

| Agent | Responsibility |
| --- | --- |
| Main agent | Coordinates the assistant |
| Research agent | Web research |
| Coding agent | Programming tasks |
| Productivity agent | Tasks and scheduling |
| File agent | Filesystem operations |
| Memory agent | Memory retrieval |
| Verification agent | Result validation |

These should initially be separate Python modules or role configurations, not separate continuously running LLM processes.

### 4.17 Visual Intelligence

- Screenshot understanding.
- OCR.
- Image-to-text extraction.
- Screenshot summarization.
- UI element detection.
- Visual comparison.
- Chart interpretation.
- Diagram understanding.
- Image metadata extraction.
- Local image search.
- Screenshot troubleshooting.

Use lightweight OCR and conventional image processing first.

Load larger visual models only if necessary.

### 4.18 Knowledge and Learning

- Personal knowledge base.
- Automatic note categorization.
- Knowledge graph.
- Topic relationship detection.
- Learning progress tracking.
- Spaced repetition.
- Flashcard generation.
- Quiz generation.
- Study planning.
- Personal knowledge search.
- Cross-document reasoning.
- Topic summaries.

### 4.19 Proactive Intelligence

- Morning briefing.
- Daily task briefing.
- Upcoming deadline notifications.
- Unfinished task reminders.
- Calendar conflict detection.
- Periodic research summaries.
- News summaries.
- Low disk space alerts.
- System performance alerts.
- Project progress summaries.
- Suggested next actions.
- Context-aware recommendations.

Use event-driven triggers and scheduled checks rather than continuous LLM reasoning.

### 4.20 Reliability and Self-Monitoring

- Failed request tracking.
- Frequently misunderstood command tracking.
- Tool selection evaluation.
- Task completion measurement.
- Response latency measurement.
- Tool failure monitoring.
- Memory usage monitoring.
- Automatic error logging.
- Configuration versioning.
- Controlled plugin registration.
- Rollback support.
- Capability discovery.
- Capability awareness.
- Offline mode.
- Privacy mode.
- Undo support for reversible actions.
- Explain mode.
- Task history.

---

# 5\. Recommended Technology Stack

Use lightweight libraries and avoid unnecessary frameworks.

## 5.1 Main Runtime

| Purpose | Technology |
| --- | --- |
| Programming language | Python 64-bit |
| LLM runtime | Existing llama-server.exe |
| LLM communication | HTTPX |
| Main orchestration | Native Python |
| Async operations | asyncio |
| Data validation | Pydantic |
| Configuration | TOML |
| Logging | Python logging |
| Process monitoring | psutil |

## 5.2 Voice Processing

| Purpose | Recommended Technology |
| --- | --- |
| Microphone input | sounddevice |
| Voice activity detection | WebRTC VAD |
| Offline STT | Vosk |
| Optional alternative STT | whisper.cpp tiny |
| Initial TTS | Windows SAPI through pyttsx3 |
| Optional neural TTS | Piper |
| Wake word, later | openWakeWord |
| Audio manipulation | NumPy, only where needed |

Initial preference:

Vosk + WebRTC VAD + Windows SAPI.

Avoid running Whisper continuously.

## 5.3 LLM

| Purpose | Technology |
| --- | --- |
| Main model | Llama 3.2 1B Instruct Q4\_K\_M |
| Inference | llama-server.exe |
| Communication | HTTPX |
| Output parsing | JSON |
| Tool validation | Pydantic |

Do not use llama-cpp-python.

Preserve the existing llama-server integration.

## 5.4 Memory and Database

| Purpose | Technology |
| --- | --- |
| Main database | SQLite |
| Keyword retrieval | SQLite FTS5 |
| Conversation history | SQLite |
| User preferences | SQLite |
| Long-term memories | SQLite |
| Task persistence | SQLite |
| Optional vector search | Small local embedding model, on demand |

Avoid a separate database server.

## 5.5 Web Intelligence

| Purpose | Technology |
| --- | --- |
| HTTP requests | HTTPX |
| Primary search | DuckDuckGo provider adapter |
| Alternative search | Brave Search API |
| Optional metasearch | External SearXNG instance |
| Article extraction | Trafilatura |
| HTML parsing | BeautifulSoup4 |
| URL handling | urllib |
| Search caching | SQLite |

Do not host SearXNG locally on this computer initially.

## 5.6 Document Processing

| Format | Technology |
| --- | --- |
| PDF | pypdf |
| DOCX | python-docx |
| XLSX | openpyxl |
| CSV | Python csv |
| TXT | Native Python |
| Markdown | Native Python |

## 5.7 Computer Interaction

| Purpose | Technology |
| --- | --- |
| Process management | subprocess |
| System information | psutil |
| File operations | pathlib, shutil |
| File monitoring | watchdog |
| Clipboard | pyperclip |
| Keyboard automation | keyboard, optional |
| Mouse automation | pyautogui, optional |
| Windows notifications | winotify |
| OCR | pytesseract, optional |

Prefer Windows-native operations and controlled APIs over unnecessary GUI automation.

## 5.8 Productivity

| Purpose | Technology |
| --- | --- |
| Reminders | APScheduler |
| Task storage | SQLite |
| Notifications | winotify |
| Date/time | datetime |
| Calendar | Optional external calendar integration |

## 5.9 Developer Tools

- pathlib.
- subprocess.
- Git CLI.
- AST module.
- Python unittest or pytest.
- Black or Ruff.
- Isolated script execution.

Do not allow unrestricted shell execution.

---

# 6\. RAM Management Architecture

**Critical requirement: The complete assistant should target a maximum peak working set of 1.3 GB.**

This must be verified by actual measurements.

An estimated initial memory allocation:

| Component | Target allowance |
| --- | --- |
| LLM weights and inference runtime | 940 MB |
| Python application | 100 MB |
| Voice input and VAD | 45 MB |
| Voice output | 65 MB |
| Database and cache | 25 MB |
| Safety margin | 125 MB |
| **Total** | **1,300 MB** |

These are budget allocations, not guaranteed measured consumption.

The model runtime allocation must be benchmarked. The actual GGUF weight size alone does not describe total inference memory.

## 6.1 Persistent components

Keep these active:

- Python orchestrator.
- llama-server.exe.
- SQLite connection management.
- Lightweight task scheduler.
- Minimal voice interface.

## 6.2 On-demand components

Load only when required:

- Whisper.
- Piper.
- OCR.
- Embedding models.
- Document processing modules.
- Browser automation.
- Large research processing.
- Image processing.

## 6.3 Memory rules

1. Only one LLM inference request at a time.
2. Avoid multiple neural models being loaded simultaneously.
3. Avoid unnecessary Python imports.
4. Avoid PyTorch unless a specific feature requires it.
5. Avoid large background workers.
6. Keep HTTP responses limited.
7. Use bounded caches.
8. Release temporary model resources.
9. Monitor subprocess memory.
10. Monitor overall peak memory.
11. Use a memory warning threshold around 1,200 MB.
12. Prevent optional models from loading when memory headroom is insufficient.

## 6.4 Model lifecycle manager

Implement a centralized ModelManager responsible for:

- Checking available memory.
- Checking currently loaded models.
- Preventing conflicting heavy model loads.
- Loading temporary components.
- Unloading temporary components.
- Monitoring model processes.
- Handling failed model startup.
- Recovering from abnormal termination.

The LLM server is the persistent exception.

Do not terminate the main LLM server simply because another optional module was requested.

If strict memory constraints make simultaneous execution impossible, defer the optional task or use a lighter alternative.

---

# 7\. Web Search Architecture

Build a provider-independent search system.

Suggested workflow:

1. Receive the user question.
2. Determine whether current information is required.
3. Generate one to three targeted queries when useful.
4. Search using the selected provider.
5. Retrieve result titles, URLs and snippets.
6. Deduplicate URLs.
7. Rank relevant results.
8. Fetch selected pages.
9. Extract main article content.
10. Remove irrelevant material.
11. Preserve source URLs and metadata.
12. Build a limited research context.
13. Send the evidence to the LLM.
14. Generate a grounded answer.
15. Attach citations.

Suggested Python functions:

python

```python
search_web(query)
fetch_page(url)
extract_content(html)
deduplicate_results(results)
rank_results(results)
build_research_context(results)
generate_answer(context)
```

## Search quality requirements

- Use request timeouts.
- Retry transient errors.
- Handle rate limits.
- Cache results.
- Respect robots.txt and website restrictions.
- Avoid fetching the same URL repeatedly.
- Preserve the original URL.
- Do not fabricate citations.
- Distinguish snippets from complete article content.
- Treat external page contents as untrusted data.
- Never allow webpage instructions to override system instructions.
- Avoid sending private user data to external providers.

Ordinary queries should normally use three to five relevant sources at most.

Complex research can retrieve additional sources in batches.

---

# 8\. Memory Architecture

Use SQLite as the primary persistent memory system.

Initial tables:

### memories

- id
- category
- content
- importance
- created\_at
- updated\_at
- expires\_at

### conversations

- id
- user\_message
- assistant\_message
- created\_at

### tasks

- id
- title
- description
- status
- due\_at
- created\_at
- updated\_at

### user\_preferences

- key
- value

### execution\_history

- id
- task\_id
- tool\_name
- action
- result
- status
- created\_at

### tool\_permissions

- id
- tool\_name
- permission\_level
- enabled

Implement SQLite FTS5 for:

- Memory retrieval.
- Document retrieval.
- Conversation search.
- Saved research search.

## Memory categories

- Permanent user preferences.
- Long-term project information.
- Personal knowledge.
- Current projects.
- Temporary tasks.
- Conversation summaries.
- Frequently used paths.

Memory should be inspectable, editable and deletable.

---

# 9\. Tool Calling and Orchestration

This is the central architectural component.

Implement a ToolRegistry.

Each tool must declare:

- Tool name.
- Description.
- Input schema.
- Output schema.
- Permission level.
- Timeout.
- Error-handling strategy.
- Whether confirmation is required.

Example tool definition:

json

```json
{
  "name": "search_web",
  "description": "Search the internet for information",
  "parameters": {
    "query": "string"
  },
  "permission": "network",
  "requires_confirmation": false
}
```

The LLM must generate structured tool requests.

Python must:

1. Parse the request.
2. Validate its schema.
3. Check permissions.
4. Request approval if necessary.
5. Execute the corresponding function.
6. Capture its result.
7. Verify success.
8. Return a structured result to the LLM.

Never execute arbitrary LLM-generated Python or shell commands directly.

## Tool execution result format

Use a consistent internal structure:

json

```json
{
  "success": true,
  "tool": "search_web",
  "data": {},
  "error": null
}
```

All tool results should distinguish successful completion, partial completion, failure and cancellation.

---

# 10\. Task Planner

Implement a lightweight task planner.

Each task should have:

- Unique ID.
- User objective.
- Task steps.
- Dependencies.
- Execution status.
- Current step.
- Result.
- Error information.
- Creation time.
- Completion time.

Task statuses:

- pending
- planning
- running
- waiting\_for\_approval
- completed
- failed
- cancelled

The planner should support:

- Sequential workflows.
- Conditional steps.
- Independent parallel steps.
- Retries.
- Cancellation.
- Persistent task state.
- Dynamic replanning.
- Completion verification.

Do not implement autonomous unrestricted execution.

Actions involving deletion, overwriting, external communication, purchases or sensitive information must have suitable approval requirements.

---

# 11\. Security and Privacy

Security must be part of the initial architecture.

Implement:

- Localhost-only LLM server binding.
- Tool permission registry.
- Filesystem access restrictions.
- Shell command allowlisting.
- Destructive action confirmation.
- User approval for external communications.
- Audit logging.
- Secrets stored outside source code.
- API key protection.
- Network timeout limits.
- Input validation.
- Output validation.
- Prompt injection resistance.
- Webpage content isolation.
- Memory inspection.
- Memory deletion.
- Privacy mode.
- Safe failure handling.

The assistant must not claim to have completed an action unless its execution result confirms success.

---

# 12\. Project Folder Structure

Use the following modular architecture.

text

```text
JARVIS/
│
├── main.py
├── requirements.txt
│
├── config/
│   ├── settings.toml
│   ├── permissions.json
│   └── tools.json
│
├── core/
│   ├── orchestrator.py
│   ├── router.py
│   ├── planner.py
│   ├── context_manager.py
│   ├── task_manager.py
│   ├── tool_registry.py
│   └── response_manager.py
│
├── brain/
│   ├── llm_client.py
│   ├── prompt_manager.py
│   └── output_parser.py
│
├── voice/
│   ├── microphone.py
│   ├── vad.py
│   ├── speech_to_text.py
│   ├── text_to_speech.py
│   └── interruption.py
│
├── memory/
│   ├── database.py
│   ├── short_term.py
│   ├── long_term.py
│   ├── memory_search.py
│   └── memory_manager.py
│
├── tools/
│   ├── web_search/
│   ├── filesystem/
│   ├── applications/
│   ├── system/
│   ├── calculator/
│   ├── documents/
│   ├── productivity/
│   └── developer/
│
├── automation/
│   ├── scheduler.py
│   ├── workflows.py
│   └── background_tasks.py
│
├── security/
│   ├── permissions.py
│   ├── validator.py
│   └── audit.py
│
├── models/
│
├── data/
│   └── jarvis.db
│
├── cache/
│
├── logs/
│
└── tests/
```

Do not create empty modules for every future feature immediately.

Build modules incrementally.

---

# 13\. Development Roadmap

Implement features in the following order.

## Phase 1 — Existing system stabilization

- Preserve existing llama-server.exe integration.
- Verify model startup.
- Verify HTTP communication.
- Verify inference reliability.
- Establish baseline RAM consumption.
- Establish inference latency.
- Create Python configuration system.
- Create logging system.
- Create structured response parsing.

Acceptance criteria:

- Model starts reliably.
- Python communicates with the server.
- Model answers requests.
- RAM usage is measured.
- Failed requests are handled gracefully.

## Phase 2 — Voice interface

- Microphone capture.
- Push-to-talk.
- WebRTC VAD.
- Vosk transcription.
- Windows SAPI TTS.
- Speech interruption.
- Audio error recovery.

Acceptance criteria:

- Voice input works offline.
- Text transcription is usable.
- Responses are spoken.
- Playback can be interrupted.
- Memory remains within budget.

## Phase 3 — Intelligence and tools

- Intent routing.
- Tool registry.
- Structured tool calling.
- Calculator.
- Application launcher.
- Filesystem tools.
- System information.
- Conversation history.
- SQLite memory.

Acceptance criteria:

- Common requests are routed without unnecessary LLM inference.
- Tool arguments are validated.
- Tool execution results are correctly reported.
- User memories persist between restarts.

## Phase 4 — Internet intelligence

- Search provider abstraction.
- DuckDuckGo integration.
- Alternative provider support.
- Search result filtering.
- Article extraction.
- Search caching.
- Source citations.
- Research context generation.

Acceptance criteria:

- Search works reliably.
- Retrieved content is distinguishable from snippets.
- Sources are preserved.
- Answers are grounded in retrieved content.
- Failed searches do not produce fabricated answers.

## Phase 5 — Productivity and documents

- Reminders.
- Scheduler.
- To-do management.
- Notes.
- PDF reading.
- DOCX reading.
- Spreadsheet reading.
- Document search.
- Document summaries.

Acceptance criteria:

- Reminders survive application restarts.
- Documents can be read locally.
- Document questions retrieve relevant passages.
- Temporary processing does not exceed memory limits.

## Phase 6 — Agent capabilities

- Multi-step planning.
- Persistent workflows.
- Background execution.
- Checkpoints.
- Retries.
- Cancellation.
- Result verification.
- Human approval.

Acceptance criteria:

- Multi-step tasks can be executed.
- Failed steps are handled.
- Tasks can be cancelled.
- Completed actions are verified.
- Destructive operations require confirmation.

## Phase 7 — Advanced JARVIS experience

- Wake-word detection.
- Long-term memory improvements.
- Proactive briefings.
- Developer assistant.
- Research reports.
- Context-aware assistance.
- Visual intelligence.
- Knowledge management.
- Adaptive personalization.
- Performance optimization.

Acceptance criteria:

- Advanced workflows work reliably.
- Background CPU usage is controlled.
- Memory remains bounded.
- Optional features can be disabled independently.

---

# 14\. Performance Benchmarking

Create a dedicated performance monitoring module.

Measure:

- Python process RSS.
- llama-server.exe process RSS.
- Total assistant working set.
- System available RAM.
- System committed memory.
- CPU utilization.
- Model loading time.
- Time to first token.
- Tokens per second.
- STT processing time.
- TTS processing time.
- Web search latency.
- Tool execution time.
- Peak memory.

Test:

1. Idle state.
2. LLM loaded.
3. LLM inference.
4. Voice transcription.
5. Voice synthesis.
6. Web search.
7. Document reading.
8. Multi-step workflow.
9. Long-running operation.
10. One-hour idle period.

Set an internal memory warning at approximately 1,200 MB.

The application must prioritize the main assistant and prevent optional components from loading when insufficient memory is available.

**Important:** The 1.3 GB target applies to the assistant, not the Windows operating system. Also distinguish process working set, private memory and shared file mappings when evaluating the budget.

---

# 15\. Coding Agent Implementation Instructions

The coding agent must follow these instructions throughout development.

## Existing implementation preservation

1. Inspect the existing project before changing anything.
2. Identify the current working llama-server.exe integration.
3. Preserve existing model paths and working configuration.
4. Do not replace llama-server.exe with llama-cpp-python.
5. Do not rewrite working modules without a demonstrated need.
6. Reuse existing code where practical.
7. Identify missing capabilities before implementing new ones.

## Architecture requirements

1. Use modular Python code.
2. Keep the LLM independent from tool execution.
3. Use HTTPX for llama-server communication.
4. Use SQLite for persistent storage.
5. Use Pydantic for tool validation.
6. Use asynchronous operations only where beneficial.
7. Avoid unnecessary background threads.
8. Limit simultaneous resource-intensive tasks.
9. Keep all tools independently testable.
10. Maintain a centralized configuration.

## Resource requirements

1. Treat 1.3 GB as the assistant's maximum peak memory target.
2. Measure actual memory before adding features.
3. Avoid heavyweight dependencies.
4. Avoid multiple persistent AI models.
5. Use on-demand model loading.
6. Release temporary resources.
7. Benchmark every major subsystem.
8. Never assume an optimization works without measurement.

## Reliability requirements

1. Handle missing files.
2. Handle network failures.
3. Handle model server failures.
4. Handle invalid JSON.
5. Handle malformed tool arguments.
6. Handle timeouts.
7. Handle interrupted tasks.
8. Log meaningful errors.
9. Verify completed actions.
10. Never fabricate execution results.

## Development workflow

For every feature:

1. Explain its purpose.
2. Identify its dependencies.
3. Implement the smallest functional version.
4. Test it independently.
5. Measure its resource usage.
6. Integrate it with the orchestrator.
7. Test its complete workflow.
8. Update documentation.

Do not implement all phases in one enormous code generation operation.

Work phase by phase.

Do not introduce additional frameworks simply because they are popular.

Prefer straightforward Python implementations over unnecessary abstractions.

---

# 16\. Final Recommended Stack Summary

| Component | Final recommendation |
| --- | --- |
| Main LLM | Llama 3.2 1B Q4\_K\_M |
| Inference | Existing llama-server.exe |
| Python communication | HTTPX |
| Voice recognition | Vosk |
| Voice activity detection | WebRTC VAD |
| Initial TTS | Windows SAPI / pyttsx3 |
| Optional TTS | Piper |
| Wake word | openWakeWord, later |
| Database | SQLite |
| Local search | SQLite FTS5 |
| Web search | DuckDuckGo adapter + optional Brave API |
| Article extraction | Trafilatura |
| HTML processing | BeautifulSoup4 |
| HTTP requests | HTTPX |
| Scheduling | APScheduler |
| System monitoring | psutil |
| File monitoring | watchdog |
| PDF extraction | pypdf |
| DOCX processing | python-docx |
| XLSX processing | openpyxl |
| OCR | Tesseract |
| Tool validation | Pydantic |
| Configuration | TOML |
| Logging | Python logging |
| Main architecture | Modular Python orchestrator |
| Agent architecture | Single LLM with specialized tool modules |

---

# 17\. Final Project Objective

The finished assistant should be capable of:

- Having natural voice conversations.
- Understanding contextual instructions.
- Retrieving current information from the internet.
- Remembering important user information.
- Interacting with the local computer.
- Reading and understanding documents.
- Managing tasks and reminders.
- Helping with programming.
- Planning multi-step workflows.
- Executing approved actions.
- Verifying task completion.
- Providing proactive notifications.
- Operating offline for supported capabilities.
- Operating with minimal background resource consumption.

The project should remain modular so that additional capabilities can be introduced without redesigning the entire system.

## Non-negotiable design principles

**1\. Small brain, powerful tools.**\
Do not rely on the 1B model for capabilities that deterministic Python functions can provide.

**2\. Local-first.**\
Personal memory, conversation history, local documents and task data should remain on the computer unless external access is explicitly needed.

**3\. Resource efficiency.**\
Keep only essential components running. Load expensive subsystems when necessary.

**4\. Reliability over apparent intelligence.**\
A tool result must be verified before the assistant reports success.

**5\. Modular development.**\
Every subsystem must be independently maintainable and testable.

**6\. Preserve existing working components.**\
In particular, retain the existing llama-server.exe inference architecture.

**Ultimate objective:** Build an efficient, intelligent, extensible JARVIS-like personal AI operating system around a small local LLM, while keeping the assistant's peak memory consumption within the 1.3 GB target.