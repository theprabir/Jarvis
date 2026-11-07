"""Single-shot brain interface used by the CLI.

Thin compatibility wrapper around :mod:`brain.llm_client`. Failures are
turned into short sentences that can be spoken; they are never raised into
the interactive loop. Phase 3 replaces this with conversation-aware
orchestration.
"""

from __future__ import annotations

import logging

from brain.errors import LLMConnectionError, LLMError, LLMTimeoutError
from brain.llm_client import get_client

log = logging.getLogger(__name__)


def ask_brain(prompt, system_prompt=""):
  """Send one prompt to the local model and return a spoken-safe string."""
  messages = []
  if system_prompt:
    messages.append({"role": "system", "content": system_prompt})
  messages.append({"role": "user", "content": prompt})

  try:
    result = get_client().chat(messages)
  except LLMConnectionError as exc:
    log.error("LLM connection failed: %s", exc)
    return "I cannot reach my language model, sir. Please check the server."
  except LLMTimeoutError as exc:
    log.error("LLM request timed out: %s", exc)
    return "My language model is taking too long to respond, sir."
  except LLMError as exc:
    log.error("LLM failure: %s", exc)
    return "My language model returned an error, sir."

  log.info(
      "LLM reply: %.1fs wall, %s tokens at %.1f tok/s",
      result.total_ms / 1000.0,
      result.completion_tokens,
      result.generated_tokens_per_second,
  )
  return result.content
