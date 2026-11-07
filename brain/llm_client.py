"""Persistent HTTPX client for the local llama-server.

One client is created per process and reused for every request, as the
specification requires. Failures are translated into the LLMError family so
callers never see raw httpx exceptions.
"""

from __future__ import annotations

import logging
import time

import httpx

from brain.errors import (
    LLMConnectionError,
    LLMHTTPError,
    LLMResponseError,
    LLMTimeoutError,
)
from brain.output_parser import parse_chat_completion

log = logging.getLogger(__name__)

_CLIENT = None


class LLMClient:
  """Minimal OpenAI-compatible client for a single local server."""

  def __init__(self, llama_config, llm_config, transport=None):
    self._base_url = llama_config.base_url.rstrip("/")
    self._max_tokens = llm_config.max_tokens
    self._temperature = llm_config.temperature
    self._max_retries = llm_config.max_retries

    timeout = httpx.Timeout(
        connect=llm_config.connect_timeout_s,
        read=llm_config.read_timeout_s,
        write=10.0,
        pool=5.0,
    )
    self._client = httpx.Client(
        base_url=self._base_url,
        timeout=timeout,
        transport=transport,
        headers={"Content-Type": "application/json"},
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
    )

  @property
  def base_url(self):
    return self._base_url

  def health(self):
    """Return True when the server reports status ok."""
    try:
      response = self._client.get("/health", timeout=2.0)
    except httpx.HTTPError:
      return False
    return response.status_code == 200

  def props(self):
    """Return the server /props payload."""
    try:
      response = self._client.get("/props", timeout=5.0)
    except httpx.HTTPError as exc:
      raise LLMConnectionError(f"Cannot read server properties: {exc}") from exc
    if response.status_code != 200:
      raise LLMHTTPError(response.status_code, response.text)
    try:
      return response.json()
    except ValueError as exc:
      raise LLMResponseError("Server properties are not valid JSON") from exc

  def chat(self, messages, max_tokens=None, temperature=None):
    """Send a chat completion request and return a ChatResult."""
    if not messages:
      raise ValueError("messages must not be empty")

    payload = {
        "messages": messages,
        "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
        "temperature": (
            self._temperature if temperature is None else temperature
        ),
    }

    attempts = max(1, self._max_retries + 1)
    for attempt in range(1, attempts + 1):
      try:
        response = self._client.post("/v1/chat/completions", json=payload)
      except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        if attempt < attempts:
          log.warning(
              "LLM connection failed (attempt %s/%s), retrying", attempt, attempts
          )
          time.sleep(0.5)
          continue
        raise LLMConnectionError(
            f"Cannot reach the LLM server at {self._base_url}"
        ) from exc
      except httpx.TimeoutException as exc:
        raise LLMTimeoutError(
            "The LLM server did not answer within the read timeout"
        ) from exc
      except httpx.HTTPError as exc:
        raise LLMConnectionError(f"HTTP transport error: {exc}") from exc

      if response.status_code == 503 and attempt < attempts:
        log.warning("LLM server is still loading, retrying in 1s")
        time.sleep(1.0)
        continue

      if response.status_code != 200:
        raise LLMHTTPError(response.status_code, response.text)

      try:
        data = response.json()
      except ValueError as exc:
        raise LLMResponseError("Response is not valid JSON") from exc

      result = parse_chat_completion(data)
      log.debug(
          "LLM reply: %s chars, %s prompt tokens, %s generated tokens",
          len(result.content),
          result.prompt_tokens,
          result.completion_tokens,
      )
      return result

    raise LLMConnectionError("LLM request failed after retries")

  def close(self):
    self._client.close()

  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc, traceback):
    self.close()

  def __del__(self):
    # Best-effort cleanup; close() should normally be called explicitly.
    client = getattr(self, "_client", None)
    if client is not None and not client.is_closed:
      try:
        client.close()
      except Exception:
        pass


def get_client(settings=None):
  """Return the process-wide client, creating it on first use."""
  global _CLIENT
  if _CLIENT is None:
    if settings is None:
      from core.config import load_settings

      settings = load_settings()
    _CLIENT = LLMClient(settings.llama, settings.llm)
  return _CLIENT


def close_client():
  """Close and forget the shared client."""
  global _CLIENT
  if _CLIENT is not None:
    _CLIENT.close()
    _CLIENT = None
