"""Multi-turn conversation state: history, compression and answer modes.

The model has a 1536-token context shared with the reply budget, so only the
most recent exchanges are sent verbatim. Older ones are compressed into a short
summary by the model itself, with a deterministic extract as a fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

MODE_INSTRUCTIONS = {
    "short": "Answer in at most two short sentences.",
    "detailed": (
        "Answer thoroughly but concisely, covering the key details and ending"
        " with a brief conclusion."
    ),
    "technical": (
        "Answer with precise technical detail: correct terminology, concrete"
        " mechanisms and specifics. Do not oversimplify."
    ),
}
MODES = tuple(MODE_INSTRUCTIONS)

# Words that cannot be interpreted without earlier context.
REFERENCE_WORDS = {
    "it",
    "its",
    "that",
    "this",
    "they",
    "them",
    "those",
    "these",
    "there",
    "he",
    "she",
    "him",
    "her",
}

MAX_SUMMARY_CHARS = 700


@dataclass
class Turn:
  """One completed request and reply."""

  user: str
  assistant: str


def needs_clarification(text, has_history):
  """True when a short request only refers to earlier context we do not have."""
  if has_history or not text.strip():
    return False
  words = [word.strip(".,;:!?\"'") for word in text.lower().split()]
  return len(words) <= 6 and bool(set(words) & REFERENCE_WORDS)


class Conversation:
  """In-memory transcript with a mode and a compressed summary of older turns."""

  def __init__(self, config, mode=None):
    self._config = config
    self.mode = mode or config.mode
    self.turns = []
    self.summary = ""

  @property
  def has_history(self):
    return bool(self.turns)

  def reset(self):
    self.turns.clear()
    self.summary = ""

  def add(self, user, assistant):
    self.turns.append(Turn(user=user, assistant=assistant))

  def max_tokens(self):
    """Reply limit for the active answer mode."""
    return getattr(self._config, f"{self.mode}_max_tokens")

  def context_turns(self):
    """Return the stored turns that fit in the configured prompt budget."""
    budget = self._config.max_context_chars
    selected = []
    for turn in reversed(self.turns):
      cost = len(turn.user) + len(turn.assistant)
      if selected and budget - cost < 0:
        break
      selected.append(turn)
      budget -= cost
    return list(reversed(selected))

  def messages(self, system_prompt):
    """Build the stable chat prefix for the next request.

    The system prompt and stored turns are kept byte-identical between calls so
    llama.cpp can reuse its prompt cache. Mutable context (the summary and
    learned memory) is appended as a trailing system message by the assistant.
    """
    system = f"{system_prompt.strip()} {MODE_INSTRUCTIONS[self.mode]}".strip()
    messages = [{"role": "system", "content": system}]
    for turn in self.context_turns():
      messages.append({"role": "user", "content": turn.user})
      messages.append({"role": "assistant", "content": turn.assistant})
    return messages

  def maybe_summarize(self, summarize):
    """Compress the oldest turns once the transcript grows past the limit."""
    config = self._config
    if len(self.turns) <= config.summarize_after:
      return False
    keep = max(1, config.max_turns)
    dropped = self.turns[:-keep]
    if not dropped:
      return False

    transcript = "\n".join(
        f"User: {turn.user}\nJarvis: {turn.assistant}" for turn in dropped
    )
    summary = ""
    if summarize is not None:
      try:
        summary = summarize(transcript) or ""
      except Exception:
        log.warning("Conversation summarization failed", exc_info=True)
    if not summary.strip():
      summary = self._fallback_summary(dropped)

    combined = f"{self.summary} {summary}".strip()
    self.summary = combined[-MAX_SUMMARY_CHARS:]
    del self.turns[:-keep]
    log.info("Compressed %s turns into a %s-char summary", len(dropped),
             len(self.summary))
    return True

  @staticmethod
  def _fallback_summary(turns):
    """Extractive summary used when the model cannot compress the transcript."""
    parts = [turn.user[:80].strip() for turn in turns]
    return "Earlier the user asked about: " + "; ".join(parts) + "."
