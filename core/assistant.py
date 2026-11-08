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
from core.intents import TOOL_INTENTS, IntentRouter
from tools import build_registry

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
    "Commands: /new starts a fresh conversation, /mode short|detailed|technical"
    " changes how much I say, /history shows recent exchanges, /intent TEXT"
    " shows the detected intent as JSON, /tools lists computer tools, /confirm"
    " and /cancel handle pending actions, /forget deletes saved history after"
    " confirmation, /help lists this."
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
    self._pending_tool = None
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
    if self._pending_tool is not None:
      answer = self._confirmation_reply(text)
      if answer is not None:
        return answer
      # Any other request abandons the pending action instead of surprising
      # the user later.
      self._pending_tool = None

    # Deterministic intents come first: short questions like "what time is it?"
    # contain reference words but are fully understood without context.
    intent = self.router.route(text)
    if self.router.is_confident(intent):
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
      return f"{tool.describe(arguments)}. Say confirm to proceed, or cancel."

    result = registry.execute(intent.name, arguments)
    if result.ok:
      log.info("Tool %s completed", intent.name)
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
      return "Cancelled, sir."
    return None

  def _confirm_pending(self):
    pending, self._pending_tool = self._pending_tool, None
    if pending is None:
      return "There is nothing waiting for confirmation, sir."
    name, arguments, user_text = pending
    if self.registry is None:
      return "Computer tools are disabled, sir."
    result = self.registry.execute(name, arguments)
    if result.ok:
      log.info("Confirmed tool %s completed", name)
      self._remember(user_text, result.text)
    else:
      log.info("Confirmed tool %s failed: %s", name, result.error)
    return result.text

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
      self._pending_tool = None
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
    if command == "/tools":
      return self._tools()
    if command == "/confirm":
      return self._confirm_pending()
    if command == "/cancel":
      if self._pending_tool is None:
        return "There is nothing to cancel, sir."
      self._pending_tool = None
      return "Cancelled, sir."
    if command == "/forget":
      return self._forget(argument)
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
