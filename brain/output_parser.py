"""Structured parsing of llama-server chat completion responses.

The server returns timings per request; keeping them lets later phases
measure latency and tokens per second without re-instrumenting the client.
"""

from __future__ import annotations

from dataclasses import dataclass

from brain.errors import LLMResponseError


@dataclass(frozen=True)
class ChatResult:
  """A validated chat completion."""

  content: str
  model: str
  prompt_tokens: int
  completion_tokens: int
  prompt_ms: float
  predicted_ms: float

  @property
  def generated_tokens_per_second(self):
    if self.predicted_ms <= 0:
      return 0.0
    return self.completion_tokens / (self.predicted_ms / 1000.0)

  @property
  def prompt_tokens_per_second(self):
    if self.prompt_ms <= 0:
      return 0.0
    return self.prompt_tokens / (self.prompt_ms / 1000.0)

  @property
  def total_ms(self):
    return self.prompt_ms + self.predicted_ms


def parse_chat_completion(payload):
  """Validate a chat completion payload and return a ChatResult."""
  if not isinstance(payload, dict):
    raise LLMResponseError("Response is not a JSON object")

  choices = payload.get("choices")
  if not isinstance(choices, list) or not choices:
    raise LLMResponseError("Response has no choices")

  first = choices[0]
  if not isinstance(first, dict):
    raise LLMResponseError("Response choice has an unexpected shape")

  message = first.get("message")
  if not isinstance(message, dict):
    raise LLMResponseError("Response choice has no message")

  content = message.get("content")
  if not isinstance(content, str):
    raise LLMResponseError("Response message has no text content")
  content = content.strip()
  if not content:
    raise LLMResponseError("Response message is empty")

  usage = payload.get("usage") or {}
  timings = payload.get("timings") or {}

  return ChatResult(
      content=content,
      model=str(payload.get("model") or ""),
      prompt_tokens=int(usage.get("prompt_tokens") or 0),
      completion_tokens=int(usage.get("completion_tokens") or 0),
      prompt_ms=float(timings.get("prompt_ms") or 0.0),
      predicted_ms=float(timings.get("predicted_ms") or 0.0),
  )
