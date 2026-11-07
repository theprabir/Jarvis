"""Phase 1 tests: configuration, response parsing, client errors, argv.

These run offline; no llama-server is required.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from brain.errors import (
    LLMConnectionError,
    LLMHTTPError,
    LLMResponseError,
    LLMTimeoutError,
)
from brain.llm_client import LLMClient
from brain.output_parser import parse_chat_completion
from core.config import ConfigError, LlamaConfig, LlmConfig, load_settings
from core.server_manager import LlamaServer

VALID_PAYLOAD = {
    "model": "test-model",
    "choices": [{"message": {"role": "assistant", "content": " Paris. "}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    "timings": {"prompt_ms": 1000.0, "predicted_ms": 500.0},
}


def write_config(directory, body):
  path = Path(directory) / "settings.toml"
  path.write_text(body, encoding="utf-8")
  return path


class ConfigTests(unittest.TestCase):
  def test_project_config_loads(self):
    settings = load_settings()
    self.assertEqual(settings.llama.port, 8080)
    self.assertEqual(settings.llama.parallel_slots, 1)
    self.assertEqual(settings.llm.max_tokens, 50)

  def test_unknown_key_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = write_config(tmp, "[llama]\nthreds = 2\n")
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_invalid_value_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = write_config(tmp, "[llama]\nthreads = 99\n")
      with self.assertRaises(ConfigError):
        load_settings(path)

  def test_missing_file_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      with self.assertRaises(ConfigError):
        load_settings(Path(tmp) / "nope.toml")

  def test_model_path_is_resolved(self):
    config = LlamaConfig()
    self.assertTrue(config.resolved_model_path.is_absolute())


class OutputParserTests(unittest.TestCase):
  def test_valid_payload(self):
    result = parse_chat_completion(VALID_PAYLOAD)
    self.assertEqual(result.content, "Paris.")
    self.assertEqual(result.prompt_tokens, 12)
    self.assertEqual(result.completion_tokens, 3)
    self.assertAlmostEqual(result.generated_tokens_per_second, 6.0)
    self.assertAlmostEqual(result.prompt_tokens_per_second, 12.0)

  def test_missing_choices(self):
    with self.assertRaises(LLMResponseError):
      parse_chat_completion({"model": "x"})

  def test_missing_message(self):
    with self.assertRaises(LLMResponseError):
      parse_chat_completion({"choices": [{}]})

  def test_empty_content(self):
    payload = {"choices": [{"message": {"content": "   "}}]}
    with self.assertRaises(LLMResponseError):
      parse_chat_completion(payload)


def make_client(handler, max_retries=0):
  return LLMClient(
      LlamaConfig(),
      LlmConfig(max_retries=max_retries),
      transport=httpx.MockTransport(handler),
  )


class ClientTests(unittest.TestCase):
  def test_successful_chat(self):
    client = make_client(lambda request: httpx.Response(200, json=VALID_PAYLOAD))
    try:
      result = client.chat([{"role": "user", "content": "capital of France?"}])
    finally:
      client.close()
    self.assertEqual(result.content, "Paris.")

  def test_http_error_is_mapped(self):
    client = make_client(lambda request: httpx.Response(500, text="boom"))
    try:
      with self.assertRaises(LLMHTTPError) as caught:
        client.chat([{"role": "user", "content": "hi"}])
    finally:
      client.close()
    self.assertEqual(caught.exception.status_code, 500)

  def test_invalid_json_is_mapped(self):
    client = make_client(lambda request: httpx.Response(200, text="not json"))
    try:
      with self.assertRaises(LLMResponseError):
        client.chat([{"role": "user", "content": "hi"}])
    finally:
      client.close()

  def test_connection_error_is_mapped(self):
    def handler(request):
      raise httpx.ConnectError("connection refused", request=request)

    client = make_client(handler)
    try:
      with self.assertRaises(LLMConnectionError):
        client.chat([{"role": "user", "content": "hi"}])
    finally:
      client.close()

  def test_timeout_is_mapped(self):
    def handler(request):
      raise httpx.ReadTimeout("too slow", request=request)

    client = make_client(handler)
    try:
      with self.assertRaises(LLMTimeoutError):
        client.chat([{"role": "user", "content": "hi"}])
    finally:
      client.close()

  def test_503_is_retried(self):
    calls = {"count": 0}

    def handler(request):
      calls["count"] += 1
      if calls["count"] == 1:
        return httpx.Response(503, text="loading")
      return httpx.Response(200, json=VALID_PAYLOAD)

    original_sleep = __import__("time").sleep
    __import__("time").sleep = lambda seconds: None
    client = make_client(handler, max_retries=1)
    try:
      result = client.chat([{"role": "user", "content": "hi"}])
    finally:
      client.close()
      __import__("time").sleep = original_sleep
    self.assertEqual(result.content, "Paris.")
    self.assertEqual(calls["count"], 2)


class ServerCommandTests(unittest.TestCase):
  def test_command_uses_configured_flags(self):
    server = LlamaServer(LlamaConfig())
    command = server.command()
    self.assertIn("-np", command)
    self.assertEqual(command[command.index("-np") + 1], "1")
    self.assertEqual(command[command.index("-t") + 1], "2")
    self.assertEqual(command[command.index("-ub") + 1], "64")
    self.assertEqual(command[command.index("-ngl") + 1], "0")


if __name__ == "__main__":
  unittest.main()
