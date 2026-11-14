"""Feature set 4.23 tests: lazy model startup and idle unloading."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

import httpx

from brain.errors import LLMConnectionError
from brain.llm_client import LLMClient
from core.config import ConfigError, LlamaConfig, LlmConfig, load_settings
from core.model_manager import ModelManager
from core.server_manager import ServerError

VALID_PAYLOAD = {
    "model": "test-model",
    "choices": [{"message": {"role": "assistant", "content": " Paris. "}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    "timings": {"prompt_ms": 1000.0, "predicted_ms": 500.0},
}


class Clock:
  def __init__(self):
    self.now = 0.0

  def __call__(self):
    return self.now

  def advance(self, seconds):
    self.now += seconds


class FakeServer:
  def __init__(self):
    self._pid = None
    self.started = 0
    self.stopped = 0
    self.healthy = False
    self.fail_start = False
    self.startup_seconds = 0.1

  @property
  def pid(self):
    return self._pid

  def is_healthy(self):
    return self.healthy

  def ensure_running(self):
    self.started += 1
    if self.fail_start:
      raise ServerError("model failed")
    self.healthy = True
    self._pid = 4242
    return True

  def stop(self):
    self.stopped += 1
    self.healthy = False
    self._pid = None


def make_settings(idle=10.0, lazy=True):
  base = load_settings()
  return dataclasses.replace(
      base,
      llama=dataclasses.replace(
          base.llama, lazy_start=lazy, idle_unload_s=idle
      ),
  )


class ModelManagerTests(unittest.TestCase):
  def make(self, idle=10.0, lazy=True, server=None):
    clock = Clock()
    server = server or FakeServer()
    manager = ModelManager(
        make_settings(idle=idle, lazy=lazy),
        server=server,
        clock=clock,
        start_monitor=False,
    )
    return manager, server, clock

  def test_lazy_start_does_not_load_at_construction(self):
    manager, server, _clock = self.make()
    self.assertEqual(server.started, 0)
    self.assertFalse(manager.is_loaded)

  def test_first_request_starts_the_model_once(self):
    manager, server, _clock = self.make()
    manager.before_request()
    manager.after_request()
    manager.before_request()
    manager.after_request()
    self.assertEqual(server.started, 1)
    self.assertTrue(manager.is_loaded)

  def test_idle_server_is_unloaded_once(self):
    manager, server, clock = self.make(idle=10)
    manager.before_request()
    manager.after_request()
    clock.advance(11)
    self.assertTrue(manager._check_idle())
    self.assertEqual(server.stopped, 1)
    self.assertFalse(manager.is_loaded)
    self.assertFalse(manager._check_idle())

  def test_active_request_blocks_unload(self):
    manager, server, clock = self.make(idle=10)
    manager.before_request()
    clock.advance(100)
    self.assertFalse(manager._check_idle())
    self.assertEqual(server.stopped, 0)
    manager.after_request()
    clock.advance(11)
    self.assertTrue(manager._check_idle())

  def test_disabled_idle_unload_keeps_the_server(self):
    manager, server, clock = self.make(idle=0)
    manager.before_request()
    manager.after_request()
    clock.advance(1000)
    self.assertFalse(manager._check_idle())
    self.assertEqual(server.stopped, 0)

  def test_external_server_is_not_auto_unloaded(self):
    server = FakeServer()
    server.healthy = True
    server._pid = None
    manager, server, clock = self.make(idle=10, server=server)
    manager.ensure_loaded()
    clock.advance(100)
    self.assertFalse(manager._check_idle())
    self.assertEqual(server.stopped, 0)

  def test_start_failure_releases_the_active_count(self):
    server = FakeServer()
    server.fail_start = True
    manager, _server, _clock = self.make(server=server)
    with self.assertRaises(ServerError):
      manager.before_request()
    self.assertEqual(manager.active_requests, 0)

  def test_close_stops_the_owned_server(self):
    manager, server, _clock = self.make()
    manager.before_request()
    manager.after_request()
    manager.close()
    self.assertEqual(server.stopped, 1)


class LifecycleClientTests(unittest.TestCase):
  def test_chat_notifies_lifecycle_around_request(self):
    calls = []

    class Lifecycle:
      def before_request(self):
        calls.append("before")

      def after_request(self):
        calls.append("after")

    client = LLMClient(
        LlamaConfig(),
        LlmConfig(max_retries=0),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=VALID_PAYLOAD)
        ),
        lifecycle=Lifecycle(),
    )
    try:
      result = client.chat([{"role": "user", "content": "capital?"}])
    finally:
      client.close()
    self.assertEqual(result.content, "Paris.")
    self.assertEqual(calls, ["before", "after"])

  def test_start_failure_is_mapped_to_connection_error(self):
    class Lifecycle:
      def before_request(self):
        raise ServerError("model failed")

      def after_request(self):
        raise AssertionError("after_request must not run")

    client = LLMClient(
        LlamaConfig(),
        LlmConfig(max_retries=0),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=VALID_PAYLOAD)
        ),
        lifecycle=Lifecycle(),
    )
    try:
      with self.assertRaises(LLMConnectionError):
        client.chat([{"role": "user", "content": "hello"}])
    finally:
      client.close()


class ModelConfigTests(unittest.TestCase):
  def test_project_defaults_enable_lazy_loading(self):
    llama = load_settings().llama
    self.assertTrue(llama.lazy_start)
    self.assertEqual(llama.idle_unload_s, 180.0)

  def test_idle_unload_can_be_disabled(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "settings.toml"
      path.write_text(
          "[llama]\nlazy_start = false\nidle_unload_s = 0\n",
          encoding="utf-8",
      )
      llama = load_settings(path).llama
      self.assertFalse(llama.lazy_start)
      self.assertEqual(llama.idle_unload_s, 0)

  def test_invalid_lifecycle_settings_are_rejected(self):
    for body, fragment in (
        ("[llama]\nlazy_start = 1\n", "lazy_start"),
        ("[llama]\nidle_unload_s = 2\n", "idle_unload_s"),
        ("[llama]\nidle_unload_s = 90000\n", "idle_unload_s"),
    ):
      with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.toml"
        path.write_text(body, encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
          load_settings(path)
        self.assertIn(fragment, str(caught.exception))


if __name__ == "__main__":
  unittest.main()
