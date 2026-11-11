"""Pomodoro work and break phases backed by the existing timer service.

The active phase, its due time, completed work count and work length are stored
in a single SQLite row. Closing JARVIS leaves that row intact, so the next
Pomodoro request resumes the remaining phase instead of starting over.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from core.timers import TimerError

SCHEMA = """
CREATE TABLE IF NOT EXISTS pomodoro_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  phase TEXT NOT NULL,
  due_at TEXT NOT NULL,
  completed_work INTEGER NOT NULL,
  work_minutes INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class PomodoroError(Exception):
    """Raised when a Pomodoro session cannot be started or changed."""


@dataclass(frozen=True)
class PomodoroStatus:
    """Current Pomodoro state."""

    running: bool
    phase: str
    remaining_s: int
    completed_work: int
    work_minutes: int
    recovered: bool = False
    recovery_details: str = ""

    @property
    def phase_label(self):
        return self.phase.replace("_", " ")


class PomodoroStore:
    """Single-row SQLite persistence for the active Pomodoro session."""

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
            raise PomodoroError(f"Could not open {self.path}: {exc}") from exc

    def load(self):
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT phase, due_at, completed_work, work_minutes"
                    " FROM pomodoro_state WHERE id = 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise PomodoroError(f"Could not read Pomodoro state: {exc}") from exc
        if not row:
            return None
        try:
            due_at = datetime.fromisoformat(row[1])
        except (TypeError, ValueError):
            self.clear()
            return None
        return row[0], due_at, int(row[2]), int(row[3])

    def save(self, phase, due_at, completed_work, work_minutes):
        try:
            with self._lock:
                self._db.execute("DELETE FROM pomodoro_state")
                self._db.execute(
                    "INSERT INTO pomodoro_state"
                    " (id, phase, due_at, completed_work, work_minutes,"
                    " updated_at) VALUES (1, ?, ?, ?, ?, ?)",
                    (
                        phase,
                        due_at.isoformat(timespec="seconds"),
                        int(completed_work),
                        int(work_minutes),
                        datetime.now().replace(microsecond=0).isoformat(
                            timespec="seconds"
                        ),
                    ),
                )
                self._db.commit()
        except sqlite3.Error as exc:
            raise PomodoroError(f"Could not save Pomodoro state: {exc}") from exc

    def clear(self):
        try:
            with self._lock:
                self._db.execute("DELETE FROM pomodoro_state")
                self._db.commit()
        except sqlite3.Error as exc:
            raise PomodoroError(f"Could not clear Pomodoro state: {exc}") from exc

    def close(self):
        try:
            with self._lock:
                self._db.close()
        except sqlite3.Error:
            pass


class PomodoroService:
    """Drive work, break and long-break timers on one TimerService."""

    _PHASES = {"work", "break", "long_break"}

    def __init__(self, timers, config):
        self._timers = timers
        self._config = config
        self._running = False
        self._phase = ""
        self._timer_id = None
        self._completed = 0
        self._work_minutes = config.work_minutes
        self._recovered = False
        self._recovery_details = ""
        self._store = PomodoroStore(config.resolved_db)
        self._restore()

    def start(self, work_minutes=None):
        if self._running:
            raise PomodoroError("a Pomodoro session is already running")
        minutes = self._work_minutes if work_minutes is None else int(work_minutes)
        if not 1 <= minutes <= 180:
            raise PomodoroError("work minutes must be between 1 and 180")
        self._work_minutes = minutes
        self._completed = 0
        self._recovered = False
        self._recovery_details = ""
        self._schedule("work")
        return self.status()

    def stop(self):
        status = self.status()
        if self._timer_id is not None:
            self._timers.cancel(self._timer_id)
        self._running = False
        self._phase = ""
        self._timer_id = None
        self._recovered = False
        self._recovery_details = ""
        self._store.clear()
        return status

    def skip(self):
        if not self._running:
            raise PomodoroError("no Pomodoro session is running")
        self._timers.cancel(self._timer_id)
        next_phase = "break" if self._phase == "work" else "work"
        self._schedule(next_phase)
        return self.status()

    def status(self):
        if not self._running or self._timer_id is None:
            return PomodoroStatus(False, "", 0, self._completed, self._work_minutes)
        entry = self._timers.get(self._timer_id)
        if entry is None:
            self._running = False
            self._phase = ""
            self._timer_id = None
            return PomodoroStatus(False, "", 0, self._completed, self._work_minutes)
        return PomodoroStatus(
            True,
            self._phase,
            entry.remaining_s,
            self._completed,
            self._work_minutes,
            self._recovered,
            self._recovery_details,
        )

    def close(self):
        """Stop the in-memory timer but leave the session recoverable."""
        if self._timer_id is not None:
            self._timers.cancel(self._timer_id)
        self._running = False
        self._phase = ""
        self._timer_id = None
        self._store.close()

    def _restore(self):
        state = self._store.load()
        if state is None:
            return
        phase, due_at, completed, work_minutes = state
        if phase not in self._PHASES or work_minutes < 1:
            self._store.clear()
            return
        self._phase = phase
        self._completed = max(0, completed)
        self._work_minutes = work_minutes
        now = datetime.now()
        saved_remaining = max(0, int((due_at - now).total_seconds()))
        self._recovered = True
        self._recovery_details = (
            f"Resumed from saved state: {phase.replace('_', ' ')} with"
            f" {saved_remaining // 60}m {saved_remaining % 60}s remaining"
            f" (saved due {due_at.strftime('%Y-%m-%d %H:%M')})."
        )
        loops = 0
        while due_at <= now and loops < 1000:
            if self._session_limit_reached(completed):
                self._finish()
                return
            phase, due_at, completed = self._next(phase, due_at, completed)
            loops += 1
        if due_at <= now:
            # A very long absence should not loop forever; begin a fresh work
            # phase but preserve the completed-work count where possible.
            phase = "work"
            due_at = now + timedelta(minutes=self._work_minutes)
            completed = self._completed
        self._completed = completed
        seconds = max(1, int((due_at - now).total_seconds()))
        self._schedule(phase, seconds=seconds, due_at=due_at)

    def _schedule(self, phase, seconds=None, due_at=None):
        minutes = {
            "work": self._work_minutes,
            "break": self._config.break_minutes,
            "long_break": self._config.long_break_minutes,
        }[phase]
        if seconds is None:
            seconds = minutes * 60
        if due_at is None:
            due_at = datetime.now() + timedelta(seconds=seconds)
        label = f"Pomodoro {phase.replace('_', ' ')}"
        try:
            entry = self._timers.add_timer(
                seconds,
                label,
                on_done=lambda _entry: self._advance(phase),
            )
        except TimerError as exc:
            raise PomodoroError(f"could not start the {phase} timer: {exc}") from exc
        try:
            self._store.save(phase, due_at, self._completed, self._work_minutes)
        except PomodoroError:
            self._timers.cancel(entry.id)
            raise
        self._phase = phase
        self._timer_id = entry.id
        self._running = True

    def _advance(self, phase):
        if not self._running or self._phase != phase:
            return
        if self._session_limit_reached(self._completed):
            self._finish()
            return
        next_phase, due_at, completed = self._next(
            phase,
            datetime.now(),
            self._completed,
        )
        self._completed = completed
        self._schedule(next_phase, due_at=due_at)

    def _session_limit_reached(self, completed):
        limit = int(self._config.end_after_cycles)
        return limit > 0 and completed >= limit

    def _finish(self):
        """End a bounded session after its final break completes."""
        self._running = False
        self._phase = ""
        self._timer_id = None
        self._recovered = False
        self._recovery_details = ""
        self._store.clear()

    def _next(self, phase, due_at, completed):
        if phase == "work":
            completed += 1
            every = max(1, int(self._config.cycles_before_long_break))
            next_phase = (
                "long_break" if completed % every == 0 else "break"
            )
        else:
            next_phase = "work"
        minutes = {
            "work": self._work_minutes,
            "break": self._config.break_minutes,
            "long_break": self._config.long_break_minutes,
        }[next_phase]
        return next_phase, due_at + timedelta(minutes=minutes), completed
