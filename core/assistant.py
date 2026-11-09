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
from core.intents import TOOL_INTENTS, IntentRouter, plan_file_actions
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
    "Commands: /new starts a fresh conversation, /mode short|detailed|technical"
    " changes how much I say, /web TEXT answers from web sources, /history"
    " shows recent exchanges, /intent TEXT shows the detected intent as JSON,"
    " /tools lists computer tools, /confirm and /cancel handle pending"
    " actions, /forget deletes saved history after confirmation, /help lists"
    " this."
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
    self._pending_plan = None
    self._searcher = None
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
    messages.append({"role": "user", "content": text})
    answer, ok = self._complete(messages, conversation.max_tokens())

    if ok and self._config.enabled:
      conversation.add(text, answer)
      self._record(text, answer)
    return answer

  def close(self):
    if self._searcher is not None:
      self._searcher.close()
      self._searcher = None
    if self._store is not None:
      self._store.close()
      self._store = None

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
