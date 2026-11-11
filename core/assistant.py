"""Conversation-aware assistant used by every interface.

Wraps the LLM client with multi-turn context, answer modes, history
persistence, clarification and graceful failures. Deterministic intents
(arithmetic, clock, calendar, computer tools, commands) are answered locally;
everything else falls back to the model. Destructive tools wait for an explicit
confirmation. Both the text loop and the voice interface call
:meth:`Assistant.reply`.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from brain.errors import LLMConnectionError, LLMError, LLMTimeoutError
from brain.llm_client import get_client
from core.calculator import format_number
from core.conversation import (
    MODES,
    Conversation,
    needs_clarification,
)
from core.documents import (
    DocumentError,
    DocumentService,
    normalize_text,
    summarize_document,
)
from core.history import HistoryError, HistoryStore
from core.intents import (
    TOOL_INTENTS,
    Intent,
    IntentRouter,
    plan_file_actions,
    recognize_tasks,
)
from core.learning import (
    ShortTermMemory,
    episode_summary,
    episode_topics,
    extract_candidates,
    subject_for,
)
from core.memory import (
    CATEGORIES,
    USAGE_KINDS,
    MemoryStore,
    MemoryStoreError,
    classify as classify_memory,
)
from core.habits import (
    HabitError,
    HabitService,
    format_habit,
    format_habit_detail,
    parse_habit_input,
)
from core.goals import (
    GoalError,
    GoalService,
    format_goal,
    format_goal_detail,
    parse_goal_input,
)
from core.time_tracking import (
    TimeError,
    TimeService,
    format_entry,
    format_report,
    format_week,
)
from core.calendar import (
    CalendarError,
    CalendarService,
    format_agenda,
    format_event,
    parse_event_input,
)
from core.clipboard_history import (
    ClipboardError,
    ClipboardHistoryService,
    format_entry as format_clipboard_entry,
)
from core.developer import DeveloperError, DeveloperService
from core.pomodoro import PomodoroError, PomodoroService
from core.summaries import SummaryError, SummaryService
from core.mail import (
    EmailError,
    EmailService,
    format_draft,
    format_message,
    format_summary,
    parse_email_input,
)
from core.notes import NoteError, NoteService, format_note
from core.productivity import (
    ProductivityError,
    ProductivityService,
    format_plan,
    format_todo,
    parse_todo_input,
    parse_when,
)
from core.tasks import TaskEngine, TaskError
from core.timers import TimerError, TimerService
from core.utilities import humanize_duration, parse_duration
from tools import build_registry
from tools.base import shorten
from web.base import STOPWORDS, SearchError, words

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Jarvis, a helpful personal AI assistant. Answer the user's"
    " questions directly and accurately. Never refuse a safe, normal factual"
    " question. Use the conversation so far to resolve follow-ups, pronouns and"
    " references. If you are unsure, say so plainly instead of inventing"
    " details; if a request is ambiguous, ask one short clarifying question."
)

SUMMARY_PROMPT = (
    "You compress conversations. Summarize the exchange below in at most three"
    " sentences. Keep names, facts, decisions and open questions; drop small"
    " talk. Reply with the summary only."
)

CLARIFY_REPLY = (
    "I don't have earlier context for that, sir. What are you referring to?"
)

SEARCH_SYSTEM_PROMPT = (
    "You are a research assistant. Answer the question in one or two"
    " sentences using only the evidence below, and cite the source you used"
    " in square brackets, for example [2]. Use no outside knowledge."
)

SEARCH_RETRY_PROMPT = (
    "Answer the question in one sentence using the evidence below, and cite"
    " the source number in square brackets, like [2]. Use no outside"
    " knowledge and do not repeat these instructions."
)

NOT_FOUND_MARKER = "NOT_FOUND"

DOCUMENT_SYSTEM_PROMPT = (
    "You answer questions using only the document passages below. Give a"
    " concise, direct answer and cite the passage number you used in square"
    " brackets, for example [2]. If the passages do not contain the answer,"
    " reply exactly NOT_FOUND. Do not use outside knowledge."
)

DOCUMENT_EXPLAIN_PROMPT = (
    "You explain documents clearly to a non-specialist. Use only the supplied"
    " text. Start with a one-sentence purpose, then explain the structure and"
    " the important technical terms, decisions or risks. Keep it under 250"
    " words and do not invent facts."
)

DOCUMENT_WRITE_PROMPT = (
    "You write clear, well-structured documents. Write the requested document"
    " in plain Markdown with a title and useful headings. Do not mention that"
    " you are an AI. Return the document text only."
)

DOCUMENT_DISABLED = "Document intelligence is disabled, sir."
DOCUMENT_EMPTY = (
    "I couldn't find anything about that in the indexed documents, sir."
    " Run /documents index PATH first if the document is not indexed yet."
)


def _has_citation(answer, count):
  """True when the answer cites at least one source that exists."""
  for chunk in (answer or "").split("["):
    marker = chunk.split("]")[0].strip()
    if marker.isdigit() and 1 <= int(marker) <= count:
      return True
  return False


def _strip_preamble(answer):
  """Drop an instruction or meta sentence the model repeated back."""
  text = (answer or "").strip()
  if not text:
    return ""
  paragraphs = [part.strip() for part in text.split("\n") if part.strip()]
  while len(paragraphs) > 1:
    first = paragraphs[0].lower()
    if "question" in first and (
        "evidence" in first or "instruction" in first
    ):
      paragraphs.pop(0)
      continue
    break
  cleaned = "\n".join(paragraphs)
  return re.sub(r"^\s*answer\s*:\s*", "", cleaned, flags=re.I).strip()


def _supported(answer, evidence):
  """True when the answer's figures and terms come from the evidence."""
  text = re.sub(r"\[\d+\]", " ", answer or "")
  source = (evidence or "").replace(",", "")
  numbers = re.findall(r"\d[\d,.]*", text)
  if numbers:
    for number in numbers:
      digits = number.replace(",", "").rstrip(".")
      whole = digits.split(".")[0]
      if digits and digits in source:
        return True
      if len(whole) > 2 and whole in source:
        return True
    return False
  terms = [
      word for word in words(text)
      if len(word) > 3 and word not in STOPWORDS
  ]
  available = set(words(source))
  return sum(1 for term in terms if term in available) >= 2

WEB_DISABLED = "Web search is disabled, sir."
MEMORY_DISABLED = "Memory is disabled, sir."
WEB_EMPTY = "I couldn't find anything reliable about that on the web, sir."
WEB_UNGROUNDED = (
    "I found sources but none of them actually answered that, sir."
)

CONTINUE_PROMPT = (
    "Continue exactly where your previous reply stopped. Do not repeat any"
    " text, do not restart the answer and do not add a greeting."
)

FAILURE_REPLIES = {
    "connection": "I cannot reach my language model, sir. Please check the server.",
    "timeout": "My language model is taking too long to respond, sir.",
    "other": "My language model returned an error, sir.",
}

HELP_TEXT = (
    " Commands: /new starts a fresh conversation, /mode short|detailed|technical"
    " changes how much I say, /web TEXT answers from web sources, /history"
    " shows recent exchanges, /memory manages saved and learned memories"
    " (list, learned, episodes, explain ID), /tasks lists durable plans"
    " (show, approve, resume, cancel, history), /intent TEXT"
    " shows the detected intent as JSON, /tools lists computer tools, /confirm"
    " and /cancel handle pending actions, /forget deletes saved history after"
    " confirmation, /documents manages local document reading, indexing,"
    " search, summarization, comparison and generation. Timers, reminders,"
    " conversions, weather and text tools work directly. /todo manages local"
    " to-dos and deadlines, /plan shows a daily or weekly agenda, /notes"
    " manages local notes with search, /habits tracks daily streaks and"
    " reminders, /goals tracks progress and target dates, and /time tracks"
    " start, stop and daily totals, and /calendar manages events and daily"
    " agendas, and /email drafts local messages and uses an explicitly"
    " configured account for search, reading and sending, /pomodoro runs work"
    " and break phases, /clipboard searches bounded copy history, /summary"
    " builds daily or meeting summaries, and /dev explains source files and"
    " inspects projects. /help lists this."
)

DOCUMENTS_HELP = (
    "Document commands: list, index [PATH], search QUERY, ask QUESTION,"
    " read PATH, summarize PATH, compare A | B, dates PATH, actions PATH,"
    " tables PATH, explain PATH, convert SOURCE | TARGET_OR_FORMAT,"
    " create PATH | TEXT, report [OUTPUT | PATH...], remove PATH, last."
    " Use /documents index Documents to index the Documents folder."
)

TODO_DISABLED = "Productivity is disabled, sir."
NOTES_DISABLED = "Notes are disabled, sir."
HABITS_DISABLED = "Habit tracking is disabled, sir."
GOALS_DISABLED = "Goal tracking is disabled, sir."
TIME_DISABLED = "Time tracking is disabled, sir."
CALENDAR_DISABLED = "Calendar is disabled, sir."
EMAIL_DISABLED = "Email is disabled, sir."
POMODORO_DISABLED = "Pomodoro is disabled, sir."
CLIPBOARD_DISABLED = "Clipboard history is disabled, sir."
DEVELOPER_DISABLED = "Developer tools are disabled, sir."

TODO_HELP = (
    "To-do commands: list, all, add TEXT [due WHEN] [priority LEVEL]"
    " [repeat daily|weekly|monthly], show ID, done ID, snooze ID [MINUTES],"
    " edit ID title|due|remind|priority|repeat VALUE, remove ID."
    " Use /plan or /plan week for a read-only agenda."
)

NOTES_HELP = (
    "Note commands: list, add TEXT [| TITLE], show ID, search QUERY,"
    " edit ID content|title VALUE, remove ID."
)

HABITS_HELP = (
    "Habit commands: list, add NAME [at TIME], show ID, done ID, undo ID,"
    " stats, edit ID name|remind VALUE, remove ID."
)

GOALS_HELP = (
    "Goal commands: list [all|completed], add TEXT [target N UNIT]"
    " [due WHEN], show ID, progress ID VALUE, increment ID VALUE,"
    " complete ID, reopen ID, edit ID name|target|unit|due VALUE,"
    " remove ID."
)

TIME_HELP = (
    "Time commands: start LABEL, stop, status, today, yesterday,"
    " report [DATE], week, list, remove ID."
)

CALENDAR_HELP = (
    "Calendar commands: today, tomorrow, agenda [DATE], list, add TITLE"
    " at WHEN [for DURATION] [| DETAILS], show ID, edit ID title|start|"
    "duration|details VALUE, remove ID."
)

EMAIL_HELP = (
    "Email commands: draft RECIPIENT [about SUBJECT] [| SUBJECT | BODY],"
    " list, show ID, edit ID to|cc|subject|body VALUE, remove ID,"
    " search QUERY, read UID, summarize UID, reply UID, send ID."
    " Remote actions need [email] imap_host or smtp_host and password_env."
)

POMODORO_HELP = (
    "Pomodoro commands: start [MINUTES], status, stop, skip."
)

CLIPBOARD_HELP = (
    "Clipboard commands: list, search QUERY, show ID, remove ID, clear."
)

SUMMARY_HELP = (
    "Summary commands: daily [DATE], meeting TITLE | NOTES."
)

DEVELOPER_HELP = (
    "Developer commands: explain PATH, inspect [PATH]."
)

def _normalize_sentence(text):
  """Reduce a sentence to plain words for repeat detection."""
  return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _drop_repeats(existing, addition):
  """Drop sentences the model repeated from the text already written."""
  seen = _normalize_sentence(existing)
  kept = []
  for sentence in re.split(r"(?<=[.!?])\s+", addition):
    clean = sentence.strip()
    if not clean:
      continue
    normalized = _normalize_sentence(clean)
    if normalized and normalized in seen:
      continue
    kept.append(clean)
  return " ".join(kept)


def _join_parts(parts):
  """Join reply fragments without gluing words or repeating sentences."""
  combined = ""
  for part in parts:
    text = (part or "").strip()
    if not text:
      continue
    if combined:
      text = _drop_repeats(combined, text)
      if not text:
        continue
    if combined and not combined.endswith((" ", "\n")):
      combined += " "
    combined += text
  return combined


NO_INTENT_JSON = (
    '{"confidence": 0.0, "entities": {}, "intent": null,'
    ' "source": "deterministic"}'
)

YES_WORDS = {
    "yes",
    "yeah",
    "yep",
    "ok",
    "okay",
    "confirm",
    "confirmed",
    "do it",
    "go ahead",
    "sure",
}
NO_WORDS = {
    "no",
    "nope",
    "cancel",
    "stop",
    "never mind",
    "nevermind",
    "don't",
    "dont",
}


class Assistant:
  """Stateful conversation front end over the local model and tools."""

  def __init__(self, settings, client=None):
    self._settings = settings
    self._config = settings.conversation
    self._client = client
    self.conversation = Conversation(self._config)
    self._session_id = uuid.uuid4().hex
    self._forget_pending = False
    self._memory_clear_pending = False
    self._pending_tool = None
    self._pending_plan = None
    self._pending_task = None
    self._episode_clear_pending = False
    self._session = ShortTermMemory(settings.memory.short_term_items)
    self._learned = set()
    self._last_episode = ""
    self._searcher = None
    self._weather = None
    self._timers = None
    self._tasks = None
    self._productivity = None
    self._notes = None
    self._habits = None
    self._goals = None
    self._time_tracking = None
    self._calendar = None
    self._email = None
    self._pomodoro = None
    self._clipboard_history = None
    self._summaries = None
    self._developer = None
    self._notifier = None
    self._documents = None
    self._last_document = ""
    if settings.documents.enabled:
      try:
        self._documents = DocumentService(settings.documents, settings.tools)
      except DocumentError as exc:
        log.warning("Document intelligence disabled: %s", exc)
    self.registry = (
        build_registry(
            settings,
            self._documents,
            self._get_productivity,
            self._get_notes,
            self._get_habits,
            self._get_goals,
            self._get_time_tracking,
            self._get_calendar,
            self._get_email,
            self._get_pomodoro,
            self._get_clipboard_history,
            self._get_summaries,
            self._get_developer,
        )
        if settings.tools.enabled
        else None
    )
    self.router = IntentRouter(
        allowed_apps=settings.tools.allowed_apps,
        allowed_commands=settings.tools.allowed_commands,
    )
    self._store = None
    if self._config.enabled:
      try:
        self._store = HistoryStore(self._config.resolved_history_db)
      except HistoryError as exc:
        log.warning("Conversation history disabled: %s", exc)
    self._memory = None
    if settings.memory.enabled:
      try:
        self._memory = MemoryStore(settings.memory.resolved_db)
      except MemoryStoreError as exc:
        log.warning("Memory disabled: %s", exc)
    if self._store is not None and self._config.resume_last_session:
      self._resume_last_session()

  @property
  def mode(self):
    return self.conversation.mode

  @property
  def session_id(self):
    return self._session_id

  def reply(self, text):
    """Answer one user message and learn from the exchange afterwards."""
    answer = self._reply(text)
    self._learn(text, answer)
    return answer

  def _reply(self, text):
    """Answer one user message (typed or transcribed) with context."""
    text = (text or "").strip()
    if not text:
      return "I didn't catch that, sir."
    if text.startswith("/"):
      return self._command(text)
    if (
        self._pending_tool is not None
        or self._pending_plan is not None
        or self._pending_task is not None
    ):
      answer = self._confirmation_reply(text)
      if answer is not None:
        return answer
      # Any other request abandons the pending action instead of surprising
      # the user later.
      self._pending_tool = None
      self._pending_plan = None
      self._pending_task = None

    if self.registry is not None:
      plan = plan_file_actions(
          text, self.router.allowed_apps, self.router.allowed_commands
      )
      if plan is not None:
        return self._propose_plan(plan, text)

    # Deterministic intents come first: short questions like "what time is it?"
    # contain reference words but are fully understood without context.
    intent = self.router.route(text)
    if self.router.is_confident(intent):
      if intent.name == "web_search":
        return self._answer_web(
            intent.entities.get("query", ""),
            user_text=text,
            wikipedia=bool(intent.entities.get("wikipedia")),
        )
      if intent.name in TOOL_INTENTS:
        answer = self._run_tool(text, intent)
        if answer is not None:
          return answer
      else:
        answer = self._answer_intent(intent, text)
        if answer is not None:
          log.info(
              "Intent %s answered locally (confidence %.2f)",
              intent.name,
              intent.confidence,
          )
          self._remember(text, answer)
          return answer

    if self._config.enabled and needs_clarification(
        text, self.conversation.has_history
    ):
      return CLARIFY_REPLY

    conversation = self.conversation
    if self._config.enabled:
      conversation.maybe_summarize(self._summarize)

    messages = conversation.messages(SYSTEM_PROMPT)
    memory_block = self._memory_context()
    if memory_block:
      messages[0]["content"] += "\n\n" + memory_block
    messages.append({"role": "user", "content": text})
    answer, ok = self._complete(messages, conversation.max_tokens())

    if ok and self._config.enabled:
      conversation.add(text, answer)
      self._record(text, answer)
    return answer

  def _learn(self, text, answer):
    """Extract durable candidates and an expiring episode from one turn."""
    user_text = " ".join((text or "").split())
    self._session.add(user_text, answer)
    if self._memory is None or not self._settings.memory.auto_learn:
      return
    if not user_text or user_text.startswith("/"):
      return
    config = self._settings.memory
    summary = episode_summary(user_text, answer)
    if summary and summary != self._last_episode:
      try:
        self._memory.add_episode(
            self._session_id,
            summary,
            episode_topics(user_text),
            ttl_days=config.episodic_ttl_days,
            max_episodes=config.episodic_items,
        )
        self._last_episode = summary
      except MemoryStoreError as exc:
        log.warning("Could not save episode: %s", exc)
    for candidate in extract_candidates(user_text):
      if candidate.confidence < config.learn_min_confidence:
        continue
      key = candidate.content.lower()
      if key in self._learned:
        continue
      existing = self._memory.find_by_subject(candidate.subject)
      if (
          existing
          and existing.get("source") == "explicit"
          and existing["content"].lower() != key
      ):
        log.info(
            "Kept explicit memory #%s over a learned conflict",
            existing["id"],
        )
        continue
      try:
        memory_id, created = self._memory.add(
            candidate.category,
            candidate.content,
            importance=candidate.importance,
            source="learned",
            confidence=candidate.confidence,
            subject=candidate.subject,
            expires_at=self._expiry(config.learned_ttl_days),
        )
      except MemoryStoreError as exc:
        log.warning("Could not learn a memory: %s", exc)
        continue
      self._learned.add(key)
      if created:
        log.info(
            "Learned memory #%s (%s, importance %s, confidence %.2f)",
            memory_id,
            candidate.subject or "no subject",
            candidate.importance,
            candidate.confidence,
        )

  @staticmethod
  def _expiry(days):
    return (
        datetime.now(timezone.utc) + timedelta(days=max(1, int(days)))
    ).isoformat(timespec="seconds")

  def set_notifier(self, callback):
    """Route timer and reminder notifications through the interface."""
    self._notifier = callback
    if self._timers is not None:
      self._timers.set_notifier(callback)
    if self._tasks is not None:
      self._tasks.set_notifier(callback)

  def close(self):
    if self._productivity is not None:
      self._productivity.close()
      self._productivity = None
    if self._notes is not None:
      self._notes.close()
      self._notes = None
    if self._habits is not None:
      self._habits.close()
      self._habits = None
    if self._goals is not None:
      self._goals.close()
      self._goals = None
    if self._time_tracking is not None:
      self._time_tracking.close()
      self._time_tracking = None
    if self._calendar is not None:
      self._calendar.close()
      self._calendar = None
    if self._email is not None:
      self._email.close()
      self._email = None
    if self._pomodoro is not None:
      self._pomodoro.close()
      self._pomodoro = None
    if self._clipboard_history is not None:
      self._clipboard_history.close()
      self._clipboard_history = None
    if self._documents is not None:
      self._documents.close()
      self._documents = None
    if self._tasks is not None:
      self._tasks.close()
      self._tasks = None
    if self._timers is not None:
      self._timers.close()
      self._timers = None
    if self._weather is not None:
      self._weather.close()
      self._weather = None
    if self._searcher is not None:
      self._searcher.close()
      self._searcher = None
    if self._store is not None:
      self._store.close()
      self._store = None
    if self._memory is not None:
      self._memory.close()
      self._memory = None

  # -- utilities, weather and timers ------------------------------------

  def _get_weather(self):
    """Build the weather client on first use so chatting stays light."""
    if self._weather is None and self._settings.weather.enabled:
      try:
        from core.weather import WeatherClient

        self._weather = WeatherClient(self._settings.weather)
      except Exception as exc:  # a bad client must not break chatting
        log.error("Weather unavailable: %s", exc)
        return None
    return self._weather

  def _get_timers(self):
    """Start the scheduler thread only when a timer is first requested."""
    if self._timers is None:
      try:
        self._timers = TimerService(
            self._settings.memory.resolved_db, notify=self._notifier
        )
      except TimerError as exc:
        log.error("Timers unavailable: %s", exc)
        return None
    return self._timers

  def _get_productivity(self):
    """Build the to-do service lazily and share the timer scheduler."""
    if not self._settings.productivity.enabled:
      return None
    if self._productivity is None:
      timers = self._get_timers()
      try:
        self._productivity = ProductivityService(
            self._settings.productivity, timers=timers
        )
      except ProductivityError as exc:
        log.error("Productivity unavailable: %s", exc)
        return None
    return self._productivity

  def _get_notes(self):
    """Build the note service lazily; it uses its own SQLite tables."""
    if not self._settings.notes.enabled:
      return None
    if self._notes is None:
      try:
        self._notes = NoteService(self._settings.notes)
      except NoteError as exc:
        log.error("Notes unavailable: %s", exc)
        return None
    return self._notes

  def _get_habits(self):
    """Build the habit service lazily and reuse the timer scheduler."""
    if not self._settings.habits.enabled:
      return None
    if self._habits is None:
      timers = self._get_timers()
      try:
        self._habits = HabitService(self._settings.habits, timers=timers)
      except HabitError as exc:
        log.error("Habits unavailable: %s", exc)
        return None
    return self._habits

  def _get_goals(self):
    """Build the goal service lazily; it needs no timer or worker."""
    if not self._settings.goals.enabled:
      return None
    if self._goals is None:
      try:
        self._goals = GoalService(self._settings.goals)
      except GoalError as exc:
        log.error("Goals unavailable: %s", exc)
        return None
    return self._goals

  def _get_time_tracking(self):
    """Build the time service lazily; elapsed time comes from timestamps."""
    if not self._settings.time.enabled:
      return None
    if self._time_tracking is None:
      try:
        self._time_tracking = TimeService(self._settings.time)
      except TimeError as exc:
        log.error("Time tracking unavailable: %s", exc)
        return None
    return self._time_tracking

  def _get_calendar(self):
    """Build the calendar service lazily; no external service is used."""
    if not self._settings.calendar.enabled:
      return None
    if self._calendar is None:
      try:
        self._calendar = CalendarService(self._settings.calendar)
      except CalendarError as exc:
        log.error("Calendar unavailable: %s", exc)
        return None
    return self._calendar

  def _get_email(self):
    """Build the email service lazily; local drafts need no account."""
    if not self._settings.email.enabled:
      return None
    if self._email is None:
      try:
        self._email = EmailService(self._settings.email)
      except EmailError as exc:
        log.error("Email unavailable: %s", exc)
        return None
    return self._email

  def _get_pomodoro(self):
    """Build the Pomodoro controller over the shared timer scheduler."""
    if not self._settings.pomodoro.enabled:
      return None
    if self._pomodoro is None:
      timers = self._get_timers()
      if timers is None:
        return None
      try:
        self._pomodoro = PomodoroService(timers, self._settings.pomodoro)
      except PomodoroError as exc:
        log.error("Pomodoro unavailable: %s", exc)
        return None
    return self._pomodoro

  def _get_clipboard_history(self):
    """Build the bounded clipboard history store lazily."""
    if not self._settings.clipboard.enabled:
      return None
    if self._clipboard_history is None:
      try:
        self._clipboard_history = ClipboardHistoryService(
            self._settings.clipboard
        )
      except ClipboardError as exc:
        log.error("Clipboard history unavailable: %s", exc)
        return None
    return self._clipboard_history

  def _get_summaries(self):
    """Build the read-only summary service over existing providers."""
    if self._summaries is None:
      self._summaries = SummaryService(
          self._get_productivity,
          self._get_calendar,
          self._get_time_tracking,
          self._get_habits,
      )
    return self._summaries

  def _get_developer(self):
    """Build the read-only developer assistant lazily."""
    if not self._settings.developer.enabled:
      return None
    if self._developer is None:
      self._developer = DeveloperService(
          self._settings.developer, self._settings.tools
      )
    return self._developer

  # -- web search -------------------------------------------------------

  def _get_tasks(self):
    """Build the durable task engine only when a plan is first requested."""
    if not self._settings.tasks.enabled:
      return None
    if self._tasks is None:
      if self.registry is None:
        return None
      try:
        self._tasks = TaskEngine(
            self._settings.tasks,
            self.registry,
            notifier=self._notifier,
        )
      except TaskError as exc:
        log.error("Task planning unavailable: %s", exc)
        return None
    return self._tasks

  def _get_searcher(self):
    """Build the searcher on first use so text-only sessions stay light."""
    if self._searcher is None and self._settings.web.enabled:
      try:
        from web.search import Searcher

        self._searcher = Searcher(self._settings.web)
      except Exception as exc:  # a broken cache must not break chatting
        log.error("Web search unavailable: %s", exc)
        return None
    return self._searcher

  def _answer_web(self, query, user_text=None, wikipedia=False):
    """Answer a question from fetched web evidence, with sources."""
    query = " ".join((query or "").split())
    if not query:
      return "What should I search the web for, sir?"
    searcher = self._get_searcher()
    if searcher is None:
      return WEB_DISABLED

    try:
      bundle = searcher.search(query, wikipedia=wikipedia)
    except SearchError as exc:
      log.warning("Web search failed for %r: %s", query, exc)
      return f"I couldn't search the web, sir: {exc}."

    if bundle.empty:
      return WEB_EMPTY

    answer, ok = self._complete(
        self._search_messages(
            query, bundle.evidence, SEARCH_SYSTEM_PROMPT
        ),
        self.conversation.max_tokens(),
    )
    if not ok:
      return answer
    answer = _strip_preamble(answer)

    if not self._grounded(answer, bundle):
      log.info("Retrying search answer for %r with a direct prompt", query)
      answer, ok = self._complete(
          self._search_messages(
              query, bundle.wide_evidence(), SEARCH_RETRY_PROMPT
          ),
          self.conversation.max_tokens(),
      )
      if not ok:
        return answer
      answer = _strip_preamble(answer)

    if not self._grounded(answer, bundle):
      log.info("Search evidence did not answer %r", query)
      return WEB_UNGROUNDED

    final = f"{answer.strip()}\n\n{self._sources(bundle.sources)}"
    self._remember(user_text or query, final)
    log.info(
        "Answered %r from %s source(s) via %s",
        query,
        len(bundle.results),
        bundle.provider,
    )
    return final

  def _search_messages(self, query, evidence, system):
    """Messages that keep the model inside the fetched evidence."""
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"Question: {query}\n\nEvidence:\n{evidence}",
        },
    ]

  def _grounded(self, answer, bundle):
    """Whether an answer cites a real source and stays within the evidence."""
    if not answer or NOT_FOUND_MARKER in answer.upper():
      return False
    sources = len(bundle.results[:3])
    return _has_citation(answer, sources) and _supported(
        answer, bundle.evidence
    )

  def _sources(self, results, limit=3):
    """Numbered source list matching the citations in the answer."""
    lines = []
    for index, result in enumerate(results[:limit], 1):
      title = result.title or result.url
      lines.append(f"[{index}] {title} - {result.url}")
    if not lines:
      return "Sources: none"
    return "Sources:\n" + "\n".join(lines)

  # -- computer tools ---------------------------------------------------

  def _run_tool(self, text, intent):
    registry = self.registry
    if registry is None:
      if intent.name.startswith("document_"):
        return DOCUMENT_DISABLED
      return None
    tool = registry.get(intent.name)
    if tool is None:
      if intent.name.startswith("document_"):
        return DOCUMENT_DISABLED
      log.info("No tool registered for intent %s", intent.name)
      return None
    arguments = dict(intent.entities)

    if tool.permission == "confirm":
      self._pending_tool = (intent.name, arguments, text)
      # One short sentence: a long read-out is slow and useless when spoken.
      return f"{tool.describe(arguments)}. Please confirm to continue."

    result = registry.execute(intent.name, arguments)
    if result.ok:
      log.info("Tool %s completed", intent.name)
      self._note_usage(intent.name, arguments)
      self._remember(text, result.text)
    else:
      log.info("Tool %s rejected the request: %s", intent.name, result.error)
    return result.text

  def _confirmation_reply(self, text):
    lowered = text.strip().lower().rstrip(".!")
    if lowered in YES_WORDS:
      return self._confirm_pending()
    if lowered in NO_WORDS:
      self._cancel_pending()
      return "Cancelled, sir."
    return None

  def _cancel_pending(self):
    """Drop pending actions and cancel a task still awaiting approval."""
    task_id, self._pending_task = self._pending_task, None
    self._pending_tool = None
    self._pending_plan = None
    if task_id is None:
      return
    tasks = self._get_tasks()
    if tasks is not None:
      try:
        tasks.cancel(task_id)
      except TaskError as exc:
        log.warning("Could not cancel pending task: %s", exc)

  def _confirm_pending(self):
    if self._pending_task is not None:
      task_id, self._pending_task = self._pending_task, None
      tasks = self._get_tasks()
      if tasks is None:
        return "Task planning is disabled, sir."
      try:
        task = tasks.approve(task_id)
      except TaskError as exc:
        return f"I couldn't start that task, sir: {exc}."
      return self._task_report(task)
    if self._pending_plan is not None:
      return self._run_plan()
    pending, self._pending_tool = self._pending_tool, None
    if pending is None:
      return "There is nothing waiting for confirmation, sir."
    name, arguments, user_text = pending
    if name == "clipboard_delete" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_clipboard_history()
      if service is None:
        return CLIPBOARD_DISABLED
      answer = self._clipboard_delete_text(
          service, str(arguments.get("id", ""))
      )
      self._remember(user_text, answer)
      return answer
    if name == "clipboard_clear" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_clipboard_history()
      if service is None:
        return CLIPBOARD_DISABLED
      answer = self._clipboard_clear_text(service)
      self._remember(user_text, answer)
      return answer
    if name == "email_send" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_email()
      if service is None:
        return EMAIL_DISABLED
      answer = self._email_send_text(service, str(arguments.get("id", "")))
      self._remember(user_text, answer)
      return answer
    if name == "email_delete" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_email()
      if service is None:
        return EMAIL_DISABLED
      answer = self._email_delete_text(service, str(arguments.get("id", "")))
      self._remember(user_text, answer)
      return answer
    if name == "calendar_delete" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_calendar()
      if service is None:
        return CALENDAR_DISABLED
      answer = self._calendar_delete(service, str(arguments.get("id", "")))
      self._remember(user_text, answer)
      return answer
    if name == "time_delete" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_time_tracking()
      if service is None:
        return TIME_DISABLED
      answer = self._time_delete(service, str(arguments.get("id", "")))
      self._remember(user_text, answer)
      return answer
    if name == "goal_delete" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_goals()
      if service is None:
        return GOALS_DISABLED
      answer = self._goal_delete(service, str(arguments.get("id", "")))
      self._remember(user_text, answer)
      return answer
    if name == "habit_delete" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_habits()
      if service is None:
        return HABITS_DISABLED
      answer = self._habit_delete(service, str(arguments.get("id", "")))
      self._remember(user_text, answer)
      return answer
    if name == "note_delete" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_notes()
      if service is None:
        return NOTES_DISABLED
      answer = self._note_delete(service, str(arguments.get("id", "")))
      self._remember(user_text, answer)
      return answer
    if name == "todo_delete" and (
        self.registry is None or self.registry.get(name) is None
    ):
      service = self._get_productivity()
      if service is None:
        return TODO_DISABLED
      answer = self._todo_delete(service, str(arguments.get("id", "")))
      self._remember(user_text, answer)
      return answer
    if self.registry is None:
      return "Computer tools are disabled, sir."
    result = self.registry.execute(name, arguments)
    if result.ok:
      log.info("Confirmed tool %s completed", name)
      self._note_usage(name, arguments)
      self._remember(user_text, result.text)
    else:
      log.info("Confirmed tool %s failed: %s", name, result.error)
    return result.text

  # -- multi-step file plans --------------------------------------------

  def _propose_plan(self, plan, text):
    """Ask once for an ordered list of file actions."""
    self._pending_plan = (plan, text)
    self._pending_tool = None
    labels = [
        self.registry.get(name).describe(arguments)
        for name, arguments, _clause in plan
    ]
    if len(labels) > 1:
      labels = [labels[0]] + [
          label[0].lower() + label[1:] for label in labels[1:]
      ]
    if len(labels) > 2:
      summary = f"{labels[0]}, {labels[1]} and {len(labels) - 2} more"
    else:
      summary = " and ".join(labels)
    log.info("Proposed a %s-step file plan", len(plan))
    return f"{shorten(summary)}. Please confirm to continue."

  def _run_plan(self):
    """Run the confirmed plan in order, stopping at the first failure."""
    pending, self._pending_plan = self._pending_plan, None
    if pending is None:
      return "There is nothing waiting for confirmation, sir."
    plan, user_text = pending
    results = []
    for index, (name, arguments, _clause) in enumerate(plan, 1):
      result = self.registry.execute(name, arguments)
      if not result.ok:
        log.info(
            "Plan step %s/%s (%s) failed: %s",
            index,
            len(plan),
            name,
            result.error,
        )
        results.append(f"Step {index} failed: {result.text}")
        break
      log.info("Plan step %s/%s (%s) completed", index, len(plan), name)
      self._note_usage(name, arguments)
      results.append(result.text)
    answer = " ".join(results)
    self._remember(user_text, answer)
    return answer

  # -- durable tasks ----------------------------------------------------

  def _task_organize(self, intent, background=None):
    tasks = self._get_tasks()
    if tasks is None:
      return "Task planning is disabled, sir."
    path = intent.entities.get("path") or "Downloads"
    if background is None:
      background = bool(intent.entities.get("background"))
    try:
      task = tasks.create_organize(path, background=background)
    except TaskError as exc:
      return f"I couldn't plan that, sir: {exc}."
    files = int(task.context.get("files") or 0)
    label = task.context.get("label") or path
    if not files:
      tasks.cancel(task.id)
      return f"{label} has no files to organize, sir."
    categories = task.context.get("categories") or {}
    self._pending_task = task.id
    self._pending_tool = None
    self._pending_plan = None
    mode = " in the background" if task.background else ""
    return (
        f"Task #{task.id} will organize {files} files into"
        f" {len(categories)} folders under {label}{mode}."
        " Run /confirm to approve."
    )

  def _task_plan(self, request, background=False):
    tasks = self._get_tasks()
    if tasks is None:
      return "Task planning is disabled, sir."
    request = " ".join(str(request or "").split())
    if not request:
      return "What file steps should I plan, sir?"
    nested = recognize_tasks(request)
    if nested is not None and nested.name == "task_organize":
      nested_background = background or bool(
          nested.entities.get("background")
      )
      return self._task_organize(nested, nested_background)
    plan = plan_file_actions(
        request, self.router.allowed_apps, self.router.allowed_commands
    )
    if plan is None:
      return (
          "I can turn explicit multi-step file actions into a task, sir."
          " Try 'as a task, create a folder named drafts and in that folder"
          " write hello to file notes.txt'."
      )
    try:
      task = tasks.create_file_plan(
          request, plan, background=background
      )
    except TaskError as exc:
      return f"I couldn't plan that task, sir: {exc}."
    self._pending_task = task.id
    self._pending_tool = None
    self._pending_plan = None
    mode = " in the background" if task.background else ""
    return (
        f"Task #{task.id} will run {len(plan)} file steps{mode}."
        " Run /confirm to approve."
    )

  def _task_list_text(self, tasks):
    rows = tasks.list(limit=10)
    if not rows:
      return "You have no tasks yet, sir."
    return "Tasks:\n" + "\n".join(
        f"[{task.id}] {task.status} {task.goal}"
        f" ({task.done_count()}/{len(task.steps)})"
        for task in rows
    )

  def _task_detail(self, task):
    lines = [
        f"Task #{task.id}: {task.goal}",
        f"Status: {task.status}",
        f"Progress: {task.done_count()}/{len(task.steps)} steps",
    ]
    if task.error:
      lines.append(f"Error: {task.error}")
    for index, step in enumerate(task.steps, 1):
      extra = f" (attempt {step.attempts})" if step.attempts else ""
      lines.append(f"{index}. [{step.status}] {step.name}{extra}")
      if step.error:
        lines.append(f"   {shorten(step.error, 120)}")
    return "\n".join(lines)

  def _task_report(self, task):
    if task is None:
      return "There is no task with that id, sir."
    if task.status == "completed":
      checks = task.verified_count()
      if task.kind == "organize":
        files = int(task.context.get("files") or 0)
        categories = len(task.context.get("categories") or {})
        return (
            f"Task #{task.id} completed: {files} files into"
            f" {categories} folders, {checks} checks passed."
        )
      return (
          f"Task #{task.id} completed:"
          f" {task.done_count()} file steps, {checks} checks passed."
      )
    if task.status == "failed":
      return (
          f"Task #{task.id} failed: {task.error}"
          f" Run /tasks resume {task.id} to retry."
      )
    if task.status == "awaiting_step":
      return (
          f"Task #{task.id} is waiting at step {task.current_step}."
          f" Run /tasks approve-step {task.id} to continue."
      )
    if task.status == "awaiting_approval":
      return (
          f"Task #{task.id} is waiting for approval."
          f" Run /tasks approve {task.id}."
      )
    if task.status == "cancelled":
      return f"Task #{task.id} was cancelled."
    if task.status == "paused":
      return (
          f"Task #{task.id} is paused after an interruption."
          f" Run /tasks resume {task.id} to continue."
      )
    if task.background:
      return (
          f"Task #{task.id} started in the background."
          f" Run /tasks show {task.id} for progress."
      )
    return f"Task #{task.id} is {task.status}. /tasks show {task.id}."

  def _task_history_text(self, tasks):
    rows = tasks.events(limit=self._settings.tasks.history_items)
    if not rows:
      return "There is no task history yet, sir."
    return "Task history:\n" + "\n".join(
        f"{row['created_at'][5:16]} #{row['task_id']} {row['kind']}:"
        f" {shorten(row['message'], 100)}"
        for row in rows
    )

  def _task_reply(self, intent):
    tasks = self._get_tasks()
    if tasks is None:
      return "Task planning is disabled, sir."
    if intent.name == "task_list":
      return self._task_list_text(tasks)
    if intent.name == "task_history":
      return self._task_history_text(tasks)
    try:
      task = tasks.get(int(intent.entities.get("id") or 0))
    except (TypeError, ValueError):
      task = None
    if task is None:
      return "There is no task with that id, sir."
    if intent.name == "task_show":
      return self._task_detail(task)
    try:
      if intent.name == "task_approve":
        task = tasks.approve(task.id)
      elif intent.name == "task_resume":
        task = tasks.resume(task.id)
      elif intent.name == "task_cancel":
        task = tasks.cancel(task.id)
    except TaskError as exc:
      return f"I couldn't update that task, sir: {exc}."
    return self._task_report(task)

  def _tasks_command(self, parts):
    tasks = self._get_tasks()
    if tasks is None:
      return "Task planning is disabled, sir."
    action = parts[0].lower() if parts else "list"
    rest = parts[1:]
    if action in {"list", "ls"}:
      return self._task_list_text(tasks)
    if action == "plan":
      request = " ".join(rest)
      if not request:
        return "Usage: /tasks plan TEXT"
      background = " in the background" in f" {request.lower()}"
      request = re.sub(
          r"\s+in\s+(?:the\s+)?background\b",
          "",
          request,
          flags=re.I,
      ).strip()
      return self._task_plan(request, background)
    if action in {"history", "log"}:
      return self._task_history_text(tasks)
    if action in {"show", "status", "details"}:
      if len(rest) != 1 or not rest[0].isdigit():
        return "Usage: /tasks show ID"
      task = tasks.get(int(rest[0]))
      return (
          self._task_detail(task)
          if task is not None
          else "There is no task with that id, sir."
      )
    if action in {"approve", "run", "start", "approve-step"}:
      if len(rest) != 1 or not rest[0].isdigit():
        return f"Usage: /tasks {action} ID"
      try:
        task = (
            tasks.approve_step(int(rest[0]))
            if action == "approve-step"
            else tasks.approve(int(rest[0]))
        )
      except TaskError as exc:
        return f"I couldn't start that task, sir: {exc}."
      return self._task_report(task)
    if action in {"resume", "retry"}:
      if len(rest) != 1 or not rest[0].isdigit():
        return "Usage: /tasks resume ID"
      try:
        task = tasks.resume(int(rest[0]))
      except TaskError as exc:
        return f"I couldn't resume that task, sir: {exc}."
      return self._task_report(task)
    if action in {"cancel", "stop"}:
      if len(rest) != 1 or not rest[0].isdigit():
        return "Usage: /tasks cancel ID"
      try:
        task = tasks.cancel(int(rest[0]))
      except TaskError as exc:
        return f"I couldn't cancel that task, sir: {exc}."
      return self._task_report(task)
    return (
        "Task commands: list, show ID, approve ID, approve-step ID,"
        " resume ID, cancel ID, history."
    )

  # -- deterministic intents -------------------------------------------

  def _answer_intent(self, intent, user_text=None):
    if intent.name == "time":
      now = datetime.now()
      return (
          f"It is {now.strftime('%H:%M')} on"
          f" {now.strftime('%A, %d %B %Y')}, sir."
      )
    if intent.name == "date":
      return self._answer_date(intent)
    if intent.name == "math":
      if "result" not in intent.entities:
        reason = intent.entities.get("error", "unsupported expression")
        return f"I can't calculate that, sir: {reason}."
      return f"That is {format_number(intent.entities['result'])}."
    if intent.name == "remember_memory":
      return self._remember_memory(intent)
    if intent.name in {
        "search_memory",
        "list_memory",
        "delete_memory",
        "learned_memory",
        "memory_episodes",
    }:
      return self._memory_reply(intent)
    if intent.name == "unit_convert":
      return self._answer_conversion(intent)
    if intent.name == "text_transform":
      return self._answer_text(intent)
    if intent.name == "format_size":
      return f"That is {intent.entities.get('result')}."
    if intent.name == "weather":
      return self._answer_weather(intent)
    if intent.name == "task_organize":
      return self._task_organize(intent)
    if intent.name == "task_plan":
      return self._task_plan(
          intent.entities.get("request", ""),
          bool(intent.entities.get("background")),
      )
    if intent.name in {
        "task_list",
        "task_show",
        "task_approve",
        "task_resume",
        "task_cancel",
        "task_history",
    }:
      return self._task_reply(intent)
    if intent.name in {
        "timer_set",
        "reminder_set",
        "timer_list",
        "timer_cancel",
    }:
      return self._answer_timers(intent)
    if intent.name in {"plan_day", "plan_week"}:
      return self._answer_plan(intent)
    if intent.name.startswith("time_"):
      return self._answer_time_tracking(intent, user_text or "")
    if intent.name.startswith("calendar_"):
      return self._answer_calendar(intent, user_text or "")
    if intent.name.startswith("email_"):
      return self._answer_email(intent, user_text or "")
    if intent.name.startswith("pomodoro_"):
      return self._answer_pomodoro(intent)
    if intent.name.startswith("clipboard_"):
      return self._answer_clipboard_history(intent, user_text or "")
    if intent.name.startswith("summary_"):
      return self._answer_summaries(intent)
    if intent.name.startswith("developer_"):
      return self._answer_developer(intent)
    if intent.name.startswith("goal_"):
      return self._answer_goals(intent, user_text or "")
    if intent.name.startswith("habit_"):
      return self._answer_habits(intent, user_text or "")
    if intent.name.startswith("note_"):
      return self._answer_notes(intent, user_text or "")
    if intent.name.startswith("todo_"):
      return self._answer_todo(intent, user_text or "")
    if intent.name.startswith("document_"):
      return self._answer_document(intent, user_text or "")
    return None

  def _answer_conversion(self, intent):
    if "error" in intent.entities:
      return f"I can't convert that, sir: {intent.entities['error']}."
    value = format_number(intent.entities.get("value", 0))
    result = intent.entities.get("result")
    unit = intent.entities.get("unit", "")
    source = intent.entities.get("source", "")
    return f"{value} {source} is {result} {unit}, sir."

  def _answer_text(self, intent):
    if "error" in intent.entities:
      return f"I can't do that, sir: {intent.entities['error']}."
    operation = intent.entities.get("operation", "")
    result = intent.entities.get("result", "")
    if operation in {"words", "characters", "chars", "letters"}:
      noun = "words" if operation == "words" else "characters"
      if result == "1":
        noun = noun[:-1]
      return f"The text has {result} {noun}, sir."
    return result

  def _answer_weather(self, intent):
    from core.weather import WeatherError

    if not self._settings.weather.enabled:
      return "Weather is disabled, sir."
    location = (
        intent.entities.get("location")
        or self._settings.weather.default_location
    )
    if not location:
      return "Which place should I check the weather for, sir?"
    weather = self._get_weather()
    if weather is None:
      return "Weather is unavailable, sir."
    try:
      data = weather.current(location)
    except WeatherError as exc:
      log.warning("Weather lookup failed for %r: %s", location, exc)
      return f"I couldn't get the weather, sir: {exc}."
    answer = (
        f"In {data['place']}, it is {data['temperature']}"
        f" {data['unit']} and {data['description']}."
    )
    details = []
    if data.get("feels_like") is not None:
      details.append(f"feels like {data['feels_like']} {data['unit']}")
    if data.get("humidity") is not None:
      details.append(f"humidity {data['humidity']}%")
    if data.get("wind") is not None:
      details.append(f"wind {data['wind']} {data['wind_unit']}")
    if details:
      answer += " It " + ", ".join(details) + "."
    if data.get("high") is not None and data.get("low") is not None:
      answer += (
          f" Today's high is {data['high']} and the low is {data['low']}."
      )
    return answer

  def _answer_timers(self, intent):
    timers = self._get_timers()
    if timers is None:
      return "Timers are unavailable, sir."
    if intent.name == "timer_set":
      seconds = int(intent.entities.get("seconds", 0))
      label = intent.entities.get("label", "")
      try:
        entry = timers.add_timer(seconds, label)
      except TimerError as exc:
        return f"I can't set that timer, sir: {exc}."
      message = f"Timer set for {humanize_duration(seconds)}"
      if entry.label:
        message += f" ({entry.label})"
      return message + ", sir."
    if intent.name == "reminder_set":
      try:
        due = datetime.fromisoformat(intent.entities.get("due_at", ""))
      except (TypeError, ValueError):
        return "I couldn't understand when to remind you, sir."
      try:
        entry = timers.add_reminder(due, intent.entities.get("text", ""))
      except TimerError as exc:
        return f"I can't set that reminder, sir: {exc}."
      return (
          f"I'll remind you to {entry.label} at"
          f" {entry.due_at.strftime('%H:%M')} on"
          f" {entry.due_at.strftime('%A')}, sir."
      )
    if intent.name == "timer_list":
      entries = timers.list()
      if not entries:
        return "You have no timers or reminders, sir."
      lines = []
      for entry in entries:
        label = f" {entry.label}" if entry.label else ""
        remaining = humanize_duration(entry.remaining_s)
        lines.append(f"[{entry.id}] {entry.kind}{label} in {remaining}")
      return "Pending:\n" + "\n".join(lines)
    return self._cancel_timer(timers, intent)

  def _cancel_timer(self, timers, intent):
    kind = intent.entities.get("kind", "timer")
    entries = [item for item in timers.list() if item.kind == kind]
    plural = "" if kind.endswith("s") else "s"
    if not entries:
      return f"There are no {kind}{plural} to cancel, sir."
    if intent.entities.get("all"):
      for entry in entries:
        timers.cancel(entry.id)
      return f"Cancelled {len(entries)} {kind}{plural}, sir."
    identifier = int(intent.entities.get("id") or 0)
    if identifier:
      target = next(
          (item for item in entries if item.id == identifier), None
      )
      if target is None:
        return f"I couldn't find that {kind}, sir."
    else:
      target = entries[0]
    timers.cancel(target.id)
    return f"Cancelled {kind} {target.id}, sir."

  # -- productivity to-dos ----------------------------------------------

  def _answer_pomodoro(self, intent):
    service = self._get_pomodoro()
    if service is None:
      return POMODORO_DISABLED
    try:
      if intent.name == "pomodoro_start":
        minutes = int(intent.entities.get("minutes") or 0) or None
        return self._pomodoro_text(service.start(minutes), started=True)
      if intent.name == "pomodoro_stop":
        current = service.stop()
        if not current.running:
          return "No Pomodoro session is running, sir."
        return "Pomodoro session stopped, sir."
      if intent.name == "pomodoro_skip":
        return self._pomodoro_text(service.skip(), skipped=True)
      return self._pomodoro_text(service.status())
    except PomodoroError as exc:
      return f"I couldn't change the Pomodoro, sir: {exc}."

  @staticmethod
  def _pomodoro_text(status, started=False, skipped=False):
    if not status.running:
      return "No Pomodoro session is running, sir."
    minutes, seconds = divmod(status.remaining_s, 60)
    if started:
      return (
          f"Pomodoro {status.phase_label} started for"
          f" {status.work_minutes} minutes, sir."
      )
    if skipped:
      return (
          f"Skipped to Pomodoro {status.phase_label} with"
          f" {minutes}m {seconds}s remaining, sir."
      )
    text = (
        f"Pomodoro {status.phase_label}: {minutes}m {seconds}s remaining;"
        f" {status.completed_work} work phase(s) completed, sir."
    )
    if status.recovered and status.recovery_details:
      text += " " + status.recovery_details
    return text

  def _answer_clipboard_history(self, intent, user_text=""):
    service = self._get_clipboard_history()
    if service is None:
      return CLIPBOARD_DISABLED
    if intent.name == "clipboard_list":
      return self._clipboard_list_text(service)
    if intent.name == "clipboard_search":
      return self._clipboard_search_text(
          service, intent.entities.get("query", "")
      )
    if intent.name == "clipboard_show":
      return self._clipboard_show(service, intent.entities.get("id"))
    if intent.name == "clipboard_delete":
      return self._clipboard_delete_pending(
          service, intent.entities.get("id"), user_text
      )
    if intent.name == "clipboard_clear":
      return self._clipboard_clear_pending(user_text)
    return CLIPBOARD_HELP

  def _clipboard_list_text(self, service, limit=20):
    try:
      entries = service.list(limit)
    except ClipboardError as exc:
      return f"I couldn't read clipboard history, sir: {exc}."
    if not entries:
      return "Clipboard history is empty, sir."
    return "Clipboard history:\n" + "\n".join(
        format_clipboard_entry(entry) for entry in entries
    )

  def _clipboard_search_text(self, service, query):
    try:
      entries = service.search(query)
    except ClipboardError as exc:
      return f"I couldn't search clipboard history, sir: {exc}."
    if not entries:
      return "No clipboard entries matched, sir."
    return "Clipboard search results:\n" + "\n".join(
        format_clipboard_entry(entry) for entry in entries
    )

  def _clipboard_show(self, service, identifier):
    identifier = self._email_id(identifier)
    if identifier is None:
      return "Usage: /clipboard show ID"
    try:
      entry = service.get(identifier)
    except ClipboardError as exc:
      return f"I couldn't read that clipboard entry, sir: {exc}."
    if entry is None:
      return "There is no clipboard entry with that id, sir."
    return format_clipboard_entry(entry, detail=True)

  def _clipboard_delete_text(self, service, identifier):
    identifier = self._email_id(identifier)
    if identifier is None:
      return "Usage: /clipboard remove ID"
    try:
      entry = service.delete(identifier)
    except ClipboardError as exc:
      return f"I couldn't delete that clipboard entry, sir: {exc}."
    return f"Clipboard entry #{entry.id} deleted, sir."

  def _clipboard_clear_text(self, service):
    try:
      count = service.clear()
    except ClipboardError as exc:
      return f"I couldn't clear clipboard history, sir: {exc}."
    return f"Deleted {count} clipboard entries, sir."

  def _clipboard_delete_pending(self, service, identifier, user_text):
    identifier = self._email_id(identifier)
    if identifier is None:
      return "Usage: /clipboard remove ID"
    try:
      entry = service.get(identifier)
    except ClipboardError as exc:
      return f"I couldn't read that clipboard entry, sir: {exc}."
    if entry is None:
      return "There is no clipboard entry with that id, sir."
    arguments = {"id": entry.id}
    tool = self.registry.get("clipboard_delete") if self.registry else None
    description = (
        tool.describe(arguments)
        if tool is not None
        else f"Delete clipboard entry #{entry.id}"
    )
    self._pending_tool = ("clipboard_delete", arguments, user_text)
    return f"{description}. Please confirm to continue."

  def _clipboard_clear_pending(self, user_text):
    tool = self.registry.get("clipboard_clear") if self.registry else None
    description = (
        tool.describe({}) if tool is not None else "Delete all clipboard history"
    )
    self._pending_tool = ("clipboard_clear", {}, user_text)
    return f"{description}. Please confirm to continue."

  def _answer_summaries(self, intent):
    service = self._get_summaries()
    try:
      if intent.name == "summary_meeting":
        return service.meeting(intent.entities.get("text", ""))
      return service.daily(intent.entities.get("day", ""))
    except SummaryError as exc:
      return f"I couldn't build that summary, sir: {exc}."

  def _summary_command(self, text):
    service = self._get_summaries()
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body or body.lower() in {"daily", "today", "day"}:
      return service.daily("")
    action, _separator, rest = body.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in {"meeting", "meetings"}:
      try:
        return service.meeting(rest)
      except SummaryError as exc:
        return f"I couldn't build that meeting summary, sir: {exc}."
    if action in {"tomorrow", "yesterday"} and not rest:
      return service.daily(action)
    return service.daily(rest or action)

  def _answer_developer(self, intent):
    service = self._get_developer()
    if service is None:
      return DEVELOPER_DISABLED
    try:
      if intent.name == "developer_explain":
        return service.explain_source(intent.entities.get("path", ""))[0]
      return service.inspect_project(intent.entities.get("path", "") or ".")[0]
    except DeveloperError as exc:
      return f"I couldn't explain that source, sir: {exc}."

  def _developer_command(self, text):
    service = self._get_developer()
    if service is None:
      return DEVELOPER_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    action, _separator, rest = body.partition(" ")
    action = action.lower() or "help"
    rest = rest.strip()
    if action in {"explain", "describe", "source"}:
      if not rest:
        return "Usage: /dev explain PATH"
      try:
        return service.explain_source(rest)[0]
      except DeveloperError as exc:
        return f"I couldn't explain that source, sir: {exc}."
    if action in {"inspect", "project", "tree"}:
      try:
        return service.inspect_project(rest or ".")[0]
      except DeveloperError as exc:
        return f"I couldn't inspect that project, sir: {exc}."
    return DEVELOPER_HELP

  def _pomodoro_command(self, text):
    service = self._get_pomodoro()
    if service is None:
      return POMODORO_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    action, _separator, rest = body.partition(" ")
    action = action.lower() or "status"
    rest = rest.strip()
    try:
      if action in {"start", "begin"}:
        minutes = int(rest) if rest.isdigit() else None
        return self._pomodoro_text(service.start(minutes), started=True)
      if action in {"stop", "end", "cancel"}:
        current = service.stop()
        if not current.running:
          return "No Pomodoro session is running, sir."
        return "Pomodoro session stopped, sir."
      if action in {"skip", "next"}:
        return self._pomodoro_text(service.skip(), skipped=True)
      if action in {"status", "show"}:
        return self._pomodoro_text(service.status())
      return POMODORO_HELP
    except (PomodoroError, ValueError) as exc:
      return f"I couldn't change the Pomodoro, sir: {exc}."

  def _clipboard_command(self, text):
    service = self._get_clipboard_history()
    if service is None:
      return CLIPBOARD_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body:
      return CLIPBOARD_HELP
    action, _separator, rest = body.partition(" ")
    action = action.lower() or "list"
    rest = rest.strip()
    if action in {"list", "history", "show"} and not rest:
      return self._clipboard_list_text(service)
    if action == "search":
      return self._clipboard_search_text(service, rest)
    if action in {"show", "detail", "details"}:
      return self._clipboard_show(service, rest)
    if action in {"remove", "delete", "rm"}:
      return self._clipboard_delete_pending(service, rest, text)
    if action == "clear":
      return self._clipboard_clear_pending(text)
    return CLIPBOARD_HELP

  @staticmethod
  def _email_id(value):
    text = str(value or "").strip().lstrip("#")
    return int(text) if text.isdigit() else None

  def _answer_email(self, intent, user_text=""):
    service = self._get_email()
    if service is None:
      return EMAIL_DISABLED
    if intent.name == "email_draft":
      return self._email_draft_text(
          service,
          intent.entities.get("recipient", ""),
          intent.entities.get("subject", ""),
          intent.entities.get("body", ""),
      )
    if intent.name == "email_list":
      return self._email_list_text(service)
    if intent.name == "email_show":
      return self._email_show(service, intent.entities.get("id"))
    if intent.name == "email_delete":
      return self._email_delete_pending(
          service, intent.entities.get("id"), user_text
      )
    if intent.name == "email_send":
      return self._email_send_pending(
          service, intent.entities.get("id"), user_text
      )
    if intent.name == "email_search":
      return self._email_search_text(
          service, intent.entities.get("query", "")
      )
    uid = intent.entities.get("uid")
    if intent.name == "email_read":
      return self._email_read_text(service, uid)
    if intent.name == "email_summarize":
      return self._email_summary_text(service, uid)
    if intent.name == "email_reply":
      return self._email_reply_text(service, uid)
    return EMAIL_HELP

  def _email_draft_text(
      self, service, recipient, subject="", body="", cc=""
  ):
    try:
      draft = service.add(
          recipient, subject=subject, body=body, cc=cc
      )
    except EmailError as exc:
      return f"I couldn't save that email draft, sir: {exc}."
    return f"Draft #{draft.id} saved: {format_draft(draft)}."

  def _email_list_text(self, service, status=None):
    try:
      drafts = service.list(limit=20, status=status)
    except EmailError as exc:
      return f"I couldn't list email drafts, sir: {exc}."
    if not drafts:
      return "You have no email drafts, sir."
    return "Email drafts:\n" + "\n".join(
        format_draft(draft) for draft in drafts
    )

  def _email_show(self, service, identifier):
    identifier = self._email_id(identifier)
    if identifier is None:
      return "Usage: /email show ID"
    try:
      draft = service.get(identifier)
    except EmailError as exc:
      return f"I couldn't read that draft, sir: {exc}."
    if draft is None:
      return "There is no email draft with that id, sir."
    return format_draft(draft, detail=True)

  def _email_edit(self, service, text):
    parts = str(text or "").split(maxsplit=2)
    if len(parts) < 3:
      return "Usage: /email edit ID to|cc|subject|body VALUE"
    identifier = self._email_id(parts[0])
    field = parts[1].lower()
    value = parts[2].strip()
    if identifier is None:
      return "Usage: /email edit ID to|cc|subject|body VALUE"
    if field in {"to", "recipient"}:
      fields = {"recipient": value}
    elif field in {"cc", "subject", "body"}:
      fields = {field: value}
    else:
      return "Usage: /email edit ID to|cc|subject|body VALUE"
    try:
      draft = service.update(identifier, **fields)
    except EmailError as exc:
      return f"I couldn't update that draft, sir: {exc}."
    return f"Draft #{draft.id} updated: {format_draft(draft)}."

  def _email_delete_text(self, service, identifier):
    identifier = self._email_id(identifier)
    if identifier is None:
      return "Usage: /email remove ID"
    try:
      draft = service.delete(identifier)
    except EmailError as exc:
      return f"I couldn't delete that draft, sir: {exc}."
    return f"Draft #{draft.id} removed, sir."

  def _email_send_text(self, service, identifier):
    identifier = self._email_id(identifier)
    if identifier is None:
      return "Usage: /email send ID"
    try:
      draft = service.send(identifier)
    except EmailError as exc:
      return f"I couldn't send that email, sir: {exc}."
    return f"Draft #{draft.id} sent to {draft.recipient}, sir."

  def _email_send_pending(self, service, identifier, user_text):
    identifier = self._email_id(identifier)
    if identifier is None:
      return "Usage: /email send ID"
    try:
      draft = service.get(identifier)
    except EmailError as exc:
      return f"I couldn't read that draft, sir: {exc}."
    if draft is None:
      return "There is no email draft with that id, sir."
    arguments = {"id": draft.id}
    tool = self.registry.get("email_send") if self.registry else None
    description = (
        tool.describe(arguments)
        if tool is not None
        else f"Send draft #{draft.id} to {draft.recipient}"
    )
    self._pending_tool = ("email_send", arguments, user_text)
    return f"{description}. Please confirm to continue."

  def _email_delete_pending(self, service, identifier, user_text):
    identifier = self._email_id(identifier)
    if identifier is None:
      return "Usage: /email remove ID"
    try:
      draft = service.get(identifier)
    except EmailError as exc:
      return f"I couldn't read that draft, sir: {exc}."
    if draft is None:
      return "There is no email draft with that id, sir."
    arguments = {"id": draft.id}
    tool = self.registry.get("email_delete") if self.registry else None
    description = (
        tool.describe(arguments)
        if tool is not None
        else f"Delete draft #{draft.id}"
    )
    self._pending_tool = ("email_delete", arguments, user_text)
    return f"{description}. Please confirm to continue."

  def _email_search_text(self, service, query):
    try:
      messages = service.search(query)
    except EmailError as exc:
      return f"I couldn't search email, sir: {exc}."
    if not messages:
      return "No matching email was found, sir."
    return "Email search results:\n" + "\n".join(
        format_message(message) for message in messages
    )

  def _email_read_text(self, service, uid):
    uid = self._email_id(uid)
    if uid is None:
      return "Usage: /email read UID"
    try:
      message = service.read(uid)
    except EmailError as exc:
      return f"I couldn't read that email, sir: {exc}."
    return format_message(message, detail=True)

  def _email_summary_text(self, service, uid):
    uid = self._email_id(uid)
    if uid is None:
      return "Usage: /email summarize UID"
    try:
      message, summary = service.summarize(uid)
    except EmailError as exc:
      return f"I couldn't summarize that email, sir: {exc}."
    return format_summary(message, summary)

  def _email_reply_text(self, service, uid):
    uid = self._email_id(uid)
    if uid is None:
      return "Usage: /email reply UID"
    try:
      message, draft = service.reply(uid)
    except EmailError as exc:
      return f"I couldn't draft that reply, sir: {exc}."
    return (
        f"Reply draft #{draft.id} saved for {message.sender}: "
        f"{format_draft(draft)}."
    )

  def _email_command(self, text):
    service = self._get_email()
    if service is None:
      return EMAIL_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body or body.lower() in {"help", "?"}:
      return EMAIL_HELP
    action, _separator, rest = body.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in {"draft", "add", "compose", "write"}:
      parsed = parse_email_input(rest)
      if not parsed.get("recipient"):
        return "Who should I address the email to, sir?"
      return self._email_draft_text(
          service,
          parsed["recipient"],
          parsed.get("subject", ""),
          parsed.get("body", ""),
          parsed.get("cc", ""),
      )
    if action in {"list", "drafts"}:
      return self._email_list_text(service)
    if action == "sent":
      return self._email_list_text(service, status="sent")
    if action in {"show", "detail", "details", "get"}:
      return self._email_show(service, rest)
    if action == "edit":
      return self._email_edit(service, rest)
    if action == "send":
      return self._email_send_pending(service, rest, text)
    if action in {"remove", "delete", "rm"}:
      return self._email_delete_pending(service, rest, text)
    if action == "search":
      return self._email_search_text(service, rest)
    if action == "read":
      return self._email_read_text(service, rest)
    if action in {"summarize", "summarise", "summary"}:
      return self._email_summary_text(service, rest)
    if action == "reply":
      return self._email_reply_text(service, rest)
    return EMAIL_HELP

  @staticmethod
  def _calendar_id(value):
    text = str(value or "").strip().lstrip("#")
    return int(text) if text.isdigit() else None

  def _answer_calendar(self, intent, user_text=""):
    service = self._get_calendar()
    if service is None:
      return CALENDAR_DISABLED
    if intent.name == "calendar_add":
      start_at = intent.entities.get("start_at", "")
      try:
        start_at = (
            datetime.fromisoformat(str(start_at)) if start_at else None
        )
      except (TypeError, ValueError):
        start_at = None
      return self._calendar_add_text(
          service,
          intent.entities.get("title", ""),
          start_at,
          int(intent.entities.get("duration_minutes") or 60),
          intent.entities.get("details", ""),
      )
    if intent.name == "calendar_agenda":
      return self._calendar_agenda_text(
          service, intent.entities.get("day", "")
      )
    identifier = intent.entities.get("id")
    if intent.name == "calendar_show":
      return self._calendar_show(service, identifier)
    if intent.name == "calendar_delete":
      return self._calendar_delete_pending(service, identifier, user_text)
    return CALENDAR_HELP

  def _calendar_add_text(
      self, service, title, start_at, duration_minutes=60, details=""
  ):
    if start_at is None:
      return "When is that event, sir?"
    try:
      event = service.add(
          title,
          start_at,
          duration_minutes=duration_minutes,
          details=details,
      )
    except CalendarError as exc:
      return f"I couldn't add that event, sir: {exc}."
    return f"Event #{event.id} added: {format_event(event)}."

  def _calendar_agenda_text(self, service, day=""):
    try:
      agenda = service.agenda(day or None)
    except CalendarError as exc:
      return f"I couldn't read the calendar, sir: {exc}."
    return format_agenda(agenda)

  def _calendar_list_text(self, service):
    try:
      events = service.upcoming(limit=20)
    except CalendarError as exc:
      return f"I couldn't read the calendar, sir: {exc}."
    if not events:
      return "You have no upcoming events, sir."
    return "Upcoming events:\n" + "\n".join(
        format_event(event) for event in events
    )

  def _calendar_show(self, service, identifier):
    identifier = self._calendar_id(identifier)
    if identifier is None:
      return "Usage: /calendar show ID"
    try:
      event = service.get(identifier)
    except CalendarError as exc:
      return f"I couldn't read that event, sir: {exc}."
    if event is None:
      return "There is no event with that id, sir."
    text = format_event(event)
    if event.details:
      text += f"\nDetails: {event.details}"
    return text

  def _calendar_edit(self, service, text):
    parts = str(text or "").split(maxsplit=2)
    if len(parts) < 3:
      return "Usage: /calendar edit ID title|start|duration|details VALUE"
    identifier = self._calendar_id(parts[0])
    field = parts[1].lower()
    value = parts[2].strip()
    if identifier is None:
      return "Usage: /calendar edit ID title|start|duration|details VALUE"
    fields = {}
    if field in {"name", "title"}:
      fields["title"] = value
    elif field in {"start", "time"}:
      fields["start_at"] = value
    elif field in {"duration", "length"}:
      try:
        fields["duration_minutes"] = int(value)
      except ValueError:
        return "Duration must be a number of minutes, sir."
    elif field in {"details", "detail"}:
      fields["details"] = value
    else:
      return "Usage: /calendar edit ID title|start|duration|details VALUE"
    try:
      event = service.update(identifier, **fields)
    except CalendarError as exc:
      return f"I couldn't update that event, sir: {exc}."
    return f"Event #{event.id} updated: {format_event(event)}."

  def _calendar_delete(self, service, identifier):
    identifier = self._calendar_id(identifier)
    if identifier is None:
      return "Usage: /calendar remove ID"
    try:
      event = service.delete(identifier)
    except CalendarError as exc:
      return f"I couldn't delete that event, sir: {exc}."
    return f"Event #{event.id} removed: {event.title}."

  def _calendar_delete_pending(self, service, identifier, user_text):
    try:
      event = service.get(identifier)
    except CalendarError as exc:
      return f"I couldn't read that event, sir: {exc}."
    if event is None:
      return "There is no event with that id, sir."
    arguments = {"id": event.id}
    tool = self.registry.get("calendar_delete") if self.registry else None
    description = (
        tool.describe(arguments)
        if tool is not None
        else f"Delete event #{event.id}"
    )
    self._pending_tool = ("calendar_delete", arguments, user_text)
    return f"{description}. Please confirm to continue."

  def _calendar_command(self, text):
    service = self._get_calendar()
    if service is None:
      return CALENDAR_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body or body.lower() in {"help", "?"}:
      return CALENDAR_HELP
    action, _separator, rest = body.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in {"today", "tomorrow", "yesterday"}:
      return self._calendar_agenda_text(service, action)
    if action in {"agenda", "day"}:
      return self._calendar_agenda_text(service, rest or "today")
    if action in {"list", "upcoming"}:
      return self._calendar_list_text(service)
    if action == "add":
      parsed = parse_event_input(rest)
      if not parsed.get("title"):
        return "What is the event, sir?"
      return self._calendar_add_text(
          service,
          parsed["title"],
          parsed.get("start_at"),
          parsed.get("duration_minutes", 60),
          parsed.get("details", ""),
      )
    if action in {"show", "detail", "details", "get"}:
      return self._calendar_show(service, rest)
    if action == "edit":
      return self._calendar_edit(service, rest)
    if action in {"remove", "delete", "rm"}:
      return self._calendar_delete(service, rest)
    return CALENDAR_HELP

  @staticmethod
  def _time_id(value):
    text = str(value or "").strip().lstrip("#")
    return int(text) if text.isdigit() else None

  def _answer_time_tracking(self, intent, user_text=""):
    service = self._get_time_tracking()
    if service is None:
      return TIME_DISABLED
    if intent.name == "time_start":
      return self._time_start_text(
          service, intent.entities.get("label", "")
      )
    if intent.name == "time_stop":
      return self._time_stop_text(service)
    if intent.name == "time_status":
      return self._time_status_text(service)
    if intent.name == "time_report":
      return self._time_report_text(
          service, intent.entities.get("day", "")
      )
    if intent.name == "time_week":
      return self._time_week_text(service, intent.entities.get("start", ""))
    if intent.name == "time_list":
      return self._time_list_text(service)
    if intent.name == "time_delete":
      return self._time_delete_pending(
          service, intent.entities.get("id"), user_text
      )
    return TIME_HELP

  def _time_start_text(self, service, label):
    try:
      entry = service.start(label)
    except TimeError as exc:
      return f"I couldn't start tracking, sir: {exc}."
    return (
        f"Started tracking {entry.label} at"
        f" {entry.started_at.strftime('%H:%M')}."
    )

  def _time_stop_text(self, service):
    try:
      entry = service.stop()
    except TimeError as exc:
      return f"I couldn't stop tracking, sir: {exc}."
    return f"Stopped tracking {entry.label}. {format_entry(entry)}."

  def _time_status_text(self, service):
    try:
      entry = service.active()
    except TimeError as exc:
      return f"I couldn't read the active entry, sir: {exc}."
    if entry is None:
      return "Nothing is being tracked, sir."
    return "Currently tracking: " + format_entry(entry) + "."

  def _time_report_text(self, service, day=""):
    try:
      report = service.report(day or None)
    except TimeError as exc:
      return f"I couldn't build that time report, sir: {exc}."
    return format_report(report)

  def _time_week_text(self, service, start=""):
    try:
      reports = service.week(start or None)
    except TimeError as exc:
      return f"I couldn't build that time report, sir: {exc}."
    return format_week(reports, reports[0].day)

  def _time_list_text(self, service):
    try:
      entries = service.list()
    except TimeError as exc:
      return f"I couldn't read time entries, sir: {exc}."
    if not entries:
      return "No time entries found, sir."
    return "Time entries:\n" + "\n".join(
        format_entry(entry) for entry in entries
    )

  def _time_delete(self, service, identifier):
    identifier = self._time_id(identifier)
    if identifier is None:
      return "Usage: /time remove ID"
    try:
      entry = service.delete(identifier)
    except TimeError as exc:
      return f"I couldn't delete that time entry, sir: {exc}."
    return f"Time entry #{entry.id} removed: {entry.label}."

  def _time_delete_pending(self, service, identifier, user_text):
    try:
      entry = service.get(identifier)
    except TimeError as exc:
      return f"I couldn't read that time entry, sir: {exc}."
    if entry is None:
      return "There is no time entry with that id, sir."
    arguments = {"id": entry.id}
    tool = self.registry.get("time_delete") if self.registry else None
    description = (
        tool.describe(arguments)
        if tool is not None
        else f"Delete time entry #{entry.id}"
    )
    self._pending_tool = ("time_delete", arguments, user_text)
    return f"{description}. Please confirm to continue."

  def _time_command(self, text):
    service = self._get_time_tracking()
    if service is None:
      return TIME_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body or body.lower() in {"help", "?"}:
      return TIME_HELP
    action, _separator, rest = body.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action == "start":
      if not rest:
        return "What should I track, sir?"
      return self._time_start_text(service, rest)
    if action in {"stop", "end", "finish"}:
      return self._time_stop_text(service)
    if action in {"status", "active"}:
      return self._time_status_text(service)
    if action == "today":
      return self._time_report_text(service, "today")
    if action == "yesterday":
      return self._time_report_text(service, "yesterday")
    if action == "report":
      return self._time_report_text(service, rest or "today")
    if action == "week":
      return self._time_week_text(service, rest)
    if action in {"list", "entries"}:
      return self._time_list_text(service)
    if action in {"remove", "delete", "rm"}:
      return self._time_delete(service, rest)
    return TIME_HELP

  @staticmethod
  def _goal_id(value):
    text = str(value or "").strip().lstrip("#")
    return int(text) if text.isdigit() else None

  def _answer_goals(self, intent, user_text=""):
    service = self._get_goals()
    if service is None:
      return GOALS_DISABLED
    if intent.name == "goal_add":
      return self._goal_create(
          service,
          intent.entities.get("title", ""),
          intent.entities.get("target_value"),
          intent.entities.get("unit", ""),
          intent.entities.get("target_date", ""),
      )
    if intent.name == "goal_list":
      return self._goal_list_text(service)
    identifier = intent.entities.get("id")
    if intent.name == "goal_show":
      return self._goal_show(service, identifier)
    if intent.name == "goal_progress":
      return self._goal_progress(
          service,
          identifier,
          intent.entities.get("value", 0),
          bool(intent.entities.get("add")),
      )
    if intent.name == "goal_complete":
      return self._goal_complete(service, identifier)
    if intent.name == "goal_reopen":
      return self._goal_reopen(service, identifier)
    if intent.name == "goal_delete":
      return self._goal_delete_pending(service, identifier, user_text)
    return GOALS_HELP

  def _goal_create(
      self, service, title, target_value=None, unit="", target_date=""
  ):
    try:
      goal = service.add(
          title,
          target_value=target_value,
          unit=unit,
          target_date=target_date,
      )
    except GoalError as exc:
      return f"I couldn't add that goal, sir: {exc}."
    message = f"Goal #{goal.id} added: {goal.title}."
    if goal.target_value is not None:
      message += (
          f" Target: {goal.target_value:g} {goal.unit}".rstrip() + "."
      )
    if goal.target_date is not None:
      message += (
          f" Target date: {goal.target_date.strftime('%A %d %B %Y')}."
      )
    return message

  def _goal_list_text(self, service, status="active"):
    if status not in {"active", "completed", "all"}:
      return "Usage: /goal list [all|completed]"
    wanted = None if status == "all" else status
    try:
      goals = service.list(status=wanted)
    except GoalError as exc:
      return f"I couldn't read your goals, sir: {exc}."
    if not goals:
      if status == "active":
        return "You have no active goals, sir."
      return "You have no goals yet, sir."
    heading = "Active goals:" if status == "active" else "Goals:"
    return heading + "\n" + "\n".join(format_goal(goal) for goal in goals)

  def _goal_show(self, service, identifier):
    identifier = self._goal_id(identifier)
    if identifier is None:
      return "Usage: /goal show ID"
    try:
      goal = service.get(identifier)
    except GoalError as exc:
      return f"I couldn't read that goal, sir: {exc}."
    if goal is None:
      return "There is no goal with that id, sir."
    return format_goal_detail(goal)

  def _goal_progress(self, service, identifier, value, add=False):
    identifier = self._goal_id(identifier)
    if identifier is None:
      return "Usage: /goal progress ID VALUE"
    try:
      goal = service.progress(identifier, value, add=add)
    except GoalError as exc:
      return f"I couldn't update that goal, sir: {exc}."
    return f"Goal #{goal.id} progress updated: {format_goal(goal)}."

  def _goal_complete(self, service, identifier):
    identifier = self._goal_id(identifier)
    if identifier is None:
      return "Usage: /goal complete ID"
    try:
      goal = service.complete(identifier)
    except GoalError as exc:
      return f"I couldn't complete that goal, sir: {exc}."
    return f"Goal #{goal.id} completed: {goal.title}."

  def _goal_reopen(self, service, identifier):
    identifier = self._goal_id(identifier)
    if identifier is None:
      return "Usage: /goal reopen ID"
    try:
      goal = service.reopen(identifier)
    except GoalError as exc:
      return f"I couldn't reopen that goal, sir: {exc}."
    return f"Goal #{goal.id} reopened: {goal.title}."

  def _goal_edit(self, service, text):
    parts = str(text or "").split(maxsplit=2)
    if len(parts) < 3:
      return "Usage: /goal edit ID name|target|unit|due VALUE"
    identifier = self._goal_id(parts[0])
    field = parts[1].lower()
    value = parts[2].strip()
    if identifier is None:
      return "Usage: /goal edit ID name|target|unit|due VALUE"
    fields = {}
    if field in {"name", "title"}:
      fields["title"] = value
    elif field in {"target", "value"}:
      fields["target_value"] = value
    elif field == "unit":
      fields["unit"] = value
    elif field in {"due", "date", "deadline"}:
      fields["target_date"] = value
    elif field == "clear" and value.lower() in {"due", "date", "deadline"}:
      fields["target_date"] = ""
    else:
      return "Usage: /goal edit ID name|target|unit|due VALUE"
    try:
      goal = service.update(identifier, **fields)
    except GoalError as exc:
      return f"I couldn't update that goal, sir: {exc}."
    message = f"Goal #{goal.id} updated: {format_goal(goal)}."
    if "target_date" in fields and goal.target_date is None:
      message += " No target date is set."
    return message

  def _goal_delete(self, service, identifier):
    identifier = self._goal_id(identifier)
    if identifier is None:
      return "Usage: /goal remove ID"
    try:
      goal = service.delete(identifier)
    except GoalError as exc:
      return f"I couldn't delete that goal, sir: {exc}."
    return f"Goal #{goal.id} removed: {goal.title}."

  def _goal_delete_pending(self, service, identifier, user_text):
    try:
      goal = service.get(identifier)
    except GoalError as exc:
      return f"I couldn't read that goal, sir: {exc}."
    if goal is None:
      return "There is no goal with that id, sir."
    arguments = {"id": goal.id}
    tool = self.registry.get("goal_delete") if self.registry else None
    description = (
        tool.describe(arguments)
        if tool is not None
        else f"Delete goal #{goal.id}"
    )
    self._pending_tool = ("goal_delete", arguments, user_text)
    return f"{description}. Please confirm to continue."

  def _goal_command(self, text):
    service = self._get_goals()
    if service is None:
      return GOALS_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body or body.lower() in {"help", "?"}:
      return GOALS_HELP
    action, _separator, rest = body.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in {"list", "ls"}:
      status = rest.lower() if rest.lower() in {"all", "completed"} else "active"
      return self._goal_list_text(service, status)
    if action == "add":
      parsed = parse_goal_input(rest)
      if not parsed.get("title"):
        return "What should the goal be called, sir?"
      return self._goal_create(
          service,
          parsed["title"],
          parsed.get("target_value"),
          parsed.get("unit", ""),
          parsed.get("target_date", ""),
      )
    if action in {"show", "detail", "details", "get"}:
      return self._goal_show(service, rest)
    if action in {"progress", "update"}:
      pieces = rest.split(maxsplit=1)
      if len(pieces) < 2:
        return "Usage: /goal progress ID VALUE"
      return self._goal_progress(service, pieces[0], pieces[1])
    if action in {"increment", "addprogress"}:
      pieces = rest.split(maxsplit=1)
      if len(pieces) < 2:
        return "Usage: /goal increment ID VALUE"
      return self._goal_progress(service, pieces[0], pieces[1], add=True)
    if action in {"complete", "done", "finish"}:
      return self._goal_complete(service, rest)
    if action in {"reopen", "restart"}:
      return self._goal_reopen(service, rest)
    if action == "edit":
      return self._goal_edit(service, rest)
    if action in {"remove", "delete", "rm"}:
      return self._goal_delete(service, rest)
    return GOALS_HELP

  @staticmethod
  def _habit_id(value):
    text = str(value or "").strip().lstrip("#")
    return int(text) if text.isdigit() else None

  def _answer_habits(self, intent, user_text=""):
    service = self._get_habits()
    if service is None:
      return HABITS_DISABLED
    if intent.name == "habit_add":
      return self._habit_create(
          service,
          intent.entities.get("title", ""),
          intent.entities.get("reminder_time", ""),
      )
    if intent.name == "habit_list":
      return self._habit_list_text(service)
    if intent.name == "habit_stats":
      return self._habit_stats_text(service)
    if intent.name == "habit_show":
      return self._habit_show(service, intent.entities.get("id"))
    if intent.name == "habit_done":
      return self._habit_done(service, intent.entities.get("id"))
    if intent.name == "habit_undo":
      return self._habit_undo(service, intent.entities.get("id"))
    if intent.name == "habit_delete":
      return self._habit_delete_pending(
          service, intent.entities.get("id"), user_text
      )
    return HABITS_HELP

  def _habit_create(self, service, title, reminder_time=""):
    try:
      summary = service.add(title, reminder_time=reminder_time)
    except HabitError as exc:
      return f"I couldn't add that habit, sir: {exc}."
    message = f"Habit #{summary.id} added: {summary.title}."
    if summary.habit.reminder_time:
      message += (
          f" I'll remind you every day at {summary.habit.reminder_time}."
      )
    return message

  def _habit_list_text(self, service):
    try:
      summaries = service.list()
    except HabitError as exc:
      return f"I couldn't read your habits, sir: {exc}."
    if not summaries:
      return "You have no habits, sir."
    return "Habits:\n" + "\n".join(
        format_habit(summary) for summary in summaries
    )

  def _habit_stats_text(self, service):
    try:
      summaries = service.list()
    except HabitError as exc:
      return f"I couldn't read your habits, sir: {exc}."
    if not summaries:
      return "You have no habits, sir."
    done = sum(1 for summary in summaries if summary.done_today)
    best = max(summaries, key=lambda item: item.streak)
    lines = [
        f"{len(summaries)} habits, {done} done today.",
        f"Longest current streak: {best.streak} days ({best.title}).",
    ]
    lines.extend(format_habit(summary) for summary in summaries)
    return "\n".join(lines)

  def _habit_show(self, service, identifier):
    identifier = self._habit_id(identifier)
    if identifier is None:
      return "Usage: /habit show ID"
    try:
      summary = service.summary(identifier)
    except HabitError as exc:
      return f"I couldn't read that habit, sir: {exc}."
    return format_habit_detail(summary)

  def _habit_done(self, service, identifier):
    identifier = self._habit_id(identifier)
    if identifier is None:
      return "Usage: /habit done ID"
    try:
      before = service.summary(identifier)
      summary = service.check_in(identifier)
    except HabitError as exc:
      return f"I couldn't check in that habit, sir: {exc}."
    if before.done_today:
      return (
          f"Habit #{summary.id} is already done today: {summary.title}."
          f" Current streak: {summary.streak} days."
      )
    return (
        f"Habit #{summary.id} checked in: {summary.title}."
        f" Current streak: {summary.streak} days."
    )

  def _habit_undo(self, service, identifier):
    identifier = self._habit_id(identifier)
    if identifier is None:
      return "Usage: /habit undo ID"
    try:
      before = service.summary(identifier)
      summary = service.undo(identifier)
    except HabitError as exc:
      return f"I couldn't undo that check-in, sir: {exc}."
    if not before.done_today:
      return f"Habit #{summary.id} was not checked in today: {summary.title}."
    return (
        f"Habit #{summary.id} check-in undone: {summary.title}."
        f" Current streak: {summary.streak} days."
    )

  def _habit_edit(self, service, text):
    parts = str(text or "").split(maxsplit=2)
    if len(parts) < 3:
      return "Usage: /habit edit ID name|remind VALUE"
    identifier = self._habit_id(parts[0])
    field = parts[1].lower()
    value = parts[2].strip()
    if identifier is None:
      return "Usage: /habit edit ID name|remind VALUE"
    fields = {}
    if field in {"name", "title"}:
      fields["title"] = value
    elif field in {"remind", "reminder", "time"}:
      fields["reminder_time"] = value
    elif field == "clear" and value.lower() in {"remind", "reminder"}:
      fields["reminder_time"] = ""
    else:
      return "Usage: /habit edit ID name|remind VALUE"
    try:
      summary = service.update(identifier, **fields)
    except HabitError as exc:
      return f"I couldn't update that habit, sir: {exc}."
    message = f"Habit #{summary.id} updated: {summary.title}."
    if summary.habit.reminder_time:
      message += f" Reminder daily at {summary.habit.reminder_time}."
    else:
      message += " No reminder is set."
    return message

  def _habit_delete(self, service, identifier):
    identifier = self._habit_id(identifier)
    if identifier is None:
      return "Usage: /habit remove ID"
    try:
      habit = service.delete(identifier)
    except HabitError as exc:
      return f"I couldn't delete that habit, sir: {exc}."
    return f"Habit #{habit.id} removed: {habit.title}."

  def _habit_delete_pending(self, service, identifier, user_text):
    try:
      habit = service.get(identifier)
    except HabitError as exc:
      return f"I couldn't read that habit, sir: {exc}."
    if habit is None:
      return "There is no habit with that id, sir."
    arguments = {"id": habit.id}
    tool = self.registry.get("habit_delete") if self.registry else None
    description = (
        tool.describe(arguments)
        if tool is not None
        else f"Delete habit #{habit.id}"
    )
    self._pending_tool = ("habit_delete", arguments, user_text)
    return f"{description}. Please confirm to continue."

  def _habit_command(self, text):
    service = self._get_habits()
    if service is None:
      return HABITS_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body or body.lower() in {"help", "?"}:
      return HABITS_HELP
    action, _separator, rest = body.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in {"list", "ls", "all"}:
      return self._habit_list_text(service)
    if action in {"stats", "streaks"}:
      return self._habit_stats_text(service)
    if action == "add":
      parsed = parse_habit_input(rest)
      if not parsed.get("title"):
        return "What should the habit be called, sir?"
      return self._habit_create(
          service, parsed["title"], parsed.get("reminder_time", "")
      )
    if action in {"show", "detail", "details", "get"}:
      return self._habit_show(service, rest)
    if action in {"done", "complete", "check"}:
      return self._habit_done(service, rest)
    if action in {"undo", "uncheck", "reset"}:
      return self._habit_undo(service, rest)
    if action == "edit":
      return self._habit_edit(service, rest)
    if action in {"remove", "delete", "rm"}:
      return self._habit_delete(service, rest)
    return HABITS_HELP

  @staticmethod
  def _note_id(value):
    text = str(value or "").strip().lstrip("#")
    return int(text) if text.isdigit() else None

  def _answer_notes(self, intent, user_text=""):
    service = self._get_notes()
    if service is None:
      return NOTES_DISABLED
    name = intent.name
    if name == "note_add":
      return self._note_create(service, intent.entities.get("content", ""))
    if name == "note_list":
      return self._note_list_text(service)
    if name == "note_show":
      return self._note_show(service, intent.entities.get("id"))
    if name == "note_search":
      return self._note_search_text(
          service, intent.entities.get("query", "")
      )
    if name == "note_delete":
      return self._note_delete_pending(
          service, intent.entities.get("id"), user_text
      )
    return NOTES_HELP

  def _note_create(self, service, content, title=""):
    try:
      note = service.add(content, title=title)
    except NoteError as exc:
      return f"I couldn't save that note, sir: {exc}."
    return f"Note #{note.id} saved: {note.title}."

  def _note_list_text(self, service):
    try:
      notes = service.list()
    except NoteError as exc:
      return f"I couldn't read your notes, sir: {exc}."
    if not notes:
      return "You have no notes, sir."
    return "Notes:\n" + "\n".join(format_note(note) for note in notes)

  def _note_show(self, service, identifier):
    identifier = self._note_id(identifier)
    if identifier is None:
      return "Usage: /notes show ID"
    try:
      note = service.get(identifier)
    except NoteError as exc:
      return f"I couldn't read that note, sir: {exc}."
    if note is None:
      return "There is no note with that id, sir."
    return format_note(note)

  def _note_search_text(self, service, query):
    try:
      hits = service.search(query)
    except NoteError as exc:
      return f"I couldn't search your notes, sir: {exc}."
    if not hits:
      return f"No notes matched {query!r}, sir."
    return f"Notes matching {query!r}:\n" + "\n".join(
        format_note(hit.note, hit.snippet) for hit in hits
    )

  def _note_edit(self, service, text):
    parts = str(text or "").split(maxsplit=2)
    if len(parts) < 3:
      return "Usage: /notes edit ID content|title VALUE"
    identifier = self._note_id(parts[0])
    field = parts[1].lower()
    value = parts[2]
    if identifier is None or field not in {"content", "title"}:
      return "Usage: /notes edit ID content|title VALUE"
    try:
      note = service.update(identifier, **{field: value})
    except NoteError as exc:
      return f"I couldn't update that note, sir: {exc}."
    return f"Note #{note.id} updated: {note.title}."

  def _note_delete(self, service, identifier):
    identifier = self._note_id(identifier)
    if identifier is None:
      return "Usage: /notes remove ID"
    try:
      note = service.delete(identifier)
    except NoteError as exc:
      return f"I couldn't delete that note, sir: {exc}."
    return f"Note #{note.id} removed: {note.title}."

  def _note_delete_pending(self, service, identifier, user_text):
    try:
      note = service.get(identifier)
    except NoteError as exc:
      return f"I couldn't read that note, sir: {exc}."
    if note is None:
      return "There is no note with that id, sir."
    arguments = {"id": note.id}
    tool = self.registry.get("note_delete") if self.registry else None
    description = (
        tool.describe(arguments)
        if tool is not None
        else f"Delete note #{note.id}"
    )
    self._pending_tool = ("note_delete", arguments, user_text)
    return f"{description}. Please confirm to continue."

  def _notes_command(self, text):
    service = self._get_notes()
    if service is None:
      return NOTES_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body or body.lower() in {"help", "?"}:
      return NOTES_HELP
    action, _separator, rest = body.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in {"list", "ls", "all"}:
      return self._note_list_text(service)
    if action == "add":
      if " | " in rest:
        title, content = rest.split(" | ", 1)
        return self._note_create(service, content, title)
      return self._note_create(service, rest)
    if action in {"show", "get", "open", "read"}:
      return self._note_show(service, rest)
    if action in {"search", "find"}:
      return self._note_search_text(service, rest)
    if action == "edit":
      return self._note_edit(service, rest)
    if action in {"remove", "delete", "rm"}:
      return self._note_delete(service, rest)
    return NOTES_HELP

  def _plan_reply(self, service, kind, when):
    try:
      if kind == "week":
        plan = service.weekly_plan(when or None)
      else:
        plan = service.daily_plan(when or None)
    except ProductivityError as exc:
      return f"I couldn't build that plan, sir: {exc}."
    return format_plan(plan)

  def _answer_plan(self, intent):
    service = self._get_productivity()
    if service is None:
      return TODO_DISABLED
    kind = "week" if intent.name == "plan_week" else "day"
    return self._plan_reply(
        service, kind, intent.entities.get("when") or ""
    )

  def _plan_command(self, text):
    service = self._get_productivity()
    if service is None:
      return TODO_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body:
      return self._plan_reply(service, "day", "")
    lowered = body.lower()
    if lowered in {"day", "daily", "today"}:
      return self._plan_reply(service, "day", "")
    if lowered in {"week", "weekly", "this week"}:
      return self._plan_reply(service, "week", "")
    if lowered == "next week":
      return self._plan_reply(service, "week", "next week")
    action, _separator, rest = body.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in {"day", "daily"}:
      return self._plan_reply(service, "day", rest)
    if action in {"week", "weekly"}:
      return self._plan_reply(service, "week", rest)
    return self._plan_reply(service, "day", body)

  @staticmethod
  def _todo_id(value):
    text = str(value or "").strip().lstrip("#")
    return int(text) if text.isdigit() else None

  def _todo_command(self, text):
    service = self._get_productivity()
    if service is None:
      return TODO_DISABLED
    parts = text.split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body or body.lower() in {"help", "?"}:
      return TODO_HELP
    action, _separator, rest = body.partition(" ")
    action = action.lower()
    rest = rest.strip()
    if action in {"list", "ls", "open"}:
      return self._todo_list_text(service, status="open")
    if action in {"all", "history"}:
      return self._todo_list_text(service, status=None)
    if action == "add":
      return self._todo_add_text(service, rest)
    if action in {"show", "detail", "details", "get"}:
      return self._todo_show(service, rest)
    if action in {"done", "complete", "finish"}:
      return self._todo_done(service, rest)
    if action in {"remove", "delete", "rm"}:
      return self._todo_delete(service, rest)
    if action == "snooze":
      return self._todo_snooze(service, rest)
    if action == "edit":
      return self._todo_edit(service, rest)
    return TODO_HELP

  def _todo_add_text(self, service, text):
    parsed = parse_todo_input(text)
    if not parsed.get("title"):
      return "What should the to-do say, sir?"
    return self._todo_create(service, parsed)

  def _todo_create(self, service, parsed):
    try:
      item = service.add(
          parsed.get("title", ""),
          priority=parsed.get("priority"),
          due_at=parsed.get("due_at"),
          remind_at=parsed.get("remind_at"),
          recurrence=parsed.get("recurrence", "none"),
      )
    except ProductivityError as exc:
      return f"I couldn't add that to-do, sir: {exc}."
    return self._todo_added_text(item)

  def _todo_added_text(self, item):
    message = f"To-do #{item.id} added: {item.title}."
    if item.due_at is not None:
      message += f" Due {item.due_at.strftime('%A %d %B at %H:%M')}."
    if item.remind_at is not None and item.remind_at != item.due_at:
      message += (
          f" I'll remind you at"
          f" {item.remind_at.strftime('%H:%M')}."
      )
    if item.priority != "normal":
      message += f" Priority: {item.priority}."
    if item.recurrence != "none":
      message += f" Repeats {item.recurrence}."
    return message

  def _todo_list_text(self, service, status="open"):
    try:
      rows = service.list(status=status, limit=20)
    except ProductivityError as exc:
      return f"I couldn't read the to-do list, sir: {exc}."
    if not rows:
      if status == "open":
        return "You have no open to-dos, sir."
      return "You have no to-dos yet, sir."
    heading = "Open to-dos:" if status == "open" else "To-dos:"
    return heading + "\n" + "\n".join(format_todo(item) for item in rows)

  def _todo_show(self, service, text):
    identifier = self._todo_id(text)
    if identifier is None:
      return "Usage: /todo show ID"
    try:
      item = service.get(identifier)
    except ProductivityError as exc:
      return f"I couldn't read that to-do, sir: {exc}."
    if item is None:
      return "There is no to-do with that id, sir."
    lines = [
        f"To-do #{item.id}: {item.title}",
        f"Status: {item.status}",
        f"Priority: {item.priority}",
    ]
    if item.details:
      lines.append(f"Details: {item.details}")
    if item.due_at is not None:
      lines.append(f"Due: {item.due_at.strftime('%A %d %B %Y at %H:%M')}")
    if item.remind_at is not None:
      lines.append(
          f"Reminder: {item.remind_at.strftime('%A %d %B at %H:%M')}"
      )
    if item.recurrence != "none":
      lines.append(f"Repeats: {item.recurrence}")
    if item.parent_id:
      lines.append(f"Previous occurrence: #{item.parent_id}")
    return "\n".join(lines)

  def _todo_done(self, service, text):
    identifier = self._todo_id(text)
    if identifier is None:
      return "Usage: /todo done ID"
    try:
      done, next_item = service.complete(identifier)
    except ProductivityError as exc:
      return f"I couldn't complete that to-do, sir: {exc}."
    message = f"To-do #{done.id} completed: {done.title}."
    if next_item is not None:
      message += (
          f" Next occurrence: #{next_item.id} due"
          f" {next_item.due_at.strftime('%A %d %B at %H:%M')}."
      )
    return message

  def _todo_delete(self, service, text):
    identifier = self._todo_id(text)
    if identifier is None:
      return "Usage: /todo remove ID"
    try:
      item = service.delete(identifier)
    except ProductivityError as exc:
      return f"I couldn't remove that to-do, sir: {exc}."
    return f"To-do #{item.id} removed: {item.title}."

  def _todo_snooze(self, service, text):
    parts = text.split(maxsplit=1)
    identifier = self._todo_id(parts[0] if parts else "")
    if identifier is None:
      return "Usage: /todo snooze ID [MINUTES]"
    duration = parts[1].strip() if len(parts) > 1 else "10 minutes"
    seconds = parse_duration(duration)
    if not seconds:
      return "Usage: /todo snooze ID [MINUTES]"
    try:
      item = service.snooze(identifier, max(1, seconds // 60))
    except ProductivityError as exc:
      return f"I couldn't snooze that to-do, sir: {exc}."
    return (
        f"To-do #{item.id} snoozed until"
        f" {item.remind_at.strftime('%A %d %B at %H:%M')}."
    )

  def _todo_edit(self, service, text):
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
      return "Usage: /todo edit ID title|due|remind|priority|repeat VALUE"
    identifier = self._todo_id(parts[0])
    field = parts[1].lower()
    value = parts[2].strip()
    if identifier is None:
      return "Usage: /todo edit ID title|due|remind|priority|repeat VALUE"
    try:
      item = service.get(identifier)
    except ProductivityError as exc:
      return f"I couldn't read that to-do, sir: {exc}."
    if item is None:
      return "There is no to-do with that id, sir."
    fields = {}
    if field == "title":
      fields["title"] = value
    elif field in {"due", "deadline"}:
      when = parse_when(value, prefer_future=False)
      if when is None:
        return "I couldn't understand that due date, sir."
      fields["due_at"] = when
      if item.remind_at is None or item.remind_at == item.due_at:
        fields["remind_at"] = when
    elif field in {"remind", "reminder"}:
      when = parse_when(value, prefer_future=True)
      if when is None:
        return "I couldn't understand that reminder time, sir."
      fields["remind_at"] = when
    elif field == "priority":
      fields["priority"] = value
    elif field in {"repeat", "recurrence"}:
      fields["recurrence"] = value
    elif field == "clear" and value.lower() in {"due", "deadline"}:
      fields["due_at"] = None
      fields["remind_at"] = None
    elif field == "clear" and value.lower() in {"remind", "reminder"}:
      fields["remind_at"] = None
    else:
      return "Usage: /todo edit ID title|due|remind|priority|repeat VALUE"
    try:
      item = service.update(identifier, **fields)
    except ProductivityError as exc:
      return f"I couldn't update that to-do, sir: {exc}."
    return self._todo_show(service, str(item.id))

  @staticmethod
  def _todo_intent_datetime(value):
    if not value:
      return None
    try:
      return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
      return None

  def _answer_todo(self, intent, user_text=""):
    service = self._get_productivity()
    if service is None:
      return TODO_DISABLED
    if intent.name == "todo_add":
      parsed = {
          "title": intent.entities.get("title", ""),
          "priority": intent.entities.get("priority"),
          "due_at": self._todo_intent_datetime(
              intent.entities.get("due_at")
          ),
          "remind_at": self._todo_intent_datetime(
              intent.entities.get("remind_at")
          ),
          "recurrence": intent.entities.get("recurrence", "none"),
      }
      return self._todo_create(service, parsed)
    if intent.name == "todo_list":
      return self._todo_list_text(service, status="open")
    identifier = int(intent.entities.get("id") or 0)
    if intent.name == "todo_show":
      return self._todo_show(service, str(identifier))
    if intent.name == "todo_done":
      return self._todo_done(service, str(identifier))
    if intent.name == "todo_delete":
      return self._todo_delete_pending(service, identifier, user_text)
    if intent.name == "todo_snooze":
      minutes = int(intent.entities.get("minutes") or 10)
      try:
        item = service.snooze(identifier, minutes)
      except ProductivityError as exc:
        return f"I couldn't snooze that to-do, sir: {exc}."
      return (
          f"To-do #{item.id} snoozed until"
          f" {item.remind_at.strftime('%A %d %B at %H:%M')}."
      )
    return None

  def _todo_delete_pending(self, service, identifier, user_text):
    """Ask for confirmation before deleting a to-do through the tool layer."""
    try:
      item = service.get(identifier)
    except ProductivityError as exc:
      return f"I couldn't read that to-do, sir: {exc}."
    if item is None:
      return "There is no to-do with that id, sir."
    arguments = {"id": identifier}
    tool = self.registry.get("todo_delete") if self.registry else None
    description = (
        tool.describe(arguments)
        if tool is not None
        else f"Delete to-do #{identifier}"
    )
    self._pending_tool = ("todo_delete", arguments, user_text)
    return f"{description}. Please confirm to continue."

  # -- document intelligence --------------------------------------------

  def _get_documents(self):
    if not self._settings.documents.enabled:
      return None
    if self._documents is None:
      try:
        self._documents = DocumentService(
            self._settings.documents, self._settings.tools
        )
      except DocumentError as exc:
        log.warning("Document intelligence unavailable: %s", exc)
    return self._documents

  def _document_target(self, intent):
    return str(
        intent.entities.get("path") or self._last_document or ""
    ).strip()

  def _remember_document(self, service, document):
    self._last_document = service.relative(document.path)

  def _document_sources(self, service, hits):
    lines = []
    for index, hit in enumerate(hits, 1):
      lines.append(
          f"[{index}] {hit['title']} - {service.relative(hit['path'])}"
          f" (passage {hit['ordinal']})"
      )
    return "Sources:\n" + "\n".join(lines) if lines else "Sources: none"

  def _document_read(self, service, intent):
    path = self._document_target(intent)
    if not path:
      return "Which document should I read, sir?"
    try:
      document = service.read(path)
    except DocumentError as exc:
      return f"I couldn't read that document, sir: {exc}."
    self._remember_document(service, document)
    text = document.text.strip()
    limit = 5000
    notes = []
    metadata = document.metadata
    confidence = metadata.get("ocr_confidence")
    if confidence is not None:
      level = metadata.get("ocr_level", "unknown")
      notes.append(
          f"OCR confidence: {round(float(confidence) * 100)}%"
          f" ({level}; local estimate)."
      )
    partial_pages = metadata.get("ocr_partial_pages") or []
    if partial_pages:
      pages = ", ".join(str(page) for page in partial_pages[:8])
      notes.append(
          f"Warning: page(s) {pages} may be only partially recognized;"
          " verify the scan."
      )
    elif metadata.get("ocr_partial"):
      notes.append(
          "Warning: the image may be only partially recognized;"
          " verify the scan."
      )
    if metadata.get("ocr_truncated"):
      notes.append("OCR was limited by the ocr_max_pages setting.")
    for warning in metadata.get("ocr_warnings", [])[:3]:
      notes.append(f"OCR warning: {warning}.")
    suffix = "".join(f"\n\n[{note}]" for note in notes)
    if len(text) > limit:
      suffix += (
          f"\n\n[Showing {limit} of {len(text)} characters. Use"
          " /documents summarize for a shorter version.]"
      )
      text = text[:limit]
    return f"{document.title} ({document.format}):\n{text}{suffix}"

  def _document_index(self, service, intent):
    path = str(intent.entities.get("path") or "Documents").strip()
    try:
      target = service.resolve(path)
      if target.is_dir():
        result = service.index_folder(str(target))
        message = (
            f"Indexed {result['indexed']} document(s) from"
            f" {service.relative(target)}; skipped {result['skipped']}"
            f" unchanged and failed {result['failed']}."
        )
        if result["errors"]:
          message += " First problem: " + result["errors"][0]
        if result.get("ocr_warnings"):
          message += " OCR warning: " + result["ocr_warnings"][0]
        return message
      result = service.index(path)
    except DocumentError as exc:
      return f"I couldn't index that, sir: {exc}."
    message = (
        f"Indexed {result['title']} into {result['chunks']} searchable"
        " passage(s)."
    )
    if result.get("ocr_partial_pages") or result.get("ocr_partial"):
      message += " OCR warning: recognition may be incomplete."
    elif result.get("ocr_confidence") is not None:
      message += (
          f" OCR confidence: {round(float(result['ocr_confidence']) * 100)}%."
      )
    return message

  def _document_search(self, service, intent):
    query = str(intent.entities.get("query") or "").strip()
    if not query:
      return "What should I search the documents for, sir?"
    try:
      hits = service.search(query)
    except DocumentError as exc:
      return f"I couldn't search the documents, sir: {exc}."
    if not hits:
      return DOCUMENT_EMPTY
    self._last_document = service.relative(hits[0]["path"])
    lines = []
    for index, hit in enumerate(hits[:3], 1):
      text = normalize_text(hit["text"])[:260]
      lines.append(
          f"{index}. {hit['title']} (passage {hit['ordinal']}): {text}"
      )
    return (
        f"I found {len(hits)} relevant passage(s):\n"
        + "\n".join(lines)
    )

  def _document_ask(self, service, intent):
    query = str(intent.entities.get("query") or "").strip()
    if not query:
      return "What should I ask the documents, sir?"
    try:
      context, hits = service.context(query)
    except DocumentError as exc:
      return f"I couldn't search the documents, sir: {exc}."
    if not context:
      return DOCUMENT_EMPTY
    self._last_document = service.relative(hits[0]["path"])
    answer, ok = self._complete(
        [
            {"role": "system", "content": DOCUMENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Question: {query}\n\nPassages:\n{context}",
            },
        ],
        self.conversation.max_tokens(),
    )
    if not ok:
      return answer
    answer = _strip_preamble(answer)
    if NOT_FOUND_MARKER in answer.upper():
      return DOCUMENT_EMPTY
    if not _has_citation(answer, len(hits)) or not _supported(
        answer, context
    ):
      excerpt = normalize_text(hits[0]["text"])[:500]
      log.info("Document answer had no citation; returning evidence")
      return (
          "I found this passage but could not form a grounded answer, sir:\n"
          f"{excerpt}\n\n"
          + self._document_sources(service, hits)
      )
    return answer + "\n\n" + self._document_sources(service, hits)

  def _document_summarize(self, service, intent):
    path = self._document_target(intent)
    if not path:
      return "Which document should I summarize, sir?"
    try:
      document = service.read(path)
    except DocumentError as exc:
      return f"I couldn't summarize that document, sir: {exc}."
    self._remember_document(service, document)
    return summarize_document(document)

  def _document_compare(self, service, intent):
    first = str(intent.entities.get("first") or "").strip()
    second = str(intent.entities.get("second") or "").strip()
    if not first or not second:
      return "Which two documents should I compare, sir?"
    try:
      result = service.compare(first, second)
    except DocumentError as exc:
      return f"I couldn't compare those documents, sir: {exc}."
    first_doc = result["first"]
    second_doc = result["second"]
    lines = [
        f"Compared {first_doc.title} and {second_doc.title}:"
        f" {result['similarity']}% line similarity.",
        f"{first_doc.title}: {first_doc.chars} characters,"
        f" {len(first_doc.tables)} table(s).",
        f"{second_doc.title}: {second_doc.chars} characters,"
        f" {len(second_doc.tables)} table(s).",
    ]
    if result["removed_count"]:
      lines.append(
          f"Only in the first ({result['removed_count']} line(s)): "
          + " | ".join(result["removed"][:4])
      )
    if result["added_count"]:
      lines.append(
          f"Only in the second ({result['added_count']} line(s)): "
          + " | ".join(result["added"][:4])
      )
    if result["only_first"]:
      lines.append("Key terms only in the first: " + ", ".join(result["only_first"]))
    if result["only_second"]:
      lines.append(
          "Key terms only in the second: " + ", ".join(result["only_second"])
      )
    self._last_document = service.relative(second_doc.path)
    return "\n".join(lines)

  def _document_dates(self, service, intent):
    path = self._document_target(intent)
    if not path:
      return "Which document should I inspect for dates, sir?"
    try:
      items = service.dates(path)
    except DocumentError as exc:
      return f"I couldn't inspect that document, sir: {exc}."
    if not items:
      return "I found no explicit dates in that document, sir."
    lines = []
    for item in items[:12]:
      date = f" ({item['date']})" if item["date"] else ""
      lines.append(f"- {item['text']}{date}: {item['context']}")
    return "Dates I found:\n" + "\n".join(lines)

  def _document_actions(self, service, intent):
    path = self._document_target(intent)
    if not path:
      return "Which document should I inspect for action items, sir?"
    try:
      items = service.actions(path)
    except DocumentError as exc:
      return f"I couldn't inspect that document, sir: {exc}."
    if not items:
      return "I found no explicit action items in that document, sir."
    return "Action items:\n" + "\n".join(f"- {item}" for item in items)

  def _document_tables(self, service, intent):
    path = self._document_target(intent)
    if not path:
      return "Which document should I inspect for tables, sir?"
    try:
      tables = service.tables(path)
    except DocumentError as exc:
      return f"I couldn't inspect that document, sir: {exc}."
    if not tables:
      return "I found no tables in that document, sir."
    blocks = []
    for table in tables[:3]:
      rows = table.markdown().splitlines()[:18]
      blocks.append(f"{table.name}:\n" + "\n".join(rows))
    return "\n\n".join(blocks)

  def _document_explain(self, service, intent):
    path = self._document_target(intent)
    if not path:
      return "Which document should I explain, sir?"
    try:
      document = service.read(path)
    except DocumentError as exc:
      return f"I couldn't explain that document, sir: {exc}."
    self._remember_document(service, document)
    excerpt = document.text[:6000]
    answer, ok = self._complete(
        [
            {"role": "system", "content": DOCUMENT_EXPLAIN_PROMPT},
            {
                "role": "user",
                "content": f"Document: {document.title}\n\n{excerpt}",
            },
        ],
        min(600, self.conversation.max_tokens()),
    )
    if ok:
      return _strip_preamble(answer)
    return (
        "I couldn't reach my language model, sir. A local summary follows:\n"
        + summarize_document(document)
    )

  def _document_generate(self, service, intent, user_text):
    format = str(intent.entities.get("format") or "").lower()
    path = str(intent.entities.get("path") or "").strip()
    prompt = str(intent.entities.get("prompt") or "").strip()
    if not format:
      format = Path(path).suffix.lstrip(".").lower()
    formats = {
        "pdf": ".pdf",
        "docx": ".docx",
        "txt": ".txt",
        "md": ".md",
        "markdown": ".md",
        "csv": ".csv",
        "xlsx": ".xlsx",
        "xlsm": ".xlsx",
    }
    extension = formats.get(format)
    if extension is None:
      return "I can generate PDF, DOCX, TXT, Markdown, CSV or XLSX, sir."
    if not path:
      stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
      path = f"generated_{stamp}{extension}"
    elif Path(path).suffix.lower() not in {
        ".pdf",
        ".docx",
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".xlsx",
        ".xlsm",
    }:
      path += extension
    if not prompt:
      return "What should the document contain, sir?"
    content, ok = self._complete(
        [
            {"role": "system", "content": DOCUMENT_WRITE_PROMPT},
            {"role": "user", "content": prompt},
        ],
        min(900, self.conversation.max_tokens() * 2),
    )
    if not ok:
      return content
    content = _strip_preamble(content)
    if not content.strip():
      return "The model returned an empty document, sir."
    title = Path(path).stem.replace("_", " ").replace("-", " ").title()
    return self._document_pending(
        "document_write",
        {"path": path, "content": content, "title": title},
        user_text,
    )

  def _document_report(self, service, intent, user_text):
    paths = str(intent.entities.get("paths") or "").strip()
    if not paths:
      return "Which documents should the report cover, sir?"
    output = str(intent.entities.get("output") or "document_report.md")
    return self._document_pending(
        "document_report",
        {"paths": paths, "output": output},
        user_text,
    )

  def _document_pending(self, name, arguments, user_text):
    if self.registry is None:
      return "Document writing needs computer tools enabled, sir."
    tool = self.registry.get(name)
    if tool is None:
      return "Document writing is unavailable, sir."
    if tool.permission == "confirm":
      self._pending_tool = (name, arguments, user_text)
      self._pending_plan = None
      self._pending_task = None
      return f"{tool.describe(arguments)}. Please confirm to continue."
    result = self.registry.execute(name, arguments)
    return result.text

  def _answer_document(self, intent, user_text):
    service = self._get_documents()
    if service is None:
      return DOCUMENT_DISABLED
    if intent.name == "document_read":
      return self._document_read(service, intent)
    if intent.name == "document_index":
      return self._document_index(service, intent)
    if intent.name == "document_search":
      return self._document_search(service, intent)
    if intent.name == "document_ask":
      return self._document_ask(service, intent)
    if intent.name == "document_summarize":
      return self._document_summarize(service, intent)
    if intent.name == "document_compare":
      return self._document_compare(service, intent)
    if intent.name == "document_dates":
      return self._document_dates(service, intent)
    if intent.name == "document_actions":
      return self._document_actions(service, intent)
    if intent.name == "document_tables":
      return self._document_tables(service, intent)
    if intent.name == "document_explain":
      return self._document_explain(service, intent)
    if intent.name == "document_generate":
      return self._document_generate(service, intent, user_text)
    if intent.name == "document_report":
      return self._document_report(service, intent, user_text)
    return None

  def _answer_date(self, intent):
    iso = intent.entities.get("date")
    try:
      target = date.fromisoformat(iso) if iso else date.today()
    except (TypeError, ValueError):
      target = date.today()
    phrase = str(intent.entities.get("phrase", "")).lower()
    if target == date.today():
      label = "Today"
    elif phrase in {
        "tomorrow",
        "yesterday",
        "the day after tomorrow",
        "the day before yesterday",
    }:
      label = phrase.capitalize()
    else:
      label = "That date"
    return f"{label} is {target.strftime('%A, %d %B %Y')}."

  # -- memory -----------------------------------------------------------

  def _memory_context(self):
    """Reconstruct prompt context from memory plus the current session."""
    if self._memory is None:
      return ""
    query = " ".join(
        text for text, _answer in self._session.recent(4)
    )
    block = self._memory.context_block(
        query=query,
        limit=self._settings.memory.inject_items,
        episode_limit=2,
        max_chars=700,
    )
    if not self._config.enabled and len(self._session) >= 1:
      summary = self._session.summary(240)
      if summary:
        block = (block + "\nSession so far: " + summary).strip()
    return block

  def _note_usage(self, name, entities):
    """Count apps, folders and websites the user actually works with."""
    if self._memory is None:
      return
    if name == "open_app":
      self._memory.note_usage("app", entities.get("application"))
    elif name == "open_url":
      self._memory.note_usage("website", entities.get("url"))
    elif name in {"open_path", "list_files"}:
      self._memory.note_usage("folder", entities.get("path"))

  def _save_memory(self, category, content, temporary=False):
    key = subject_for(content) if not temporary else None
    replaced = self._memory.find_by_subject(key) if key else None
    try:
      _memory_id, created = self._memory.add(
          category, content, temporary=temporary, subject=key
      )
    except MemoryStoreError as exc:
      log.warning("Could not save memory: %s", exc)
      return "I couldn't save that to memory, sir."
    if not created:
      return "I already remember that, sir."
    if temporary:
      return f"Saved to temporary memory: {content}."
    if replaced:
      return (
          f"Updated what I remembered about {key}: {content}."
      )
    return f"Saved to memory: {content}."

  def _remember_memory(self, intent):
    if self._memory is None:
      return MEMORY_DISABLED
    content = intent.entities.get("content", "")
    if not content:
      return "What should I remember, sir?"
    return self._save_memory(
        intent.entities.get("category") or classify_memory(content),
        content,
        temporary=bool(intent.entities.get("temporary")),
    )

  def _format_memories(self, title, rows):
    if not rows:
      return "I don't have anything in memory about that, sir."
    lines = []
    for item in rows[:8]:
      text = item["content"]
      if len(text) > 80:
        text = text[:77].rstrip() + "..."
      tags = []
      if item["temporary"]:
        tags.append("temporary")
      if item.get("source") == "learned":
        tags.append("learned")
      if item.get("importance", 1) >= 4:
        tags.append(f"i{item['importance']}")
      label = f" [{' '.join(tags)}]" if tags else ""
      lines.append(
          f"[{item['id']}] ({item['category']}) {text}{label}"
      )
    return title + "\n" + "\n".join(lines)

  def _explain_memory(self, memory_id):
    item = self._memory.get(memory_id)
    if item is None:
      return "There is no memory with that id, sir."
    lines = [
        f"[{item['id']}] {item['content']}",
        f"Category: {item['category']}",
        f"Source: {item.get('source') or 'explicit'}",
        f"Importance: {item.get('importance', 1)}/5,"
        f" confidence: {float(item.get('confidence') or 1.0):.2f}",
        f"Created: {item['created_at'][:10]},"
        f" updated: {item['updated_at'][:10]}",
    ]
    if item.get("subject"):
      lines.append(f"Subject: {item['subject']}")
    if item.get("expires_at"):
      lines.append(f"Expires: {item['expires_at'][:10]}")
    if item.get("superseded_by"):
      lines.append(f"Replaced by memory {item['superseded_by']}")
    return "\n".join(lines)

  def _memory_reply(self, intent):
    if self._memory is None:
      return MEMORY_DISABLED
    if intent.name == "list_memory":
      return self._format_memories(
          "Saved memories:", self._memory.list()
      )
    if intent.name == "learned_memory":
      return self._format_memories(
          "Learned memories:", self._memory.list(source="learned")
      )
    if intent.name == "memory_episodes":
      rows = self._memory.recent_episodes(limit=5)
      if not rows:
        return "I don't have any recent episodes, sir."
      return "Recent events:\n" + "\n".join(
          f"[{row['id']}] {row['created_at'][:10]}: {row['summary']}"
          for row in rows
      )
    query = intent.entities.get("query", "")
    if intent.name == "search_memory":
      return self._format_memories(
          "Matching memories:", self._memory.search(query)
      )
    rows = self._memory.search(query)
    if not rows:
      return "I don't have that in memory, sir."
    if len(rows) > 1:
      return (
          self._format_memories("I remember several things:", rows)
          + "\nSay /memory delete ID to remove one."
      )
    item = rows[0]
    self._memory.delete(item["id"])
    return f"Forgotten: {item['content']}."

  def _memory_command(self, parts):
    if self._memory is None:
      return MEMORY_DISABLED
    action = parts[0].lower() if parts else "list"
    rest = parts[1:]
    if action in {"list", "show"}:
      return self._format_memories(
          "Saved memories:", self._memory.list()
      )
    if action == "search":
      query = " ".join(rest)
      if not query:
        return "Usage: /memory search TEXT"
      return self._format_memories(
          "Matching memories:", self._memory.search(query)
      )
    if action == "add":
      category = (
          rest[0].lower()
          if rest and rest[0].lower() in CATEGORIES
          else None
      )
      content = " ".join(rest[1:] if category else rest)
      if not content:
        return "Usage: /memory add [category] TEXT"
      return self._save_memory(
          category or classify_memory(content), content
      )
    if action == "temporary":
      content = " ".join(rest)
      if not content:
        return "Usage: /memory temporary TEXT"
      return self._save_memory(
          classify_memory(content), content, temporary=True
      )
    if action == "edit":
      if len(rest) < 2 or not rest[0].isdigit():
        return "Usage: /memory edit ID TEXT"
      try:
        changed = self._memory.update(
            int(rest[0]), content=" ".join(rest[1:])
        )
      except MemoryStoreError as exc:
        log.warning("Could not update memory: %s", exc)
        return "I couldn't update that memory, sir."
      return (
          "Updated, sir."
          if changed
          else "There is no memory with that id, sir."
      )
    if action == "delete":
      if len(rest) != 1 or not rest[0].isdigit():
        return "Usage: /memory delete ID"
      changed = self._memory.delete(int(rest[0]))
      return (
          "Forgotten, sir."
          if changed
          else "There is no memory with that id, sir."
      )
    if action == "frequent":
      kind = (
          rest[0].lower()
          if rest and rest[0].lower() in USAGE_KINDS
          else None
      )
      rows = self._memory.frequent(kind)
      if not rows:
        return "I haven't recorded any usage yet, sir."
      return "Most used:\n" + "\n".join(
          f"{item['value']} ({item['uses']} uses)" for item in rows
      )
    if action == "learned":
      return self._format_memories(
          "Learned memories:", self._memory.list(source="learned")
      )
    if action == "episodes":
      if rest and rest[0].lower() == "clear":
        if len(rest) > 1 and rest[1].lower() == "confirm":
          if not self._episode_clear_pending:
            return (
                "Nothing to confirm, sir. Run /memory episodes clear first."
            )
          self._episode_clear_pending = False
          removed = self._memory.clear_episodes()
          return f"Deleted {removed} episodes, sir."
        self._episode_clear_pending = True
        return (
            "This deletes every episodic summary. Run"
            " /memory episodes clear confirm to proceed."
        )
      rows = self._memory.recent_episodes(limit=8)
      if not rows:
        return "I don't have any recent episodes, sir."
      return "Recent events:\n" + "\n".join(
          f"[{row['id']}] {row['created_at'][:10]}: {row['summary']}"
          for row in rows
      )
    if action == "explain":
      if len(rest) != 1 or not rest[0].isdigit():
        return "Usage: /memory explain ID"
      return self._explain_memory(int(rest[0]))
    if action == "clear":
      if rest and rest[0].lower() == "confirm":
        self._memory_clear_pending = False
        removed = self._memory.clear()
        return f"Deleted {removed} memories, sir."
      if rest and rest[0].lower() == "temporary":
        removed = self._memory.clear(temporary_only=True)
        return f"Deleted {removed} temporary memories, sir."
      self._memory_clear_pending = True
      return (
          "This deletes every saved memory. Run /memory clear confirm"
          " to proceed."
      )
    return (
        "Memory commands: list, learned, episodes, explain, search, add,"
        " temporary, edit, delete, frequent, clear."
    )

  def _remember(self, user_message, answer):
    if not self._config.enabled:
      return
    self.conversation.add(user_message, answer)
    self._record(user_message, answer)

  # -- session handling -------------------------------------------------

  def _resume_last_session(self):
    try:
      session_id = self._store.last_session_id()
      if not session_id:
        return
      turns = self._store.session_turns(session_id, limit=self._config.max_turns)
    except HistoryError as exc:
      log.warning("Could not resume the last conversation: %s", exc)
      return
    if not turns:
      return
    self._session_id = session_id
    for turn in turns:
      self.conversation.add(turn["user"], turn["assistant"])
    log.info(
        "Resumed conversation %s with %s exchanges",
        session_id[:8],
        len(turns),
    )

  # -- LLM --------------------------------------------------------------

  def _get_client(self):
    return self._client if self._client is not None else get_client()

  def _complete(self, messages, max_tokens):
    """Return ``(text, ok)``, continuing when the reply hits the token limit.

    A cut-off answer is worse than a slower one, so a reply that stops with
    ``finish_reason == "length"`` is continued up to ``max_continuations``
    times and the pieces are joined into one answer.
    """
    conversation = list(messages)
    parts = []
    continuations = 0

    while True:
      try:
        result = self._get_client().chat(
            conversation, max_tokens=max_tokens
        )
      except LLMConnectionError as exc:
        log.error("LLM connection failed: %s", exc)
        if parts:
          return _join_parts(parts), True
        return FAILURE_REPLIES["connection"], False
      except LLMTimeoutError as exc:
        log.error("LLM request timed out: %s", exc)
        if parts:
          return _join_parts(parts), True
        return FAILURE_REPLIES["timeout"], False
      except LLMError as exc:
        log.error("LLM failure: %s", exc)
        if parts:
          return _join_parts(parts), True
        return FAILURE_REPLIES["other"], False

      parts.append(result.content)
      log.info(
          "LLM reply: %.1fs wall, %s tokens at %.1f tok/s (%s mode, %s)",
          result.total_ms / 1000.0,
          result.completion_tokens,
          result.generated_tokens_per_second,
          self.conversation.mode,
          getattr(result, "finish_reason", "") or "stop",
      )

      truncated = getattr(result, "finish_reason", "") == "length"
      if not truncated or continuations >= self._config.max_continuations:
        break
      continuations += 1
      conversation = conversation + [
          {"role": "assistant", "content": result.content},
          {"role": "user", "content": CONTINUE_PROMPT},
      ]
      log.info(
          "Reply hit the %s-token limit; requesting continuation %s",
          max_tokens,
          continuations,
      )

    return _join_parts(parts), True

  def _summarize(self, transcript):
    messages = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": transcript},
    ]
    try:
      result = self._get_client().chat(
          messages, max_tokens=100, temperature=0.2
      )
    except LLMError as exc:
      log.warning("Summarization request failed: %s", exc)
      return ""
    return result.content

  def _record(self, user_message, answer):
    if self._store is None:
      return
    try:
      self._store.add(
          self._session_id, user_message, answer, self.conversation.mode
      )
    except HistoryError as exc:
      log.warning("Could not save exchange: %s", exc)

  # -- commands ---------------------------------------------------------

  def _command(self, text):
    parts = text.split()
    command = parts[0].lower()
    argument = " ".join(parts[1:]).strip().lower()

    if command == "/new":
      self.conversation.reset()
      self._session_id = uuid.uuid4().hex
      self._forget_pending = False
      self._memory_clear_pending = False
      self._episode_clear_pending = False
      self._session.clear()
      self._learned.clear()
      self._last_episode = ""
      self._pending_tool = None
      self._pending_plan = None
      self._pending_task = None
      return "Starting a new conversation, sir."
    if command == "/mode":
      if not argument:
        return (
            f"Current answer mode: {self.mode}. Available modes: "
            + ", ".join(MODES)
            + "."
        )
      if argument not in MODES:
        return (
            f"Unknown mode {argument!r}. Available modes: "
            + ", ".join(MODES)
            + "."
        )
      self.conversation.mode = argument
      return f"{argument.capitalize()} answers enabled, sir."
    if command in {"/web", "/search"}:
      if not argument:
        return "Usage: /web <question>"
      original = text.split(maxsplit=1)[1] if len(parts) > 1 else argument
      return self._answer_web(original, user_text=text)
    if command == "/history":
      return self._history()
    if command == "/intent":
      if not argument:
        return "Usage: /intent <text>"
      intent = self.router.route(argument)
      return intent.to_json() if intent is not None else NO_INTENT_JSON
    if command == "/tools":
      return self._tools()
    if command == "/confirm":
      return self._confirm_pending()
    if command == "/cancel":
      if (
          self._pending_tool is None
          and self._pending_plan is None
          and self._pending_task is None
      ):
        return "There is nothing to cancel, sir."
      self._cancel_pending()
      return "Cancelled, sir."
    if command == "/memory":
      return self._memory_command(parts[1:])
    if command in {"/notes", "/note"}:
      return self._notes_command(text)
    if command in {"/habits", "/habit"}:
      return self._habit_command(text)
    if command in {"/goals", "/goal"}:
      return self._goal_command(text)
    if command == "/time":
      return self._time_command(text)
    if command in {"/calendar", "/cal"}:
      return self._calendar_command(text)
    if command in {"/email", "/mail"}:
      return self._email_command(text)
    if command in {"/pomodoro", "/pomo"}:
      return self._pomodoro_command(text)
    if command in {"/clipboard", "/clipboard-history"}:
      return self._clipboard_command(text)
    if command in {"/summary", "/summaries"}:
      return self._summary_command(text)
    if command in {"/dev", "/developer"}:
      return self._developer_command(text)
    if command == "/plan":
      return self._plan_command(text)
    if command in {"/todo", "/todos"}:
      return self._todo_command(text)
    if command == "/tasks":
      return self._tasks_command(parts[1:])
    if command in {"/documents", "/docs"}:
      return self._documents_command(text)
    if command == "/forget":
      return self._forget(argument)
    if command == "/help":
      return HELP_TEXT
    return f"Unknown command {command}. Try /help."

  # -- document commands ------------------------------------------------

  @staticmethod
  def _command_value(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
      return text[1:-1].strip()
    return text

  def _documents_command(self, text):
    service = self._get_documents()
    if service is None:
      return DOCUMENT_DISABLED
    body = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
    if not body or body.lower() in {"help", "?"}:
      return DOCUMENTS_HELP
    parts = body.split(maxsplit=1)
    action = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if action in {"list", "indexed"}:
      try:
        rows = service.list(50)
      except DocumentError as exc:
        return f"I couldn't read the document index, sir: {exc}."
      if not rows:
        return "The document index is empty, sir. Use /documents index PATH."
      lines = [
          f"- {row['title']} ({row['format']}, {row['chunks']} passage(s))"
          f" [{service.relative(row['path'])}]"
          for row in rows
      ]
      return f"Indexed documents:\n" + "\n".join(lines)
    if action == "index":
      path = self._command_value(rest) or "Documents"
      return self._document_index(
          service, Intent("document_index", 1.0, {"path": path})
      )
    if action == "search":
      query = self._command_value(rest)
      if not query:
        return "Usage: /documents search QUERY"
      return self._document_search(
          service, Intent("document_search", 1.0, {"query": query})
      )
    if action == "ask":
      query = self._command_value(rest)
      if not query:
        return "Usage: /documents ask QUESTION"
      return self._document_ask(
          service, Intent("document_ask", 1.0, {"query": query})
      )
    if action in {
        "read",
        "summarize",
        "dates",
        "actions",
        "tables",
        "explain",
    }:
      path = self._command_value(rest)
      return self._answer_document(
          Intent("document_" + action, 1.0, {"path": path}), text
      )
    if action == "compare":
      pieces = [self._command_value(item) for item in rest.split("|")]
      if len(pieces) == 1 and " and " in rest:
        pieces = [self._command_value(item) for item in rest.split(" and ", 1)]
      if len(pieces) != 2 or not all(pieces):
        return "Usage: /documents compare A | B"
      return self._answer_document(
          Intent(
              "document_compare",
              1.0,
              {"first": pieces[0], "second": pieces[1]},
          ),
          text,
      )
    if action == "convert":
      pieces = [self._command_value(item) for item in rest.split("|")]
      if len(pieces) != 2 or not all(pieces):
        return "Usage: /documents convert SOURCE | TARGET_OR_FORMAT"
      source, second = pieces
      suffix = Path(second).suffix.lower()
      try:
        if suffix in {
            ".pdf",
            ".docx",
            ".txt",
            ".md",
            ".markdown",
            ".csv",
            ".xlsx",
            ".xlsm",
        }:
          result = service.convert(source, target=second)
        elif second.lower() in {
            "pdf",
            "docx",
            "txt",
            "md",
            "markdown",
            "csv",
            "xlsx",
        }:
          result = service.convert(source, format=second.lower())
        else:
          return "That target format is not supported, sir."
      except DocumentError as exc:
        return f"I couldn't convert that document, sir: {exc}."
      return f"Converted {service.relative(result['source'])} to {result['path']}."
    if action == "create":
      pieces = rest.split("|", 1)
      path = self._command_value(pieces[0])
      content = pieces[1].strip() if len(pieces) > 1 else ""
      if not path:
        return "Usage: /documents create PATH | TEXT"
      try:
        result = service.write(path, content, Path(path).stem.title())
      except DocumentError as exc:
        return f"I couldn't create that document, sir: {exc}."
      return f"Created {result['path']} with {result['chars']} characters."
    if action == "report":
      pieces = [
          self._command_value(item)
          for item in rest.split("|")
          if self._command_value(item)
      ]
      if not pieces:
        return "Usage: /documents report [OUTPUT | PATH...]"
      if len(pieces) == 1:
        output, paths = "document_report.md", pieces
      else:
        output, paths = pieces[0], pieces[1:]
      try:
        result = service.report(paths, output)
      except DocumentError as exc:
        return f"I couldn't create that report, sir: {exc}."
      return (
          f"Created a report on {result['documents']} document(s) at"
          f" {result['path']}."
      )
    if action == "remove":
      path = self._command_value(rest)
      if not path:
        return "Usage: /documents remove PATH"
      try:
        service.remove(path)
      except DocumentError as exc:
        return f"I couldn't remove that document, sir: {exc}."
      return f"Removed {path} from the document index, sir."
    if action == "last":
      if not self._last_document:
        return "I have not opened a document yet, sir."
      return f"The last document I used was {self._last_document}."
    return DOCUMENTS_HELP

  def _tools(self):
    if self.registry is None:
      return "Computer tools are disabled, sir."
    entries = [
        f"{name} ({self.registry.get(name).permission})"
        for name in self.registry.names()
    ]
    return "Available tools: " + ", ".join(entries) + "."

  def _history(self):
    if self._store is None:
      return "Conversation history is not available, sir."
    try:
      rows = self._store.recent(self._session_id, limit=3)
    except HistoryError as exc:
      log.warning("Could not read history: %s", exc)
      return "I could not read the conversation history, sir."
    if not rows:
      return "No exchanges in this conversation yet, sir."
    return "Recent exchanges:\n" + "\n".join(
        f"You: {row['user']}\nJarvis: {row['assistant']}" for row in rows
    )

  def _forget(self, argument):
    if self._store is None:
      return "Conversation history is not available, sir."
    if argument and argument not in {"confirm", "yes"}:
      return "Type /forget to review the deletion, then /forget confirm."

    if not argument:
      try:
        count = self._store.count()
      except HistoryError as exc:
        log.warning("Could not count history: %s", exc)
        return "I could not read the conversation history, sir."
      if count == 0:
        self._forget_pending = False
        return "There is no saved conversation history to delete, sir."
      self._forget_pending = True
      plural = "" if count == 1 else "s"
      return (
          f"This will permanently delete {count} saved exchange{plural}."
          " Run /forget confirm to proceed."
      )

    if not self._forget_pending:
      return "Nothing to confirm, sir. Run /forget first."
    try:
      self._store.clear()
    except HistoryError as exc:
      log.warning("Could not delete history: %s", exc)
      return "I could not delete the conversation history, sir."
    self._forget_pending = False
    self._pending_tool = None
    self.conversation.reset()
    self._session_id = uuid.uuid4().hex
    return "Saved conversation history deleted, sir. Starting fresh."
