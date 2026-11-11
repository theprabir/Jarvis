"""Pomodoro work and break phases backed by the existing timer service."""

from __future__ import annotations

from dataclasses import dataclass

from core.timers import TimerError


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

    @property
    def phase_label(self):
        return self.phase.replace("_", " ")


class PomodoroService:
    """Drive work, break and long-break timers on one TimerService."""

    def __init__(self, timers, config):
        self._timers = timers
        self._config = config
        self._running = False
        self._phase = ""
        self._timer_id = None
        self._completed = 0
        self._work_minutes = config.work_minutes

    def start(self, work_minutes=None):
        if self._running:
            raise PomodoroError("a Pomodoro session is already running")
        minutes = self._work_minutes if work_minutes is None else int(work_minutes)
        if not 1 <= minutes <= 180:
            raise PomodoroError("work minutes must be between 1 and 180")
        self._work_minutes = minutes
        self._completed = 0
        self._schedule("work")
        return self.status()

    def stop(self):
        status = self.status()
        if self._timer_id is not None:
            self._timers.cancel(self._timer_id)
        self._running = False
        self._phase = ""
        self._timer_id = None
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
        )

    def close(self):
        self.stop()

    def _schedule(self, phase):
        minutes = {
            "work": self._work_minutes,
            "break": self._config.break_minutes,
            "long_break": self._config.long_break_minutes,
        }[phase]
        label = f"Pomodoro {phase.replace('_', ' ')}"
        try:
            entry = self._timers.add_timer(
                minutes * 60,
                label,
                on_done=lambda _entry: self._advance(phase),
            )
        except TimerError as exc:
            raise PomodoroError(f"could not start the {phase} timer: {exc}") from exc
        self._phase = phase
        self._timer_id = entry.id
        self._running = True

    def _advance(self, phase):
        if not self._running or self._phase != phase:
            return
        if phase == "work":
            self._completed += 1
            every = max(1, int(self._config.cycles_before_long_break))
            next_phase = (
                "long_break" if self._completed % every == 0 else "break"
            )
        else:
            next_phase = "work"
        self._schedule(next_phase)
