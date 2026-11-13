"""Durable multi-step task planning and execution.

A task is a small JSON document persisted in SQLite: a goal, ordered steps,
dependencies, optional conditions, retries and verification. Steps run through
the existing validated tool registry, never around it. The engine checkpoints
after every step, supports task- and step-level approval, background execution,
cancellation, resumption, dynamic replanning and an execution history.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"
CANCELLED = "cancelled"
AWAITING_APPROVAL = "awaiting_approval"
AWAITING_STEP = "awaiting_step"
PAUSED = "paused"

TERMINAL = {COMPLETED, FAILED, CANCELLED}
SATISFIED = {COMPLETED}

KNOWN_FOLDERS = {
    "downloads": "Downloads",
    "desktop": "Desktop",
    "documents": "Documents",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}

CATEGORY_EXTENSIONS = {
    "Documents": {
        ".csv",
        ".doc",
        ".docx",
        ".epub",
        ".md",
        ".odt",
        ".pdf",
        ".ppt",
        ".pptx",
        ".rtf",
        ".txt",
        ".xls",
        ".xlsx",
    },
    "Images": {
        ".bmp",
        ".gif",
        ".heic",
        ".jpeg",
        ".jpg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    },
    "Audio": {
        ".aac",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".wav",
        ".wma",
    },
    "Video": {
        ".avi",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".webm",
        ".wmv",
    },
    "Archives": {
        ".7z",
        ".bz2",
        ".gz",
        ".rar",
        ".tar",
        ".tgz",
        ".xz",
        ".zip",
    },
    "Code": {
        ".c",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".html",
        ".java",
        ".js",
        ".json",
        ".php",
        ".ps1",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".xml",
        ".yaml",
        ".yml",
    },
    "Installers": {".exe", ".msi", ".msix"},
}
CATEGORY_ORDER = tuple(CATEGORY_EXTENSIONS) + ("Other",)


class TaskError(Exception):
  """Raised when a task cannot be planned, loaded or started."""


def _now():
  return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _join(base, name):
  return str(base).rstrip("/\\") + "/" + str(name).lstrip("/\\")


def _basename(path):
  return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _slug(text):
  return "".join(
      character.lower() if character.isalnum() else "_"
      for character in str(text)
  ).strip("_") or "step"


def _category_for(name):
  extension = os.path.splitext(str(name))[1].lower()
  for category in CATEGORY_ORDER:
    if extension in CATEGORY_EXTENSIONS.get(category, set()):
      return category
  return "Other"


def _unique_name(name, taken):
  """Return a case-insensitively unique file name in one category."""
  stem, extension = os.path.splitext(name)
  candidate = name
  number = 2
  while candidate.casefold() in taken:
    candidate = f"{stem} ({number}){extension}"
    number += 1
  taken.add(candidate.casefold())
  return candidate


def _collision_target(target, task_id, attempt):
  """A deterministic alternate name for a failed move or copy."""
  normalized = target.replace("\\", "/")
  if "/" in normalized:
    directory, name = normalized.rsplit("/", 1)
  else:
    directory, name = "", normalized
  stem, extension = os.path.splitext(name)
  suffix = f"task {task_id}" if attempt <= 1 else f"task {task_id}-{attempt}"
  candidate = f"{stem} ({suffix}){extension}"
  return f"{directory}/{candidate}" if directory else candidate


@dataclass
class TaskStep:
  """One executable step with its dependencies and verification rule."""

  key: str
  name: str
  tool: str
  arguments: dict = field(default_factory=dict)
  depends_on: tuple = ()
  condition: dict | None = None
  retries: int | None = None
  parallel: bool = False
  approval: bool = False
  verify: dict | None = None
  status: str = PENDING
  attempts: int = 0
  replans: int = 0
  text: str = ""
  result: dict = field(default_factory=dict)
  error: str = ""
  started_at: str = ""
  finished_at: str = ""

  @classmethod
  def from_dict(cls, data):
    values = {
        item.name: data[item.name]
        for item in fields(cls)
        if item.name in data
    }
    values["depends_on"] = tuple(values.get("depends_on") or ())
    values["arguments"] = dict(values.get("arguments") or {})
    values["result"] = dict(values.get("result") or {})
    return cls(**values)


@dataclass
class Task:
  """A persisted goal, its plan and its current checkpoint."""

  id: int
  goal: str
  kind: str
  status: str
  steps: list[TaskStep]
  context: dict = field(default_factory=dict)
  background: bool = False
  approval_required: bool = True
  approved: bool = False
  current_step: str = ""
  error: str = ""
  created_at: str = ""
  updated_at: str = ""
  started_at: str = ""
  finished_at: str = ""

  def step(self, key):
    for item in self.steps:
      if item.key == key:
        return item
    return None

  def done_count(self):
    return sum(1 for step in self.steps if step.status == COMPLETED)

  def verified_count(self):
    return sum(
        1 for step in self.steps
        if step.verify and step.status == COMPLETED
    )

  @classmethod
  def from_dict(cls, data):
    values = {
        item.name: data[item.name]
        for item in fields(cls)
        if item.name in data
    }
    values["steps"] = [
        TaskStep.from_dict(step) if isinstance(step, dict) else step
        for step in values.get("steps") or []
    ]
    values["context"] = dict(values.get("context") or {})
    return cls(**values)


TASK_COLUMNS = (
    "id, goal, kind, status, steps, context, background,"
    " approval_required, approved, current_step, error, created_at,"
    " updated_at, started_at, finished_at"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  steps TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '{}',
  background INTEGER NOT NULL DEFAULT 0,
  approval_required INTEGER NOT NULL DEFAULT 1,
  approved INTEGER NOT NULL DEFAULT 0,
  current_step TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task
  ON task_events (task_id, id);
"""


class TaskStore:
  """SQLite-backed task checkpoints and execution events."""

  def __init__(self, path):
    self.path = Path(path)
    self._lock = threading.RLock()
    try:
      self.path.parent.mkdir(parents=True, exist_ok=True)
      self._db = sqlite3.connect(
          str(self.path), check_same_thread=False, timeout=10.0
      )
      self._db.executescript(SCHEMA)
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise TaskError(f"Could not open {self.path}: {exc}") from exc
    self.recover_interrupted()

  def create(
      self,
      goal,
      kind,
      steps,
      context=None,
      background=False,
      approval_required=True,
  ):
    now = _now()
    status = AWAITING_APPROVAL if approval_required else PENDING
    payload = json.dumps([asdict(step) for step in steps])
    try:
      with self._lock:
        cursor = self._db.execute(
            "INSERT INTO tasks"
            " (goal, kind, status, steps, context, background,"
            " approval_required, approved, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                str(goal),
                str(kind),
                status,
                payload,
                json.dumps(dict(context or {})),
                1 if background else 0,
                1 if approval_required else 0,
                now,
                now,
            ),
        )
        self._db.commit()
        task_id = int(cursor.lastrowid)
    except sqlite3.Error as exc:
      raise TaskError(f"Could not create the task: {exc}") from exc
    self.add_event(task_id, "created", f"Task #{task_id} planned")
    task = self.get(task_id)
    if task is None:
      raise TaskError("The task disappeared after it was created")
    return task

  def save(self, task):
    task.updated_at = _now()
    payload = json.dumps([asdict(step) for step in task.steps])
    try:
      with self._lock:
        self._db.execute(
            "UPDATE tasks SET goal = ?, kind = ?, status = ?, steps = ?,"
            " context = ?, background = ?, approval_required = ?,"
            " approved = ?, current_step = ?, error = ?, created_at = ?,"
            " updated_at = ?, started_at = ?, finished_at = ?"
            " WHERE id = ?",
            (
                task.goal,
                task.kind,
                task.status,
                payload,
                json.dumps(task.context),
                1 if task.background else 0,
                1 if task.approval_required else 0,
                1 if task.approved else 0,
                task.current_step,
                task.error,
                task.created_at,
                task.updated_at,
                task.started_at,
                task.finished_at,
                task.id,
            ),
        )
        self._db.commit()
    except sqlite3.Error as exc:
      raise TaskError(f"Could not checkpoint the task: {exc}") from exc
    return task

  def get(self, task_id):
    try:
      with self._lock:
        row = self._db.execute(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ?",
            (int(task_id),),
        ).fetchone()
    except (sqlite3.Error, ValueError):
      return None
    return self._task(row) if row else None

  def list(self, limit=10, status=None):
    query = f"SELECT {TASK_COLUMNS} FROM tasks"
    params = []
    if status:
      query += " WHERE status = ?"
      params.append(str(status))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
      with self._lock:
        rows = self._db.execute(query, params).fetchall()
    except sqlite3.Error as exc:
      raise TaskError(f"Could not list tasks: {exc}") from exc
    return [self._task(row) for row in rows]

  def events(self, limit=20, task_id=None):
    query = (
        "SELECT e.task_id, t.goal, e.kind, e.message, e.created_at"
        " FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id"
    )
    params = []
    if task_id is not None:
      query += " WHERE e.task_id = ?"
      params.append(int(task_id))
    query += " ORDER BY e.id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
      with self._lock:
        rows = self._db.execute(query, params).fetchall()
    except sqlite3.Error as exc:
      raise TaskError(f"Could not read task history: {exc}") from exc
    return [
        {
            "task_id": row[0],
            "goal": row[1] or "",
            "kind": row[2],
            "message": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]

  def add_event(self, task_id, kind, message):
    try:
      with self._lock:
        self._db.execute(
            "INSERT INTO task_events (task_id, kind, message, created_at)"
            " VALUES (?, ?, ?, ?)",
            (int(task_id), str(kind), str(message), _now()),
        )
        self._db.execute(
            "DELETE FROM task_events WHERE id NOT IN"
            " (SELECT id FROM task_events ORDER BY id DESC LIMIT 2000)"
        )
        self._db.commit()
    except sqlite3.Error as exc:
      log.warning("Could not record task event: %s", exc)

  def recover_interrupted(self):
    """Turn tasks left running by a crash into resumable checkpoints."""
    try:
      with self._lock:
        rows = self._db.execute(
            "SELECT id FROM tasks WHERE status = ?",
            (RUNNING,),
        ).fetchall()
        for (task_id,) in rows:
          task = self.get(task_id)
          if task is None:
            continue
          for step in task.steps:
            if step.status == RUNNING:
              step.status = PENDING
              step.error = ""
          task.status = PAUSED
          task.error = "Interrupted; run /tasks resume ID to continue."
          task.current_step = ""
          task.finished_at = ""
          self.save(task)
          self.add_event(
              task_id, "paused", "Task recovered from an interrupted run"
          )
    except sqlite3.Error as exc:
      log.warning("Could not recover interrupted tasks: %s", exc)

  def close(self):
    try:
      with self._lock:
        self._db.close()
    except sqlite3.Error:
      log.debug("Task store close failed", exc_info=True)

  @staticmethod
  def _task(row):
    keys = tuple(column.strip() for column in TASK_COLUMNS.split(","))
    data = dict(zip(keys, row))
    data["steps"] = json.loads(data["steps"] or "[]")
    data["context"] = json.loads(data["context"] or "{}")
    data["background"] = bool(data["background"])
    data["approval_required"] = bool(data["approval_required"])
    data["approved"] = bool(data["approved"])
    return Task.from_dict(data)


def plan_organize(registry, folder, max_steps=5000):
  """Inspect a folder and return a dependency-aware organization plan."""
  raw = " ".join(str(folder or "").split()).strip().strip('"').strip("'")
  if not raw:
    raw = "Downloads"
  inspect = registry.execute(
      "list_files", {"path": raw, "limit": max_steps}
  )
  if not inspect.ok:
    raise TaskError(inspect.text)
  base = str(inspect.data.get("path") or raw)
  items = list(inspect.data.get("items") or [])
  total = int(inspect.data.get("total") or len(items))
  if total > len(items):
    raise TaskError(
        f"That folder has {total} items and I can inspect at most"
        f" {len(items)}. Raise [tasks] max_steps or organize it in"
        " smaller batches."
    )
  files = [item for item in items if not str(item).endswith("/")]
  directories = {
      str(item).rstrip("/\\") for item in items if str(item).endswith("/")
  }

  buckets = {}
  for name in files:
    buckets.setdefault(_category_for(name), []).append(name)

  steps = [
      TaskStep(
          key="inspect",
          name="Inspect the folder",
          tool="list_files",
          arguments={"path": base},
          retries=1,
      )
  ]
  context = {
      "folder": base,
      "label": _basename(base) or raw,
      "files": len(files),
      "buckets": {name: len(rows) for name, rows in buckets.items()},
      "categories": {},
  }
  condition = {"step": "inspect", "field": "total", "op": ">", "value": 0}

  for category in CATEGORY_ORDER:
    names = buckets.get(category)
    if not names:
      continue
    cat_path = _join(base, category)
    mkdir_key = f"mkdir_{_slug(category)}"
    created = category not in directories
    if created:
      steps.append(
          TaskStep(
              key=mkdir_key,
              name=f"Create the {category} folder",
              tool="create_folder",
              arguments={"path": cat_path},
              depends_on=("inspect",),
              condition=condition,
              retries=1,
              parallel=True,
          )
      )
    taken = set()
    if not created:
      listing = registry.execute(
          "list_files", {"path": cat_path, "limit": max_steps}
      )
      if listing.ok:
        taken = {
            str(item).rstrip("/\\").casefold()
            for item in listing.data.get("items") or []
        }
    expected = []
    move_keys = []
    for index, name in enumerate(sorted(names, key=str.lower), 1):
      target_name = _unique_name(str(name), taken)
      key = f"move_{_slug(category)}_{index:03d}"
      steps.append(
          TaskStep(
              key=key,
              name=f"Move {name} to {category}",
              tool="move_path",
              arguments={
                  "source": _join(base, name),
                  "target": _join(cat_path, target_name),
              },
              depends_on=(mkdir_key if created else "inspect",),
              condition=condition,
              retries=1,
              parallel=True,
          )
      )
      expected.append(target_name)
      move_keys.append(key)
    steps.append(
        TaskStep(
            key=f"verify_{_slug(category)}",
            name=f"Verify the {category} folder",
            tool="list_files",
            arguments={"path": cat_path, "limit": max_steps},
            depends_on=tuple(move_keys),
            retries=1,
            parallel=True,
            verify={"contains": expected},
        )
    )
    context["categories"][category] = len(names)

  if len(steps) > max_steps:
    raise TaskError(
        f"That folder needs {len(steps)} steps; the limit is {max_steps}."
        " Raise [tasks] max_steps or organize it in smaller batches."
    )
  return steps, context


class TaskEngine:
  """Plan, persist and execute tasks without bypassing the tool registry."""

  def __init__(self, config, registry, notifier=None):
    if registry is None:
      raise TaskError("Computer tools are disabled")
    self._config = config
    self._registry = registry
    self._notifier = notifier
    self._store = TaskStore(config.resolved_db)
    self._lock = threading.RLock()
    self._events = {}
    self._threads = {}
    self._closed = False
    self._replanner: Callable | None = None
    self._resume_background()

  def set_notifier(self, callback):
    self._notifier = callback

  def set_replanner(self, callback):
    """Install an optional ``(task, step, error) -> arguments`` recovery hook."""
    self._replanner = callback

  def _resume_background(self):
    """Continue approved background plans after a restart."""
    if not self._config.auto_resume:
      return
    for task in self._store.list(limit=100):
      if task.status != PAUSED or not task.background:
        continue
      if task.approval_required and not task.approved:
        continue
      try:
        self.start(task.id)
      except TaskError as exc:
        log.warning("Could not auto-resume task %s: %s", task.id, exc)

  def create(
      self,
      goal,
      steps,
      kind="generic",
      context=None,
      background=False,
      approval_required=None,
  ):
    steps = list(steps)
    self._validate(steps)
    if approval_required is None:
      approval_required = self._config.approval_required
    return self._store.create(
        goal,
        kind,
        steps,
        context=context,
        background=background,
        approval_required=approval_required,
    )

  def create_file_plan(self, goal, actions, background=False):
    """Turn explicit file actions into a durable, sequential task."""
    steps = []
    previous = None
    for index, (name, arguments, clause) in enumerate(actions, 1):
      if self._registry.get(name) is None:
        raise TaskError(f"there is no tool named {name}")
      key = f"file_{index:02d}"
      steps.append(
          TaskStep(
              key=key,
              name=(clause or name).strip(),
              tool=name,
              arguments=dict(arguments),
              depends_on=(previous,) if previous else (),
              retries=1,
          )
      )
      previous = key
    return self.create(
        goal,
        steps,
        kind="file_plan",
        context={"actions": len(steps)},
        background=background,
    )

  def create_organize(self, folder, background=False):
    steps, context = plan_organize(
        self._registry, folder, self._config.max_steps
    )
    label = context["label"]
    goal = f"Organize {label}"
    if context["files"]:
      goal += f" ({context['files']} files)"
    return self.create(
        goal,
        steps,
        kind="organize",
        context=context,
        background=background,
    )

  def approve(self, task_id, background=None):
    task = self._require(task_id)
    if task.status in TERMINAL:
      return task
    if not task.approved and task.approval_required:
      task.approved = True
      task.status = PENDING
      self._store.save(task)
      self._store.add_event(task.id, "approved", "User approved the task")
    if background is None:
      background = task.background
    if background:
      return self.start(task.id)
    return self.run(task.id)

  def approve_step(self, task_id):
    task = self._require(task_id)
    if task.status != AWAITING_STEP:
      return task
    approved = task.context.setdefault("approved_steps", {})
    approved[task.current_step] = True
    task.status = PENDING
    self._store.save(task)
    self._store.add_event(
        task.id, "approved", f"Approved step {task.current_step}"
    )
    return self.start(task.id) if task.background else self.run(task.id)

  def resume(self, task_id):
    task = self._require(task_id)
    if task.status == AWAITING_APPROVAL:
      return task
    if task.status == AWAITING_STEP:
      return task
    for step in task.steps:
      if step.status != COMPLETED:
        step.status = PENDING
        step.error = ""
        step.attempts = 0
        step.replans = 0
        step.started_at = ""
        step.finished_at = ""
    task.status = PENDING
    task.error = ""
    task.finished_at = ""
    task.current_step = ""
    self._store.save(task)
    self._store.add_event(task.id, "resumed", "Task resumed")
    if task.background:
      return self.start(task.id)
    return self.run(task.id)

  def cancel(self, task_id):
    task = self._require(task_id)
    with self._lock:
      event = self._events.get(task.id)
      if event is not None:
        event.set()
    if task.status not in TERMINAL:
      task.status = CANCELLED
      task.error = "Cancelled by the user."
      task.finished_at = _now()
      task.current_step = ""
      self._store.save(task)
      self._store.add_event(task.id, "cancelled", "Task cancelled by the user")
    return task

  def run(self, task_id):
    """Run a task synchronously, returning its final checkpoint."""
    with self._lock:
      task = self._require(task_id)
      if task.status in TERMINAL:
        return task
      if task.approval_required and not task.approved:
        return task
      if task.status == AWAITING_STEP:
        return task
      if task_id in self._events:
        return task
      event = threading.Event()
      self._events[task_id] = event
    try:
      return self._run_task(task_id, event)
    finally:
      self._finish_runner(task_id, event)

  def start(self, task_id):
    """Run a task in a daemon thread and return its checkpoint immediately."""
    with self._lock:
      task = self._require(task_id)
      if task.status in TERMINAL:
        return task
      if task.approval_required and not task.approved:
        return task
      if task.status == AWAITING_STEP:
        return task
      if task_id in self._events:
        return task
      event = threading.Event()
      self._events[task_id] = event
      thread = threading.Thread(
          target=self._background,
          args=(task_id, event),
          name=f"jarvis-task-{task_id}",
          daemon=True,
      )
      self._threads[task_id] = thread
    thread.start()
    return self.get(task_id)

  def get(self, task_id):
    return self._store.get(task_id)

  def list(self, limit=10):
    return self._store.list(limit=limit)

  def events(self, task_id=None, limit=20):
    return self._store.events(limit=limit, task_id=task_id)

  def close(self):
    with self._lock:
      self._closed = True
      for event in self._events.values():
        event.set()
      threads = list(self._threads.values())
    for thread in threads:
      thread.join(timeout=2.0)
    self._store.close()

  # -- execution -------------------------------------------------------

  def _run_task(self, task_id, event):
    task = self._require(task_id)
    if task.status in TERMINAL or task.status == AWAITING_STEP:
      return task
    if task.approval_required and not task.approved:
      return task
    task.status = RUNNING
    task.started_at = task.started_at or _now()
    task.current_step = ""
    task.error = ""
    self._store.save(task)
    self._store.add_event(task.id, "started", f"Started task #{task.id}")

    while True:
      if event.is_set() or self._closed:
        return self._finish_cancelled(task)
      task = self.get(task.id) or task
      ready, changed = self._settle(task)
      if changed:
        self._store.save(task)
      if event.is_set() or self._closed:
        return self._finish_cancelled(task)
      if not ready:
        return self._finish_idle(task)

      approval = next(
          (step for step in ready if step.approval), None
      )
      if approval is not None:
        approved = task.context.get("approved_steps") or {}
        if not approved.get(approval.key):
          task.status = AWAITING_STEP
          task.current_step = approval.key
          self._store.save(task)
          self._store.add_event(
              task.id,
              "approval",
              f"Waiting for approval of step {approval.key}",
          )
          return task

      workers = max(1, int(self._config.parallel_workers))
      if (
          workers > 1
          and len(ready) > 1
          and all(step.parallel for step in ready)
      ):
        task = self._run_parallel(task, ready, event, workers)
        if task.status in TERMINAL:
          return task
        continue

      task = self._run_one(task, ready[0], event)
      if task.status in TERMINAL:
        return task

  def _settle(self, task):
    """Mark blocked/skipped steps and return the currently ready ones."""
    changed = False
    ready = []
    for step in task.steps:
      if step.status != PENDING:
        continue
      dependencies = [task.step(key) for key in step.depends_on]
      if any(
          dependency is None
          or dependency.status in {FAILED, CANCELLED}
          for dependency in dependencies
      ):
        step.status = SKIPPED
        step.error = "A dependency failed."
        changed = True
        continue
      if any(dependency.status == SKIPPED for dependency in dependencies):
        step.status = SKIPPED
        step.error = "A dependency was skipped."
        changed = True
        continue
      if not all(
          dependency.status in SATISFIED for dependency in dependencies
      ):
        continue
      if step.condition and not self._condition(task, step.condition):
        step.status = SKIPPED
        step.error = "Condition not met."
        changed = True
        continue
      ready.append(step)
    return ready, changed

  def _run_one(self, task, step, event):
    step.status = RUNNING
    step.started_at = _now()
    task.current_step = step.key
    self._store.save(task)
    self._store.add_event(
        task.id, "step", f"Started {step.key}: {step.name}"
    )
    outcome = self._execute_with_retries(task, step, event)
    if event.is_set() or self._closed or outcome.get("cancelled"):
      return self._finish_cancelled(task)
    if outcome["ok"]:
      self._complete_step(step, outcome)
      task.current_step = ""
      self._store.save(task)
      self._store.add_event(
          task.id, "done", f"Completed {step.key}: {outcome['text']}"
      )
      return task
    error = outcome["error"]
    diagnosis = self._diagnose_failure(step, error)
    self._store.add_event(
        task.id, "diagnosed", f"{step.key}: {diagnosis} ({error})"
    )
    recovered = self._recover_failure(step, error)
    if recovered:
      self._complete_step(
          step,
          {
              "text": recovered,
              "result": {},
              "attempts": outcome.get("attempts", 0),
          },
      )
      task.current_step = ""
      self._store.save(task)
      self._store.add_event(
          task.id, "recovered", f"Recovered {step.key}: {recovered}"
      )
      return task
    if self._replan_step(task, step, error, diagnosis):
      self._store.save(task)
      return task
    return self._fail_task(task, step, error)

  def _run_parallel(self, task, ready, event, workers):
    for step in ready:
      step.status = RUNNING
      step.started_at = _now()
    task.current_step = ", ".join(step.key for step in ready)
    self._store.save(task)
    self._store.add_event(
        task.id,
        "parallel",
        "Started " + ", ".join(step.key for step in ready),
    )
    outcomes = {}
    with ThreadPoolExecutor(
        max_workers=min(workers, len(ready))
    ) as pool:
      futures = {
          pool.submit(self._execute_with_retries, task, step, event): step
          for step in ready
      }
      for future, step in futures.items():
        try:
          outcomes[step.key] = future.result()
        except Exception as exc:  # a worker bug must not corrupt the task
          log.exception("Task worker failed for %s", step.key)
          outcomes[step.key] = {
              "ok": False,
              "text": "The step failed unexpectedly.",
              "result": {},
              "error": repr(exc),
              "attempts": 0,
              "cancelled": False,
          }
    if event.is_set() or self._closed:
      return self._finish_cancelled(task)
    failed = []
    for step in ready:
      outcome = outcomes[step.key]
      task.current_step = ""
      error = outcome["error"]
      diagnosis = self._diagnose_failure(step, error)
      if not outcome["ok"]:
        self._store.add_event(
            task.id, "diagnosed", f"{step.key}: {diagnosis} ({error})"
        )
      recovered = self._recover_failure(step, error)
      if outcome["ok"]:
        self._complete_step(step, outcome)
        self._store.add_event(
            task.id, "done", f"Completed {step.key}: {outcome['text']}"
        )
      elif recovered:
        self._complete_step(
            step,
            {
                "text": recovered,
                "result": {},
                "attempts": outcome.get("attempts", 0),
            },
        )
        self._store.add_event(
            task.id,
            "recovered",
            f"Recovered {step.key}: {recovered}",
        )
      elif self._replan_step(task, step, error, diagnosis):
        pass
      else:
        step.status = FAILED
        step.error = error
        step.finished_at = _now()
        failed.append((step, error))
    self._store.save(task)
    if failed:
      step, error = failed[0]
      return self._fail_task(task, step, error)
    return task

  def _execute_with_retries(self, task, step, event):
    retries = (
        self._config.max_retries
        if step.retries is None
        else max(0, int(step.retries))
    )
    attempts = 0
    last_error = ""
    while attempts <= retries:
      if event.is_set() or self._closed:
        return {
            "ok": False,
            "text": "Cancelled.",
            "result": {},
            "error": "Cancelled.",
            "attempts": attempts,
            "cancelled": True,
        }
      attempts += 1
      result = self._registry.execute(step.tool, step.arguments)
      if result.ok:
        verified, error = self._verify(step, result.data)
        if verified:
          return {
              "ok": True,
              "text": result.text,
              "result": result.data,
              "error": "",
              "attempts": attempts,
              "cancelled": False,
          }
        last_error = f"Verification failed: {error}"
      else:
        last_error = result.text
      if attempts <= retries:
        self._store.add_event(
            task.id,
            "retry",
            f"Retrying {step.key} after: {last_error}",
        )
        delay = max(0.0, float(self._config.retry_delay_s))
        if delay and event.wait(delay * attempts):
          break
    return {
        "ok": False,
        "text": last_error or "The step failed.",
        "result": {},
        "error": last_error or "The step failed.",
        "attempts": attempts,
        "cancelled": False,
    }

  def _verify(self, step, data):
    spec = step.verify
    if not spec:
      return True, ""
    values = dict(data or {})
    if spec.get("tool"):
      check = self._registry.execute(
          spec["tool"], dict(spec.get("arguments") or {})
      )
      if not check.ok:
        return False, check.text
      values = dict(check.data or {})
    contains = spec.get("contains")
    if contains:
      items = values.get("items")
      if not isinstance(items, list):
        return False, "the verification result has no item list"
      missing = [
          str(name) for name in contains if str(name) not in items
      ]
      if missing:
        return False, "missing " + ", ".join(missing)
    if "field" in spec:
      actual = self._field(values, spec["field"])
      expected = spec.get("value")
      if not self._compare(actual, spec.get("op", "=="), expected):
        return False, (
            f"{spec['field']} was {actual!r}, expected"
            f" {spec.get('op', '==')} {expected!r}"
        )
    return True, ""

  def _condition(self, task, spec):
    dependency = task.step(str(spec.get("step", "")))
    if dependency is None or dependency.status != COMPLETED:
      return False
    actual = self._field(dependency.result, spec.get("field", ""))
    return self._compare(actual, spec.get("op", "truthy"), spec.get("value"))

  @staticmethod
  def _field(data, path):
    value = data
    for part in str(path).split("."):
      if not isinstance(value, dict) or part not in value:
        return None
      value = value[part]
    return value

  @staticmethod
  def _compare(actual, operator, expected):
    operator = str(operator or "truthy")
    if operator == "truthy":
      return bool(actual)
    if operator == "falsy":
      return not actual
    if operator == "==":
      return actual == expected
    if operator == "!=":
      return actual != expected
    if operator == "contains":
      try:
        return expected in actual
      except TypeError:
        return False
    try:
      if operator == ">":
        return actual > expected
      if operator == ">=":
        return actual >= expected
      if operator == "<":
        return actual < expected
      if operator == "<=":
        return actual <= expected
    except TypeError:
      return False
    return False

  @staticmethod
  def _diagnose_failure(step, error):
    """Classify a failed step before choosing a recovery strategy."""
    text = str(error or "").lower()
    if "already exists" in text:
      return (
          "already_satisfied"
          if step.tool == "create_folder"
          else "name_collision"
      )
    if "outside the allowed folder" in text:
      return "sandbox_refusal"
    if "does not exist" in text or "not found" in text:
      return "missing_source"
    if "timed out" in text or "timeout" in text:
      return "timeout"
    if "access is denied" in text or "permission" in text:
      return "permission"
    if "verification failed" in text:
      return "verification_failed"
    return "tool_failure"

  @staticmethod
  def _recover_failure(step, error=""):
    """Recognize an effect that was already achieved by an earlier attempt."""
    text = str(error or "").lower()
    try:
      if step.tool == "create_folder":
        if "already exists" in text:
          return "Folder already existed."
        path = Path(str(step.arguments.get("path") or ""))
        if path.is_absolute() and path.is_dir():
          return "Folder already existed."
      if step.tool == "move_path":
        source = Path(str(step.arguments.get("source") or ""))
        target = Path(str(step.arguments.get("target") or ""))
        if (
            source.is_absolute()
            and target.is_absolute()
            and not source.exists()
            and target.exists()
        ):
          return "File was already moved."
    except OSError:
      return ""
    return ""

  def _complete_step(self, step, outcome):
    step.status = COMPLETED
    step.attempts += int(outcome.get("attempts") or 0)
    step.text = str(outcome.get("text") or "")
    step.result = dict(outcome.get("result") or {})
    step.error = ""
    step.finished_at = _now()

  def _replan_step(self, task, step, error, diagnosis=""):
    if step.replans >= 2:
      return False
    if self._replanner is not None:
      try:
        arguments = self._replanner(task, step, error)
      except Exception:
        log.warning("Task replanner failed", exc_info=True)
        arguments = None
      if isinstance(arguments, dict) and arguments != step.arguments:
        step.arguments = dict(arguments)
        step.replans += 1
        step.status = PENDING
        step.error = ""
        step.finished_at = ""
        self._store.add_event(
            task.id,
            "replanned",
            f"Replanned {step.key} after: {error}",
        )
        return True
    if diagnosis == "name_collision" and step.tool in {
        "move_path", "copy_path", "rename_path"
    }:
      old_target = str(step.arguments.get("target") or "")
      if not old_target:
        return False
      new_target = _collision_target(
          old_target, task.id, step.replans + 1
      )
      step.arguments = dict(step.arguments)
      step.arguments["target"] = new_target
      self._retarget_verification(task, old_target, new_target)
      step.replans += 1
      step.status = PENDING
      step.error = ""
      step.finished_at = ""
      self._store.add_event(
          task.id,
          "replanned",
          f"Replanned {step.key}: {old_target} -> {new_target}",
      )
      return True
    return False

  @staticmethod
  def _retarget_verification(task, old_target, new_target):
    old_name = _basename(old_target)
    new_name = _basename(new_target)
    for step in task.steps:
      if not step.verify:
        continue
      expected = step.verify.get("contains")
      if not isinstance(expected, list):
        continue
      step.verify = dict(step.verify)
      step.verify["contains"] = [
          new_name if name == old_name else name for name in expected
      ]

  def _finish_idle(self, task):
    pending = [step for step in task.steps if step.status == PENDING]
    if pending:
      keys = ", ".join(step.key for step in pending)
      return self._fail_task(
          task, pending[0], f"Blocked steps: {keys}"
      )
    failed = [step for step in task.steps if step.status == FAILED]
    if failed:
      step = failed[0]
      task.status = FAILED
      task.error = f"{step.name}: {step.error}"
      task.finished_at = _now()
      task.current_step = ""
      self._store.save(task)
      self._store.add_event(task.id, "failed", task.error)
      return task
    task.status = COMPLETED
    task.error = ""
    task.current_step = ""
    task.finished_at = _now()
    self._store.save(task)
    self._store.add_event(
        task.id,
        "completed",
        f"Task #{task.id} completed ({task.done_count()} steps)",
    )
    return task

  def _fail_task(self, task, step, error):
    step.status = FAILED
    step.error = str(error)
    step.finished_at = _now()
    task.status = FAILED
    task.error = f"{step.name or step.key}: {error}"
    task.current_step = ""
    task.finished_at = _now()
    self._store.save(task)
    self._store.add_event(task.id, "failed", task.error)
    return task

  def _finish_cancelled(self, task):
    current = self.get(task.id) or task
    if current.status != CANCELLED:
      current.status = CANCELLED
      current.error = "Cancelled."
      current.current_step = ""
      current.finished_at = _now()
      self._store.save(current)
      self._store.add_event(current.id, "cancelled", "Task cancelled")
    return current

  def _background(self, task_id, event):
    try:
      task = self._run_task(task_id, event)
      if self._notifier is not None and task.status in TERMINAL:
        self._notifier(
            f"Task {task.id} {task.status}: {task.goal}."
        )
    except Exception as exc:
      log.exception("Background task %s failed", task_id)
      try:
        task = self.get(task_id)
        if task is not None and task.status not in TERMINAL:
          task.status = FAILED
          task.error = f"Internal error: {exc}"
          task.current_step = ""
          task.finished_at = _now()
          self._store.save(task)
          self._store.add_event(
              task.id, "failed", "Internal task engine error"
          )
      except Exception:
        log.exception("Could not record background task failure")
    finally:
      self._finish_runner(task_id, event)

  def _finish_runner(self, task_id, event):
    with self._lock:
      if self._events.get(task_id) is event:
        self._events.pop(task_id, None)
      self._threads.pop(task_id, None)

  def _validate(self, steps):
    if not steps:
      raise TaskError("a task needs at least one step")
    if len(steps) > self._config.max_steps:
      raise TaskError(
          f"a task may have at most {self._config.max_steps} steps"
      )
    keys = [step.key for step in steps]
    if len(set(keys)) != len(keys):
      raise TaskError("task step keys must be unique")
    known = set(keys)
    for step in steps:
      missing = [key for key in step.depends_on if key not in known]
      if missing:
        raise TaskError(
            f"step {step.key} depends on unknown step(s):"
            f" {', '.join(missing)}"
        )
    visiting = set()
    visited = set()

    def visit(key):
      if key in visiting:
        raise TaskError(f"task dependency cycle at step {key}")
      if key in visited:
        return
      visiting.add(key)
      for dependency in next(
          step.depends_on for step in steps if step.key == key
      ):
        visit(dependency)
      visiting.remove(key)
      visited.add(key)

    for key in keys:
      visit(key)

  def _require(self, task_id):
    task = self.get(task_id)
    if task is None:
      raise TaskError("There is no task with that id.")
    return task
