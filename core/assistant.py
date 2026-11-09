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

from brain.errors import LLMConnectionError, LLMError, LLMTimeoutError
from brain.llm_client import get_client
from core.calculator import format_number
from core.conversation import (
    MODES,
    Conversation,
    needs_clarification,
)
from core.history import HistoryError, HistoryStore
from core.intents import TOOL_INTENTS, IntentRouter, plan_file_actions
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
from core.timers import TimerError, TimerService
from core.utilities import humanize_duration
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
    " (list, learned, episodes, explain ID), /intent TEXT"
    " shows the detected intent as JSON, /tools lists computer tools, /confirm"
    " and /cancel handle pending actions, /forget deletes saved history after"
    " confirmation. Timers, reminders, conversions, weather and text tools work"
    " directly. /help lists this."
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
    self._episode_clear_pending = False
    self._session = ShortTermMemory(settings.memory.short_term_items)
    self._learned = set()
    self._last_episode = ""
    self._searcher = None
    self._weather = None
    self._timers = None
    self._notifier = None
    self.registry = (
        build_registry(settings) if settings.tools.enabled else None
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
    if self._pending_tool is not None or self._pending_plan is not None:
      answer = self._confirmation_reply(text)
      if answer is not None:
        return answer
      # Any other request abandons the pending action instead of surprising
      # the user later.
      self._pending_tool = None
      self._pending_plan = None

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
        answer = self._answer_intent(intent)
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

  def close(self):
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

  # -- web search -------------------------------------------------------

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
      return None
    tool = registry.get(intent.name)
    if tool is None:
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
      self._pending_tool = None
      self._pending_plan = None
      return "Cancelled, sir."
    return None

  def _confirm_pending(self):
    if self._pending_plan is not None:
      return self._run_plan()
    pending, self._pending_tool = self._pending_tool, None
    if pending is None:
      return "There is nothing waiting for confirmation, sir."
    name, arguments, user_text = pending
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

  # -- deterministic intents -------------------------------------------

  def _answer_intent(self, intent):
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
    if intent.name in {
        "timer_set",
        "reminder_set",
        "timer_list",
        "timer_cancel",
    }:
      return self._answer_timers(intent)
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
      if self._pending_tool is None and self._pending_plan is None:
        return "There is nothing to cancel, sir."
      self._pending_tool = None
      self._pending_plan = None
      return "Cancelled, sir."
    if command == "/forget":
      return self._forget(argument)
    if command == "/memory":
      return self._memory_command(parts[1:])
    if command == "/help":
      return HELP_TEXT
    return f"Unknown command {command}. Try /help."

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
