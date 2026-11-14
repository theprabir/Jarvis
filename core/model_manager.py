"""On-demand lifecycle for the local llama-server.

The wake-word listener and deterministic commands do not need the LLM. Keeping
``llama-server`` loaded for the entire session costs about 1.1 GB, which is too
much on an 8 GB machine. This manager starts it only for a model request and
stops it after a configurable idle period.

The manager never stops a server it did not start itself (for example, one
launched manually or reused on the configured port).
"""

from __future__ import annotations

import logging
import threading
import time

from core.server_manager import LlamaServer

log = logging.getLogger(__name__)

MONITOR_INTERVAL_S = 5.0


class ModelManager:
  """Own the optional llama-server process and unload it when idle."""

  def __init__(
      self,
      settings,
      server=None,
      clock=time.monotonic,
      start_monitor=True,
  ):
    self._settings = settings
    self._config = settings.llama
    self._server = server or LlamaServer(
        settings.llama, settings.app.log_dir
    )
    self._clock = clock
    self._lock = threading.RLock()
    self._load_lock = threading.Lock()
    self._active = 0
    self._last_used = clock()
    self._stop = threading.Event()
    self._thread = None
    self._owned = False
    self._warned_external = False

    if not self._config.lazy_start:
      self.ensure_loaded()

    if start_monitor and self._config.idle_unload_s > 0:
      self._thread = threading.Thread(
          target=self._monitor,
          name="jarvis-model-idle",
          daemon=True,
      )
      self._thread.start()

  @property
  def active_requests(self):
    with self._lock:
      return self._active

  @property
  def is_loaded(self):
    return self._server.is_healthy()

  def ensure_loaded(self):
    """Start the server if necessary and return True when it is ready."""
    with self._load_lock:
      if self._server.is_healthy():
        with self._lock:
          self._last_used = self._clock()
        return False
      log.info("Starting the local language model on demand")
      started = self._server.ensure_running()
      with self._lock:
        self._owned = bool(started)
        self._last_used = self._clock()
      if started:
        log.info(
            "Language model ready for the next request (%.1fs startup)",
            getattr(self._server, "startup_seconds", 0.0),
        )
      else:
        log.info(
            "Using an existing language model at %s; it will not be auto-unloaded",
            self._config.base_url,
        )
      return True

  def before_request(self):
    """Mark a model request active and ensure the server is available."""
    with self._lock:
      self._active += 1
    try:
      self.ensure_loaded()
    except Exception:
      with self._lock:
        self._active = max(0, self._active - 1)
        self._last_used = self._clock()
      raise
    with self._lock:
      self._last_used = self._clock()

  def after_request(self):
    """Mark a model request complete and restart the idle clock."""
    with self._lock:
      self._active = max(0, self._active - 1)
      self._last_used = self._clock()

  def _check_idle(self):
    """Unload one idle, manager-owned server; return True if stopped."""
    idle_seconds = float(self._config.idle_unload_s)
    if idle_seconds <= 0:
      return False
    with self._lock:
      if self._active or not self._owned:
        return False
      idle_for = self._clock() - self._last_used
      if idle_for < idle_seconds:
        return False
    with self._load_lock:
      with self._lock:
        # A request may have arrived while we waited for the load lock.
        if self._active or not self._owned:
          return False
        if getattr(self._server, "pid", None) is None:
          return False
        self._owned = False
      log.info(
          "Unloading the language model after %.0fs idle", idle_for
      )
      self._server.stop()
      with self._lock:
        self._last_used = self._clock()
      return True

  def _monitor(self):
    while not self._stop.wait(MONITOR_INTERVAL_S):
      try:
        self._check_idle()
      except Exception:
        log.exception("Language-model idle monitor failed")

  def close(self):
    """Stop the monitor and the managed server process."""
    self._stop.set()
    if self._thread is not None and self._thread.is_alive():
      self._thread.join(timeout=2.0)
    with self._load_lock:
      self._server.stop()
    with self._lock:
      self._owned = False
