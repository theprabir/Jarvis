"""Conversation-aware assistant used by every interface.

Wraps the LLM client with multi-turn context, answer modes, history
persistence, clarification and graceful failures. Deterministic intents
(arithmetic, clock, calendar, commands) are answered by Python; everything
else falls back to the model. Both the text loop and the voice interface call
:meth:`Assistant.reply`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from brain.errors import LLMConnectionError, LLMError, LLMTimeoutError
from brain.llm_client import get_client
from core.calculator import format_number
from core.conversation import (
    MODES,
    Conversation,
    needs_clarification,
)
from core.history import HistoryError, HistoryStore
from core.intents import IntentRouter

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

FAILURE_REPLIES = {
    "connection": "I cannot reach my language model, sir. Please check the server.",
    "timeout": "My language model is taking too long to respond, sir.",
    "other": "My language model returned an error, sir.",
}

HELP_TEXT = (
    "Commands: /new starts a fresh conversation, /mode short|detailed|technical"
    " changes how much I say, /history shows recent exchanges, /intent TEXT"
    " shows the detected intent as JSON, /forget deletes saved history after"
    " confirmation, /help lists this."
)

NO_INTENT_JSON = (
    '{"confidence": 0.0, "entities": {}, "intent": null,'
    ' "source": "deterministic"}'
)


class Assistant:
  """Stateful conversation front end over the local model."""

  def __init__(self, settings, client=None):
    self._settings = settings
    self._config = settings.conversation
    self._client = client
    self.conversation = Conversation(self._config)
    self.router = IntentRouter()
    self._session_id = uuid.uuid4().hex
    self._forget_pending = False
    self._store = None
    if self._config.enabled:
      try:
        self._store = HistoryStore(self._config.resolved_history_db)
      except HistoryError as exc:
        log.warning("Conversation history disabled: %s", exc)
    if self._store is not None and self._config.resume_last_session:
      self._resume_last_session()

  @property
  def mode(self):
    return self.conversation.mode

  @property
  def session_id(self):
    return self._session_id

  def reply(self, text):
    """Answer one user message (typed or transcribed) with conversation context."""
    text = (text or "").strip()
    if not text:
      return "I didn't catch that, sir."
    if text.startswith("/"):
      return self._command(text)
    # Deterministic intents come first: short questions like "what time is it?"
    # contain reference words but are fully understood without context.
    intent = self.router.route(text)
    if self.router.is_confident(intent):
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
    messages.append({"role": "user", "content": text})
    answer, ok = self._complete(messages, conversation.max_tokens())

    if ok and self._config.enabled:
      conversation.add(text, answer)
      self._record(text, answer)
    return answer

  def close(self):
    if self._store is not None:
      self._store.close()
      self._store = None

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
    """Return ``(text, ok)``; failures become spoken-safe sentences."""
    try:
      result = self._get_client().chat(messages, max_tokens=max_tokens)
    except LLMConnectionError as exc:
      log.error("LLM connection failed: %s", exc)
      return FAILURE_REPLIES["connection"], False
    except LLMTimeoutError as exc:
      log.error("LLM request timed out: %s", exc)
      return FAILURE_REPLIES["timeout"], False
    except LLMError as exc:
      log.error("LLM failure: %s", exc)
      return FAILURE_REPLIES["other"], False

    log.info(
        "LLM reply: %.1fs wall, %s tokens at %.1f tok/s (%s mode)",
        result.total_ms / 1000.0,
        result.completion_tokens,
        result.generated_tokens_per_second,
        self.conversation.mode,
    )
    return result.content, True

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
    if command == "/history":
      return self._history()
    if command == "/intent":
      if not argument:
        return "Usage: /intent <text>"
      intent = self.router.route(argument)
      return intent.to_json() if intent is not None else NO_INTENT_JSON
    if command == "/forget":
      return self._forget(argument)
    if command == "/help":
      return HELP_TEXT
    return f"Unknown command {command}. Try /help."

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
    self.conversation.reset()
    self._session_id = uuid.uuid4().hex
    return "Saved conversation history deleted, sir. Starting fresh."
