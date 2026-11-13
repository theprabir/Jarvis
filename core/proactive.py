"""Deterministic briefings, reminders and proactive checks.

Feature set 4.19 composes existing local services into short, read-only
briefings and alerts. It never runs a model turn, starts a worker or performs
an action: the assistant asks for a check, or the check is composed at the
start of a session, and every result is derived from current local state.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS proactive_acknowledged (
  key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  acknowledged_at TEXT NOT NULL
);
"""


class ProactiveError(Exception):
  """A proactive briefing or check could not be composed."""


def _resolve(provider):
  """Resolve a service or a zero-argument provider, tolerating absence."""
  if provider is None:
    return None
  if callable(provider):
    try:
      return provider()
    except Exception as exc:
      log.info("Proactive provider unavailable: %s", exc)
      return None
  return provider


def _now(value=None):
  return value or datetime.now()


def _when(value):
  if isinstance(value, datetime):
    return value.strftime("%a %d %b %H:%M")
  if isinstance(value, date):
    return value.strftime("%a %d %b")
  return str(value or "")


def _short(value, limit=180):
  text = " ".join(str(value or "").split())
  if len(text) <= limit:
    return text
  return text[: max(0, limit - 3)].rstrip() + "..."


def _event_start(event):
  return getattr(event, "start_at", None)


def _event_end(event):
  end = getattr(event, "end_at", None)
  if end is not None:
    return end
  start = _event_start(event)
  if start is None:
    return None
  return start + timedelta(minutes=int(getattr(event, "duration_minutes", 0) or 0))


def _status(value):
  return str(value or "").lower()


def _alert_key(kind, text):
  raw = f"{kind}:{text}".encode("utf-8", errors="replace")
  return f"{kind}:{hashlib.sha1(raw).hexdigest()}"


class ProactiveState:
  """Persistent acknowledgement state for repeated proactive alerts."""

  def __init__(self, path):
    self.path = Path(path) if path != ":memory:" else None
    self._lock = threading.RLock()
    self._db = None
    try:
      if self.path is not None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
      target = str(self.path) if self.path is not None else ":memory:"
      self._db = sqlite3.connect(
          target, check_same_thread=False, timeout=10.0
      )
      self._db.executescript(SCHEMA)
      self._db.commit()
    except (OSError, sqlite3.Error) as exc:
      raise ProactiveError(
          f"could not open proactive state {self.path or ':memory:'}: {exc}"
      ) from exc

  def close(self):
    if self._db is not None:
      with self._lock:
        self._db.close()
      self._db = None

  def is_acknowledged(self, key):
    if self._db is None:
      return False
    with self._lock:
      row = self._db.execute(
          "SELECT 1 FROM proactive_acknowledged WHERE key = ?", (key,)
      ).fetchone()
    return row is not None

  def acknowledge(self, items):
    """Persist one or more (key, kind, text) alert records."""
    if self._db is None:
      return 0
    rows = list(items or ())
    if not rows:
      return 0
    now = datetime.now().isoformat(timespec="seconds")
    with self._lock:
      try:
        self._db.executemany(
            "INSERT OR IGNORE INTO proactive_acknowledged"
            " (key, kind, text, acknowledged_at) VALUES (?, ?, ?, ?)",
            [
                (key, kind, text, now)
                for key, kind, text in rows
            ],
        )
        self._db.commit()
      except sqlite3.Error as exc:
        raise ProactiveError(
            f"could not save proactive acknowledgement: {exc}"
        ) from exc
    return len(rows)

  def clear(self):
    if self._db is None:
      return 0
    with self._lock:
      try:
        cursor = self._db.execute("DELETE FROM proactive_acknowledged")
        self._db.commit()
        return int(cursor.rowcount or 0)
      except sqlite3.Error as exc:
        raise ProactiveError(
            f"could not clear proactive acknowledgements: {exc}"
        ) from exc

  def count(self):
    if self._db is None:
      return 0
    with self._lock:
      row = self._db.execute(
          "SELECT COUNT(*) FROM proactive_acknowledged"
      ).fetchone()
    return int(row[0]) if row else 0


class ProactiveService:
  """Build concise briefings and alerts from current local state."""

  def __init__(
      self,
      config,
      productivity=None,
      calendar=None,
      tasks=None,
      goals=None,
      research=None,
      knowledge=None,
      system=None,
      news=None,
      state=None,
  ):
    self._config = config
    self._state = state if state is not None else ProactiveState(":memory:")
    self._productivity = productivity
    self._calendar = calendar
    self._tasks = tasks
    self._goals = goals
    self._research = research
    self._knowledge = knowledge
    self._system = system
    self._news = news

  def close(self):
    if self._state is not None:
      self._state.close()
      self._state = None

  def _visible(self, kind, items, acknowledge=True):
    """Drop already acknowledged alerts and mark newly shown ones."""
    if not items or self._state is None:
      return list(items or ())
    if not acknowledge:
      return list(items)
    visible = []
    records = []
    for text in items:
      key = _alert_key(kind, text)
      if self._state.is_acknowledged(key):
        continue
      visible.append(text)
      records.append((key, kind, text))
    if acknowledge and records:
      self._state.acknowledge(records)
    return visible

  def acknowledge(self, now=None):
    """Acknowledge every current alert and return the new count."""
    now = _now(now)
    records = []
    for kind, items in (
        ("deadline", self._deadline_lines(now)),
        ("conflict", self._conflict_lines(now)),
        ("unfinished", self._unfinished_lines(now)),
        ("system", self._system_lines()),
    ):
      for text in items:
        records.append((_alert_key(kind, text), kind, text))
    before = self._state.count() if self._state is not None else 0
    self._state.acknowledge(records) if self._state is not None else None
    after = self._state.count() if self._state is not None else 0
    return max(0, after - before)

  def reset_acknowledgements(self):
    return self._state.clear() if self._state is not None else 0

  # -- public checks -----------------------------------------------------

  def morning(self, now=None):
    """Return the morning briefing with tasks, calendar and recommendations."""
    now = _now(now)
    lines = [
        f"Good morning, sir. {now.strftime('%A %d %B %Y, %H:%M')}.",
    ]
    self._append(lines, "Today's tasks", self._task_lines(now))
    self._append(lines, "Calendar today", self._calendar_lines(now))
    self._append(
        lines, "Deadlines", self._visible("deadline", self._deadline_lines(now))
    )
    self._append(
        lines,
        "Unfinished tasks",
        self._visible("unfinished", self._unfinished_lines(now)),
    )
    self._append(lines, "Research digest", self._research_lines())
    self._append(
        lines, "System alerts", self._visible("system", self._system_lines())
    )
    self._append(lines, "Suggested next actions", self._recommendation_lines(now))
    return "\n".join(lines)

  def daily(self, now=None):
    """Return a task-focused daily briefing without the morning greeting."""
    now = _now(now)
    lines = [f"Daily briefing for {now.strftime('%A %d %B %Y')}."]
    self._append(lines, "Today's tasks", self._task_lines(now))
    self._append(lines, "Calendar today", self._calendar_lines(now))
    self._append(
        lines, "Deadlines", self._visible("deadline", self._deadline_lines(now))
    )
    self._append(
        lines,
        "Unfinished tasks",
        self._visible("unfinished", self._unfinished_lines(now)),
    )
    self._append(
        lines, "System alerts", self._visible("system", self._system_lines())
    )
    self._append(lines, "Suggested next actions", self._recommendation_lines(now))
    return "\n".join(lines)

  def briefing(self, kind="", now=None):
    if _status(kind) in {"daily", "today", "day"}:
      return self.daily(now)
    return self.morning(now)

  def deadlines(self, now=None):
    raw = self._deadline_lines(_now(now))
    lines = self._visible("deadline", raw)
    if not lines:
      days = self._config.deadline_days
      if raw:
        return "All current deadlines have been acknowledged, sir."
      return f"No deadlines in the next {days} day(s), sir."
    return "Upcoming deadlines:\n" + "\n".join(lines)

  def unfinished(self, now=None):
    raw = self._unfinished_lines(_now(now))
    lines = self._visible("unfinished", raw)
    if not lines:
      if raw:
        return "All current unfinished-task alerts have been acknowledged, sir."
      return "There are no unfinished durable tasks, sir."
    return "Unfinished tasks:\n" + "\n".join(lines)

  def conflicts(self, now=None):
    raw = self._conflict_lines(_now(now))
    lines = self._visible("conflict", raw)
    if not lines:
      if raw:
        return "All current calendar-conflict alerts have been acknowledged, sir."
      return "No calendar conflicts were found, sir."
    return "Calendar conflicts:\n" + "\n".join(lines)

  def system_alerts(self, snapshot=None):
    raw = self._system_lines(snapshot)
    lines = self._visible("system", raw)
    if not lines:
      if raw:
        return "All current system alerts have been acknowledged, sir."
      return "System performance is within the configured limits, sir."
    return "System alerts:\n" + "\n".join(lines)

  def research_summary(self):
    lines = self._research_lines()
    if not lines:
      return "No recent research reports are saved, sir."
    return "Research digest:\n" + "\n".join(lines)

  def news(self, query=""):
    provider = self._news
    if provider is None:
      return "News search is unavailable, sir."
    try:
      if callable(provider):
        try:
          data = provider(query) if str(query or "").strip() else provider()
        except TypeError:
          data = provider()
      else:
        data = provider
    except Exception as exc:
      log.warning("News summary failed: %s", exc)
      return f"I couldn't fetch a news summary, sir: {exc}."
    if isinstance(data, str):
      return data.strip() or "No news items were found, sir."
    if isinstance(data, dict):
      data = data.get("items") or data.get("results") or []
    rows = list(data or [])
    if not rows:
      return "No news items were found, sir."
    lines = [f"News summary{' for ' + query if query else ''}:"]
    for item in rows[: self._config.news_limit]:
      if isinstance(item, dict):
        title = item.get("title") or item.get("name") or "News item"
        snippet = item.get("snippet") or item.get("text") or ""
        url = item.get("url") or ""
      else:
        title = getattr(item, "title", "News item")
        snippet = getattr(item, "snippet", "") or getattr(item, "text", "")
        url = getattr(item, "url", "")
      line = f"- {_short(title, 120)}"
      if snippet:
        line += f": {_short(snippet, 180)}"
      if url:
        line += f" ({url})"
      lines.append(line)
    return "\n".join(lines)

  def projects(self):
    lines = self._project_lines()
    if not lines:
      return "There are no active goals or durable tasks, sir."
    return "Project and goal progress:\n" + "\n".join(lines)

  def recommendations(self, now=None):
    lines = self._recommendation_lines(_now(now))
    if not lines:
      return "No recommendations right now, sir."
    return "Suggested next actions:\n" + "\n".join(
        f"- {line}" for line in lines
    )

  def alerts(self, now=None, include_all=False):
    now = _now(now)
    acknowledge = not include_all
    lines = []
    self._append(
        lines,
        "Deadlines",
        self._visible("deadline", self._deadline_lines(now), acknowledge),
    )
    self._append(
        lines,
        "Calendar conflicts",
        self._visible("conflict", self._conflict_lines(now), acknowledge),
    )
    self._append(
        lines,
        "Unfinished tasks",
        self._visible("unfinished", self._unfinished_lines(now), acknowledge),
    )
    self._append(
        lines,
        "System alerts",
        self._visible("system", self._system_lines(), acknowledge),
    )
    if len(lines) == 0:
      if include_all:
        return "No proactive alerts right now, sir."
      return "No new proactive alerts right now, sir."
    return "Proactive alerts:\n" + "\n".join(lines)

  # -- section helpers ---------------------------------------------------

  @staticmethod
  def _append(lines, title, items):
    if not items:
      return
    lines.extend(("", title + ":"))
    lines.extend(f"- {item}" for item in items)

  def _task_lines(self, now):
    service = _resolve(self._productivity)
    if service is None:
      return ["Productivity is unavailable."]
    try:
      plan = service.daily_plan(now.date())
    except Exception as exc:
      log.info("Daily plan unavailable for briefing: %s", exc)
      return ["Today's plan is unavailable."]
    days = getattr(plan, "days", ()) or ()
    items = list(days[0].items) if days else []
    overdue = list(getattr(plan, "overdue", ()) or ())
    unscheduled = list(getattr(plan, "unscheduled", ()) or ())
    if not items and not overdue and not unscheduled:
      return ["No open to-dos."]
    lines = [
        f"{len(overdue)} overdue, {len(items)} scheduled today,"
        f" {len(unscheduled)} unscheduled."
    ]
    seen = set()
    for item in (overdue + items + unscheduled):
      if item.id in seen:
        continue
      seen.add(item.id)
      due = getattr(item, "due_at", None)
      suffix = f" (due {_when(due)})" if due else " (unscheduled)"
      lines.append(f"[{item.id}] {item.title}{suffix}")
      if len(lines) > self._config.max_items:
        break
    return lines

  def _calendar_lines(self, now):
    service = _resolve(self._calendar)
    if service is None:
      return []
    try:
      agenda = service.agenda(now.date())
    except Exception as exc:
      log.info("Calendar unavailable for briefing: %s", exc)
      return ["Calendar is unavailable."]
    events = list(getattr(agenda, "events", ()) or ())
    if not events:
      return ["No events scheduled today."]
    return [
        f"{_event_start(event).strftime('%H:%M')}-"
        f"{_event_end(event).strftime('%H:%M')} {event.title}"
        for event in events[: self._config.max_items]
    ]

  def _deadline_lines(self, now):
    entries = []
    productivity = _resolve(self._productivity)
    todos = self._open_todos(productivity)
    horizon = now + timedelta(days=self._config.deadline_days)
    for item in todos:
      due = getattr(item, "due_at", None)
      if due is None:
        continue
      if due < now:
        entries.append(
            (due, f"Overdue: [{item.id}] {item.title} (due {_when(due)})")
        )
      elif due <= horizon:
        entries.append(
            (due, f"Due soon: [{item.id}] {item.title} (due {_when(due)})")
        )
    goals = _resolve(self._goals)
    if goals is not None:
      try:
        rows = goals.list(status=None)
      except Exception:
        rows = []
      end = now.date() + timedelta(days=self._config.deadline_days)
      for goal in rows:
        if _status(getattr(goal, "status", "")) not in {"active", ""}:
          continue
        target = getattr(goal, "target_date", None)
        if target is None:
          continue
        if target < now.date():
          entries.append(
              (datetime.combine(target, datetime.min.time()),
               f"Goal overdue: {goal.title} (target {_when(target)})")
          )
        elif target <= end:
          progress = getattr(goal, "progress_percent", None)
          suffix = f", {progress}%" if progress is not None else ""
          entries.append(
              (datetime.combine(target, datetime.min.time()),
               f"Goal due: {goal.title} (target {_when(target)}{suffix})")
          )
    entries.sort(key=lambda item: item[0])
    return [text for _when_value, text in entries[: self._config.max_items]]

  def _open_todos(self, service):
    if service is None:
      return []
    try:
      rows = service.list(status="open")
    except TypeError:
      try:
        rows = service.list()
      except Exception:
        rows = []
    except Exception:
      rows = []
    return [
        item for item in rows
        if _status(getattr(item, "status", "open")) not in {"done", "cancelled"}
    ]

  def _unfinished_lines(self, now):
    service = _resolve(self._tasks)
    if service is None:
      return []
    try:
      tasks = service.list(limit=max(10, self._config.max_items * 2))
    except Exception as exc:
      log.info("Task list unavailable for briefing: %s", exc)
      return []
    entries = []
    for task in tasks:
      status = _status(getattr(task, "status", ""))
      if status in {"completed", "cancelled"}:
        continue
      steps = list(getattr(task, "steps", ()) or ())
      done = (
          task.done_count()
          if callable(getattr(task, "done_count", None))
          else sum(1 for step in steps if _status(getattr(step, "status", "")) == "completed")
      )
      progress = f"{done}/{len(steps)} steps" if steps else "no steps"
      current = getattr(task, "current_step", "")
      suffix = f"; current: {current}" if current else ""
      entries.append(
          f"Task #{task.id}: {_short(task.goal, 140)}"
          f" ({status}; {progress}{suffix})"
      )
    return entries[: self._config.max_items]

  def _conflict_lines(self, now):
    service = _resolve(self._calendar)
    if service is None:
      return []
    events = []
    seen = set()
    try:
      agenda = service.agenda(now.date())
      events.extend(list(getattr(agenda, "events", ()) or ()))
    except Exception:
      pass
    try:
      upcoming = service.upcoming(now=now, limit=100)
    except TypeError:
      try:
        upcoming = service.upcoming()
      except Exception:
        upcoming = []
    except Exception:
      upcoming = []
    events.extend(upcoming)
    horizon = now + timedelta(days=self._config.conflict_days)
    selected = []
    for event in events:
      key = getattr(event, "id", None) or (
          getattr(event, "title", ""),
          _event_start(event),
      )
      if key in seen:
        continue
      seen.add(key)
      start = _event_start(event)
      end = _event_end(event)
      if start is None or end is None:
        continue
      if start < horizon and end > now:
        selected.append(event)
    selected.sort(key=_event_start)
    conflicts = []
    for index, first in enumerate(selected):
      for second in selected[index + 1:]:
        if _event_start(second) >= _event_end(first):
          break
        if _event_start(first) < _event_end(second):
          conflicts.append(
              f"{_when(_event_start(first))} {first.title} overlaps"
              f" {second.title} at {_event_start(second).strftime('%H:%M')}"
          )
    return conflicts[: self._config.max_items]

  def _system_lines(self, snapshot=None):
    if snapshot is None:
      provider = self._system
      if callable(provider):
        try:
          snapshot = provider()
        except Exception as exc:
          log.info("System snapshot unavailable for briefing: %s", exc)
          return []
      else:
        snapshot = provider
    if not isinstance(snapshot, dict):
      return []
    lines = []
    free = snapshot.get("disk_free_gb")
    percent = snapshot.get("disk_percent")
    if (
        (free is not None and float(free) <= self._config.low_disk_free_gb)
        or (percent is not None and float(percent) >= self._config.high_disk_percent)
    ):
      lines.append(
          f"Low disk space: {float(free or 0):.1f} GB free"
          f" ({float(percent or 0):.0f}% used)."
      )
    memory = snapshot.get("memory_percent")
    if memory is not None and float(memory) >= self._config.high_memory_percent:
      lines.append(f"High memory use: {float(memory):.0f}%.")
    cpu = snapshot.get("cpu_percent")
    if cpu is not None and float(cpu) >= self._config.high_cpu_percent:
      lines.append(f"High CPU use: {float(cpu):.0f}%.")
    return lines

  def _research_lines(self):
    service = _resolve(self._research)
    if service is None:
      return []
    try:
      rows = service.history(limit=self._config.research_limit)
    except Exception as exc:
      log.info("Research history unavailable for briefing: %s", exc)
      return []
    lines = []
    for row in rows:
      if isinstance(row, dict):
        title = row.get("title") or row.get("query") or "Research report"
        created = row.get("created") or ""
        report = row.get("report") or ""
        sources = len(row.get("sources") or ())
      else:
        title = getattr(row, "query", "Research report")
        created = getattr(row, "created", "")
        report = getattr(row, "report", "")
        sources = len(getattr(row, "sources", ()) or ())
      suffix = f"; {sources} source(s)" if sources else ""
      lines.append(
          f"[{created}] {_short(title, 100)}: {_short(report, 180)}{suffix}"
      )
    return lines[: self._config.research_limit]

  def _project_lines(self):
    lines = []
    goals = _resolve(self._goals)
    if goals is not None:
      try:
        rows = goals.list(status=None)
      except Exception:
        rows = []
      for goal in rows:
        if _status(getattr(goal, "status", "")) not in {"active", ""}:
          continue
        progress = getattr(goal, "progress_percent", None)
        progress_text = f"{progress}%" if progress is not None else "no numeric target"
        target = getattr(goal, "target_date", None)
        suffix = f"; target {_when(target)}" if target else ""
        lines.append(f"Goal: {goal.title} — {progress_text}{suffix}")
    tasks = _resolve(self._tasks)
    if tasks is not None:
      try:
        rows = tasks.list(limit=max(10, self._config.max_items * 2))
      except Exception:
        rows = []
      for task in rows:
        if _status(getattr(task, "status", "")) in {"completed", "cancelled"}:
          continue
        steps = list(getattr(task, "steps", ()) or ())
        done = (
            task.done_count()
            if callable(getattr(task, "done_count", None))
            else 0
        )
        progress = f"{done}/{len(steps)} steps" if steps else "no steps"
        lines.append(
            f"Task #{task.id}: {_short(task.goal, 120)}"
            f" — {progress} ({getattr(task, 'status', '')})"
        )
    return lines[: self._config.max_items]

  def _recommendation_lines(self, now):
    lines = []
    deadlines = self._deadline_lines(now)
    overdue = [line for line in deadlines if line.startswith("Overdue")]
    if overdue:
      lines.append(f"Clear {len(overdue)} overdue item(s), starting with the oldest.")
    elif deadlines:
      lines.append("Review the nearest deadline and reserve time for it.")
    conflicts = self._conflict_lines(now)
    if conflicts:
      lines.append("Resolve the calendar overlap before the next meeting.")
    tasks = self._unfinished_lines(now)
    if tasks:
      lines.append(f"Resume one of {len(tasks)} unfinished durable task(s).")
    knowledge = _resolve(self._knowledge)
    if knowledge is not None:
      try:
        cards = knowledge.due(10)
      except Exception:
        cards = []
      if cards:
        lines.append(f"Review {len(cards)} due flashcard(s) in a short study block.")
    for alert in self._system_lines():
      if alert.startswith("Low disk space"):
        lines.append("Free disk space before starting large downloads or reports.")
        break
    if now.hour < 12:
      lines.append("Start with the highest-priority item while the day is fresh.")
    elif now.hour < 17:
      lines.append("Finish the highest-priority open item before the day ends.")
    else:
      lines.append("Close out open work and set up tomorrow's first action.")
    return lines[:5]
