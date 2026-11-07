"""Lifecycle management for the local llama-server process.

Responsibilities:
- Reuse a healthy server that is already listening.
- Launch ``llama-server`` with the flags from configuration.
- Wait for real readiness (POST /health) instead of assuming success.
- Capture server output in logs/llama-server.log instead of discarding it.
- Stop only the process this object started; never kill an unknown server.
"""

from __future__ import annotations

import atexit
import logging
import subprocess
import time
from pathlib import Path

import httpx

from core.config import PROJECT_ROOT

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.5
TERMINATE_TIMEOUT_S = 15.0
LOG_TAIL_CHARS = 1500


class ServerError(RuntimeError):
  """The llama-server process could not be started or became unusable."""


class LlamaServer:
  """Owns at most one llama-server child process."""

  def __init__(self, config, log_dir="logs"):
    self._config = config
    self._process = None
    self._log_handle = None
    self._atexit_registered = False
    self._log_path = self._resolve_log_path(log_dir)
    self.startup_seconds = 0.0

  @staticmethod
  def _resolve_log_path(log_dir):
    directory = Path(log_dir)
    if not directory.is_absolute():
      directory = PROJECT_ROOT / directory
    return directory / "llama-server.log"

  @property
  def pid(self):
    return self._process.pid if self._process is not None else None

  @property
  def log_path(self):
    return self._log_path

  def command(self):
    """Build the llama-server argv from configuration."""
    config = self._config
    return [
        config.executable,
        "-m",
        str(config.resolved_model_path),
        "--host",
        config.host,
        "--port",
        str(config.port),
        "-c",
        str(config.context_size),
        "-t",
        str(config.threads),
        "-tb",
        str(config.threads_batch),
        "-b",
        str(config.batch_size),
        "-ub",
        str(config.ubatch_size),
        "-np",
        str(config.parallel_slots),
        "-ngl",
        str(config.gpu_layers),
    ]

  def is_healthy(self):
    try:
      response = httpx.get(f"{self._config.base_url}/health", timeout=1.5)
    except httpx.HTTPError:
      return False
    return response.status_code == 200

  def ensure_running(self):
    """Reuse or start the server; return True if a new process was started."""
    if self.is_healthy():
      log.info("Using existing llama-server at %s", self._config.base_url)
      self._warn_on_mismatch()
      return False

    if not self._config.auto_start:
      raise ServerError(
          f"No llama-server is listening at {self._config.base_url} and"
          " [llama] auto_start is disabled"
      )

    model_path = self._config.resolved_model_path
    if not model_path.is_file():
      raise ServerError(f"Model file not found: {model_path}")

    self._launch()
    self._wait_until_ready()
    return True

  def _launch(self):
    self._log_path.parent.mkdir(parents=True, exist_ok=True)
    self._log_handle = self._log_path.open("a", encoding="utf-8", errors="replace")
    command = self.command()
    log.info("Launching llama-server: %s", " ".join(command))
    creation_flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
      self._process = subprocess.Popen(
          command,
          cwd=str(PROJECT_ROOT),
          stdin=subprocess.DEVNULL,
          stdout=self._log_handle,
          stderr=subprocess.STDOUT,
          creationflags=creation_flags,
      )
    except FileNotFoundError as exc:
      self._close_log_handle()
      raise ServerError(
          f"Could not execute {self._config.executable!r}. Is llama-server on PATH?"
      ) from exc
    except OSError as exc:
      self._close_log_handle()
      raise ServerError(f"Could not start llama-server: {exc}") from exc

    if not self._atexit_registered:
      atexit.register(self.stop)
      self._atexit_registered = True

  def _wait_until_ready(self):
    deadline = time.monotonic() + self._config.startup_timeout_s
    started = time.monotonic()
    last_notice = 0.0
    while time.monotonic() < deadline:
      if self._process.poll() is not None:
        raise ServerError(
            f"llama-server exited during startup with code"
            f" {self._process.returncode}.\n{self._log_tail()}"
        )
      if self.is_healthy():
        self.startup_seconds = time.monotonic() - started
        log.info(
            "llama-server ready after %.1fs (pid %s, log %s)",
            self.startup_seconds,
            self._process.pid,
            self._log_path,
        )
        return
      elapsed = time.monotonic() - started
      if elapsed - last_notice >= 5.0:
        last_notice = elapsed
        log.info("Waiting for llama-server to load the model (%.0fs)", elapsed)
      time.sleep(POLL_INTERVAL_S)

    self.stop()
    raise ServerError(
        "llama-server did not become healthy within"
        f" {self._config.startup_timeout_s:.0f}s.\n{self._log_tail()}"
    )

  def _warn_on_mismatch(self):
    """Warn when a reused server was started with different settings."""
    try:
      response = httpx.get(f"{self._config.base_url}/props", timeout=3.0)
      response.raise_for_status()
      props = response.json()
    except (httpx.HTTPError, ValueError) as exc:
      log.debug("Could not read /props for verification: %s", exc)
      return

    actual_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
    actual_slots = props.get("total_slots")
    if actual_ctx is not None and int(actual_ctx) != self._config.context_size:
      log.warning(
          "Existing server uses n_ctx=%s but configuration says %s;"
          " restart it to apply settings",
          actual_ctx,
          self._config.context_size,
      )
    if actual_slots is not None and int(actual_slots) != self._config.parallel_slots:
      log.warning(
          "Existing server uses %s parallel slots but configuration says %s;"
          " each slot gets only 1/%s of the context",
          actual_slots,
          self._config.parallel_slots,
          actual_slots,
      )

  def _log_tail(self):
    try:
      text = self._log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
      return "Server log is unavailable."
    tail = text[-LOG_TAIL_CHARS:].strip()
    return f"Last server log lines:\n{tail}" if tail else "Server log is empty."

  def _close_log_handle(self):
    if self._log_handle is not None:
      try:
        self._log_handle.close()
      finally:
        self._log_handle = None

  def stop(self):
    """Stop the child process this object started, if it is still running."""
    process = self._process
    if process is None:
      self._close_log_handle()
      return
    if process.poll() is None:
      log.info("Stopping llama-server (pid %s)", process.pid)
      process.terminate()
      try:
        process.wait(timeout=TERMINATE_TIMEOUT_S)
      except subprocess.TimeoutExpired:
        log.warning("llama-server did not stop in time; killing it")
        process.kill()
        process.wait(timeout=TERMINATE_TIMEOUT_S)
    self._process = None
    self._close_log_handle()
