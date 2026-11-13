"""Reliability, telemetry and self-monitoring.

Feature set 4.20 keeps bounded local records of requests, tool outcomes,
automatic errors, modes, configuration snapshots and reversible actions. It
uses the shared SQLite database and never sends telemetry anywhere.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
PERMISSIONS = {"safe", "confirm"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS reliability_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  intent TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ok',
  latency_ms INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  misunderstood INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reliability_requests_created
  ON reliability_requests (created_at);
CREATE TABLE IF NOT EXISTS reliability_tool_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  name TEXT NOT NULL,
  ok INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reliability_tools_name
  ON reliability_tool_events (name);
CREATE TABLE IF NOT EXISTS reliability_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  context TEXT NOT NULL,
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reliability_modes (
  name TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reliability_config_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  digest TEXT NOT NULL,
  content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reliability_undo (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  description TEXT NOT NULL,
  handler TEXT NOT NULL,
  payload TEXT NOT NULL,
  undone INTEGER NOT NULL DEFAULT 0
);
"""


class ReliabilityError(Exception):
  """A reliability record, mode or action could not be handled."""


def _now():
  return datetime.now().isoformat(timespec="seconds")


def _short(value, limit=300):
  text = " ".join(str(value or "").split())
  if len(text) <= limit:
    return text
  return text[: max(0, limit - 3)].rstrip() + "..."


def _resolve(provider):
  if provider is None:
    return None
  if callable(provider):
    try:
      return provider()
    except Exception as exc:
      log.info("Reliability provider unavailable: %s", exc)
      return None
  return provider


@dataclass(frozen=True)
class PluginSpec:
  name: str
  description: str
  permission: str
  source: str
  handler: object


class PluginRegistry:
  """Explicit, bounded in-process plugin slots; no dynamic imports."""

  def __init__(self, max_plugins=32):
    self.max_plugins = max(1, int(max_plugins))
    self._plugins = {}

  def register(
      self, name, handler, description="", permission="safe", source="local"
  ):
    name = str(name or "").strip().lower()
    if not PLUGIN_NAME.match(name):
      raise ReliabilityError(
          "a plugin name must be 3-41 lowercase letters, digits or underscores"
      )
    if name in self._plugins:
      raise ReliabilityError(f"plugin {name} is already registered")
    if len(self._plugins) >= self.max_plugins:
      raise ReliabilityError("the plugin limit is reached")
    if not callable(handler):
      raise ReliabilityError("a plugin handler must be callable")
    if permission not in PERMISSIONS:
      raise ReliabilityError("plugin permission must be safe or confirm")
    source = str(source or "local").strip().lower()
    if source not in {"local", "builtin"}:
      raise ReliabilityError("only local or builtin plugins can be registered")
    plugin = PluginSpec(name, _short(description, 200), permission, source, handler)
    self._plugins[name] = plugin
    return plugin

  def get(self, name):
    return self._plugins.get(str(name or "").strip().lower())

  def list(self):
    return [self._plugins[name] for name in sorted(self._plugins)]


class ReliabilityStore:
  """Bounded SQLite telemetry, modes, snapshots and undo records."""

  def __init__(self, path, max_requests=2000, max_tool_events=2000,
               max_errors=1000, max_config_versions=20, max_undo=50):
    self.path = Path(path)
    self.max_requests = max(1, int(max_requests))
    self.max_tool_events = max(1, int(max_tool_events))
    self.max_errors = max(1, int(max_errors))
    self.max_config_versions = max(1, int(max_config_versions))
    self.max_undo = max(1, int(max_undo))
    self._lock = threading.RLock()
    self._db = None
    try:
      self.path.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(
          str(self.path), check_same_thread=False, timeout=10.0
      )
      self._db.row_factory = sqlite3.Row
      self._db.executescript(SCHEMA)
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise ReliabilityError(
          f"could not open reliability database {self.path}: {exc}"
      ) from exc

  def close(self):
    if self._db is not None:
      with self._lock:
        self._db.close()
      self._db = None

  def _require(self):
    if self._db is None:
      raise ReliabilityError("the reliability database is closed")
    return self._db

  def _prune(self, table, limit):
    self._db.execute(
        f"DELETE FROM {table} WHERE id NOT IN"
        f" (SELECT id FROM {table} ORDER BY id DESC LIMIT ?)",
        (max(1, int(limit)),),
    )

  def record_request(
      self, text, intent="", role="", status="ok", latency_ms=0,
      error="", misunderstood=False,
  ):
    with self._lock:
      self._db.execute(
          "INSERT INTO reliability_requests"
          " (created_at, text, intent, role, status, latency_ms, error,"
          " misunderstood) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          (
              _now(),
              _short(text, 500),
              str(intent or ""),
              str(role or ""),
              str(status or "ok"),
              max(0, int(latency_ms)),
              _short(error, 500),
              1 if misunderstood else 0,
          ),
      )
      self._prune("reliability_requests", self.max_requests)
      self._db.commit()

  def record_tool(self, name, ok, duration_ms=0, error=""):
    with self._lock:
      self._db.execute(
          "INSERT INTO reliability_tool_events"
          " (created_at, name, ok, duration_ms, error)"
          " VALUES (?, ?, ?, ?, ?)",
          (
              _now(),
              str(name or ""),
              1 if ok else 0,
              max(0, int(duration_ms)),
              _short(error, 500),
          ),
      )
      self._prune("reliability_tool_events", self.max_tool_events)
      self._db.commit()

  def record_error(self, context, message):
    with self._lock:
      self._db.execute(
          "INSERT INTO reliability_errors (created_at, context, message)"
          " VALUES (?, ?, ?)",
          (_now(), _short(context, 100), _short(message, 1000)),
      )
      self._prune("reliability_errors", self.max_errors)
      self._db.commit()

  def request_stats(self):
    row = self._db.execute(
        "SELECT COUNT(*), SUM(status = 'ok'), AVG(latency_ms),"
        " MAX(latency_ms), SUM(misunderstood) FROM reliability_requests"
    ).fetchone()
    total = int(row[0] or 0)
    success = int(row[1] or 0)
    return {
        "total": total,
        "success": success,
        "failed": max(0, total - success),
        "average_latency_ms": round(float(row[2] or 0), 1),
        "max_latency_ms": int(row[3] or 0),
        "misunderstood": int(row[4] or 0),
    }

  def recent_requests(self, limit=20):
    rows = self._db.execute(
        "SELECT created_at, text, intent, role, status, latency_ms, error,"
        " misunderstood FROM reliability_requests ORDER BY id DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [dict(row) for row in rows]

  def tool_stats(self):
    rows = self._db.execute(
        "SELECT name, COUNT(*), SUM(ok), AVG(duration_ms),"
        " MAX(duration_ms) FROM reliability_tool_events"
        " GROUP BY name ORDER BY COUNT(*) DESC, name"
    ).fetchall()
    result = []
    for row in rows:
      total = int(row[1] or 0)
      success = int(row[2] or 0)
      result.append(
          {
              "name": row[0],
              "total": total,
              "success": success,
              "failed": max(0, total - success),
              "success_rate": round(success / total * 100, 1) if total else 0.0,
              "average_latency_ms": round(float(row[3] or 0), 1),
              "max_latency_ms": int(row[4] or 0),
          }
      )
    return result

  def recent_errors(self, limit=10):
    rows = self._db.execute(
        "SELECT created_at, context, message FROM reliability_errors"
        " ORDER BY id DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [dict(row) for row in rows]

  def misunderstood_commands(self, limit=10):
    rows = self._db.execute(
        "SELECT text, COUNT(*) AS count FROM reliability_requests"
        " WHERE misunderstood = 1 GROUP BY text"
        " ORDER BY count DESC, text LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [dict(row) for row in rows]

  def get_mode(self, name):
    row = self._db.execute(
        "SELECT value FROM reliability_modes WHERE name = ?", (name,)
    ).fetchone()
    return row[0] if row else None

  def set_mode(self, name, value):
    with self._lock:
      self._db.execute(
          "INSERT INTO reliability_modes (name, value, updated_at)"
          " VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET"
          " value = excluded.value, updated_at = excluded.updated_at",
          (str(name), "1" if value else "0", _now()),
      )
      self._db.commit()

  def add_config_version(self, label, digest, content):
    with self._lock:
      cursor = self._db.execute(
          "INSERT INTO reliability_config_versions"
          " (created_at, label, digest, content) VALUES (?, ?, ?, ?)",
          (_now(), _short(label, 200), digest, content),
      )
      self._prune("reliability_config_versions", self.max_config_versions)
      self._db.commit()
    return int(cursor.lastrowid)

  def config_versions(self, limit=20):
    rows = self._db.execute(
        "SELECT id, created_at, label, digest, length(content)"
        " FROM reliability_config_versions ORDER BY id DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [dict(row) for row in rows]

  def config_version(self, identifier):
    row = self._db.execute(
        "SELECT id, created_at, label, digest, content"
        " FROM reliability_config_versions WHERE id = ?",
        (int(identifier),),
    ).fetchone()
    return dict(row) if row else None

  def add_undo(self, description, handler, payload):
    with self._lock:
      cursor = self._db.execute(
          "INSERT INTO reliability_undo"
          " (created_at, description, handler, payload, undone)"
          " VALUES (?, ?, ?, ?, 0)",
          (_now(), _short(description, 300), handler, json.dumps(payload)),
      )
      self._prune("reliability_undo", self.max_undo)
      self._db.commit()
    return int(cursor.lastrowid)

  def last_undo(self):
    row = self._db.execute(
        "SELECT id, created_at, description, handler, payload"
        " FROM reliability_undo WHERE undone = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None

  def mark_undo(self, identifier):
    with self._lock:
      self._db.execute(
          "UPDATE reliability_undo SET undone = 1 WHERE id = ?",
          (int(identifier),),
      )
      self._db.commit()


class ReliabilityService:
  """Bounded telemetry, modes, capabilities and reversible actions."""

  def __init__(
      self,
      config,
      tools_config=None,
      task_provider=None,
      capabilities=None,
  ):
    self.config = config
    self._tools_config = tools_config
    self._task_provider = task_provider
    self._capabilities = capabilities
    self._store = ReliabilityStore(
        config.resolved_db,
        config.max_requests,
        config.max_tool_events,
        config.max_errors,
        config.max_config_versions,
        config.max_undo,
    )
    self._plugins = PluginRegistry(config.max_plugins)

  def close(self):
    self._store.close()

  # -- telemetry ---------------------------------------------------------

  def record_request(
      self, text, intent="", role="", status="ok", latency_ms=0,
      error="", misunderstood=False,
  ):
    self._store.record_request(
        text, intent, role, status, latency_ms, error, misunderstood
    )

  def record_tool(self, name, ok, duration_ms=0, error=""):
    self._store.record_tool(name, ok, duration_ms, error)

  def record_error(self, context, message):
    self._store.record_error(context, message)

  def recent_requests(self, limit=20):
    return self._store.recent_requests(limit)

  def stats(self):
    requests = self._store.request_stats()
    tools = self._store.tool_stats()
    errors = self._store.recent_errors(5)
    return {
        "requests": requests,
        "tools": tools,
        "tool_failures": sum(item["failed"] for item in tools),
        "errors": errors,
        "misunderstood": self._store.misunderstood_commands(5),
        "memory": self.memory_snapshot(),
        "tasks": self.task_summary(),
    }

  def memory_snapshot(self):
    try:
      import psutil

      process = psutil.Process(os.getpid())
      memory = process.memory_info()
      system = psutil.virtual_memory()
      return {
          "rss_mb": round(memory.rss / (1024 ** 2), 1),
          "vms_mb": round(memory.vms / (1024 ** 2), 1),
          "system_percent": round(float(system.percent), 1),
      }
    except Exception as exc:
      log.debug("Memory snapshot unavailable: %s", exc)
      return {}

  def task_summary(self):
    service = _resolve(self._task_provider)
    if service is None:
      return {}
    try:
      tasks = service.list(limit=100)
    except Exception:
      return {}
    statuses = {}
    completed_steps = 0
    total_steps = 0
    for task in tasks:
      status = str(getattr(task, "status", "unknown"))
      statuses[status] = statuses.get(status, 0) + 1
      steps = list(getattr(task, "steps", ()) or ())
      total_steps += len(steps)
      completed_steps += sum(
          1 for step in steps
          if str(getattr(step, "status", "")) == "completed"
      )
    return {
        "total": len(tasks),
        "statuses": statuses,
        "completed_steps": completed_steps,
        "total_steps": total_steps,
        "step_rate": (
            round(completed_steps / total_steps * 100, 1)
            if total_steps else 0.0
        ),
    }

  def task_history(self, limit=10):
    service = _resolve(self._task_provider)
    if service is None:
      return []
    try:
      rows = service.events(limit=limit)
    except Exception:
      return []
    result = []
    for row in rows or []:
      if isinstance(row, dict):
        result.append(row)
      else:
        result.append(
            {
                "created_at": getattr(row, "created_at", ""),
                "task_id": getattr(row, "task_id", ""),
                "kind": getattr(row, "kind", ""),
                "detail": getattr(row, "detail", ""),
            }
        )
    return result

  # -- modes -------------------------------------------------------------

  def mode(self, name):
    value = self._store.get_mode(name)
    if value is None:
      value = "1" if bool(getattr(self.config, name, False)) else "0"
      self._store.set_mode(name, value == "1")
    return value == "1"

  def set_mode(self, name, enabled):
    if name not in {"offline", "privacy", "explain"}:
      raise ReliabilityError("unknown reliability mode")
    self._store.set_mode(name, bool(enabled))
    return bool(enabled)

  def modes(self):
    return {
        "offline": self.mode("offline"),
        "privacy": self.mode("privacy"),
        "explain": self.mode("explain"),
    }

  # -- capabilities and plugins -----------------------------------------

  def capabilities(self):
    provider = self._capabilities
    if callable(provider):
      try:
        return provider()
      except Exception as exc:
        log.info("Capability provider unavailable: %s", exc)
        return []
    return list(provider or [])

  def register_plugin(self, name, handler, description="", permission="safe"):
    return self._plugins.register(name, handler, description, permission)

  def plugins(self):
    return self._plugins.list()

  # -- configuration snapshots ------------------------------------------

  def ensure_baseline(self):
    """Keep a first configuration snapshot without duplicating it."""
    if self._store.config_versions(1):
      return None
    try:
      return self.snapshot_config(label="baseline")
    except ReliabilityError as exc:
      log.info("Configuration baseline unavailable: %s", exc)
      return None

  def snapshot_config(self, label=""):
    path = self.config.resolved_config_path
    try:
      content = path.read_text(encoding="utf-8")
    except OSError as exc:
      raise ReliabilityError(f"could not read {path}: {exc}") from exc
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identifier = self._store.add_config_version(label, digest, content)
    return {
        "id": identifier,
        "created_at": _now(),
        "label": label,
        "digest": digest,
    }

  def config_versions(self, limit=20):
    return self._store.config_versions(limit)

  def rollback_config(self, identifier, confirm=False):
    if not confirm:
      raise ReliabilityError("configuration rollback needs explicit confirmation")
    version = self._store.config_version(identifier)
    if version is None:
      raise ReliabilityError("there is no configuration version with that id")
    path = self.config.resolved_config_path
    try:
      parsed = tomllib.loads(version["content"])
      from core.config import load_settings

      load_settings(path)
    except Exception as exc:
      raise ReliabilityError(f"that configuration version is invalid: {exc}") from exc
    if not isinstance(parsed, dict):
      raise ReliabilityError("that configuration version is not a TOML table")
    try:
      self.snapshot_config(label="before rollback")
      path.parent.mkdir(parents=True, exist_ok=True)
      handle = tempfile.NamedTemporaryFile(
          "w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp"
      )
      try:
        handle.write(version["content"])
        handle.flush()
        os.fsync(handle.fileno())
      finally:
        handle.close()
      os.replace(handle.name, path)
    except (OSError, ReliabilityError) as exc:
      raise ReliabilityError(f"could not restore the configuration: {exc}") from exc
    return version

  # -- reversible actions -----------------------------------------------

  def record_undo(self, name, arguments, result):
    """Record a safe inverse for common successful file actions."""
    name = str(name or "")
    data = getattr(result, "data", None) or {}
    arguments = arguments or {}
    if name == "create_folder":
      path = data.get("path")
      if path:
        return self._store.add_undo(
            f"remove folder {path}",
            "remove_folder",
            {"path": path},
        )
    if name == "rename_path":
      source = arguments.get("source")
      target = arguments.get("target")
      if source and target:
        return self._store.add_undo(
            f"rename {target} back to {source}",
            "rename_back",
            {"source": str(source), "target": str(target)},
        )
    if name == "copy_path":
      target = arguments.get("target")
      if target:
        return self._store.add_undo(
            f"remove copied path {target}",
            "remove_copy",
            {"path": str(target)},
        )
    if name == "move_path":
      source = arguments.get("source")
      target = arguments.get("target")
      if source and target:
        return self._store.add_undo(
            f"move {target} back to {source}",
            "move_back",
            {"source": str(source), "target": str(target)},
        )
    return None

  def undo_last(self):
    record = self._store.last_undo()
    if record is None:
      raise ReliabilityError("there is no reversible action to undo")
    payload = json.loads(record["payload"] or "{}")
    self._undo(record["handler"], payload)
    self._store.mark_undo(record["id"])
    return record["description"]

  def _safe_path(self, value):
    root = None
    if self._tools_config is not None:
      try:
        root = Path(self._tools_config.resolved_root).resolve()
      except Exception:
        root = None
    path = Path(str(value)).expanduser()
    if root is not None and not path.is_absolute():
      path = root / path
    path = path.resolve()
    if root is not None and path != root and root not in path.parents:
      raise ReliabilityError("the undo path is outside the tool sandbox")
    return path

  def _undo(self, handler, payload):
    try:
      if handler == "remove_folder":
        path = self._safe_path(payload["path"])
        if not path.exists():
          raise ReliabilityError("the folder no longer exists")
        path.rmdir()
      elif handler in {"rename_back", "move_back"}:
        source = self._safe_path(payload["source"])
        target = self._safe_path(payload["target"])
        if not target.exists():
          raise ReliabilityError("the current path no longer exists")
        if source.exists():
          raise ReliabilityError("the original path already exists")
        shutil.move(str(target), str(source))
      elif handler == "remove_copy":
        path = self._safe_path(payload["path"])
        if not path.exists():
          raise ReliabilityError("the copied path no longer exists")
        if path.is_dir():
          shutil.rmtree(path)
        else:
          path.unlink()
      else:
        raise ReliabilityError("unknown undo action")
    except (OSError, KeyError, ValueError) as exc:
      raise ReliabilityError(f"could not undo the action: {exc}") from exc
