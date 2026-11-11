"""Deterministic daily and meeting summaries from local data."""

from __future__ import annotations

from datetime import date

from core.calendar import format_agenda
from core.mail import summarize_text


class SummaryError(Exception):
    """Raised when a daily or meeting summary cannot be built."""


def parse_meeting_input(text):
    """Split ``TITLE | NOTES``; plain text becomes the meeting notes."""
    body = " ".join(str(text or "").split()).strip()
    if not body:
        return "Meeting", ""
    if "|" in body:
        title, notes = (part.strip() for part in body.split("|", 1))
        return title or "Meeting", notes
    return "Meeting", body


def format_meeting_summary(title, notes, max_sentences=3):
    """Build a meeting summary without a model or network call."""
    body = str(notes or "").strip()
    if not body:
        raise SummaryError("a meeting summary needs some notes")
    summary = summarize_text(body, max_sentences=max_sentences)
    return f"Meeting summary — {title or 'Meeting'}:\n{summary}"


class SummaryService:
    """Combine existing local services into read-only summaries."""

    def __init__(
        self,
        productivity_provider=None,
        calendar_provider=None,
        time_provider=None,
        habits_provider=None,
    ):
        self._productivity_provider = productivity_provider
        self._calendar_provider = calendar_provider
        self._time_provider = time_provider
        self._habits_provider = habits_provider

    def daily(self, day=""):
        resolved = _day_value(day)
        lines = [f"Daily summary for {resolved.strftime('%A %d %B %Y')}:"]
        productivity = _resolve(self._productivity_provider)
        if productivity is not None:
            try:
                plan = productivity.daily_plan(resolved)
                day_plan = plan.days[0] if plan.days else None
                items = day_plan.items if day_plan else ()
                lines.append(
                    f"- To-dos: {len(plan.overdue)} overdue,"
                    f" {len(items)} scheduled,"
                    f" {len(plan.unscheduled)} unscheduled."
                )
                for item in items[:5]:
                    lines.append(f"  [{item.id}] {item.title}")
            except Exception as exc:
                lines.append(f"- To-dos unavailable: {exc}.")

        calendar = _resolve(self._calendar_provider)
        if calendar is not None:
            try:
                agenda = calendar.agenda(resolved)
                lines.append(f"- Calendar: {len(agenda.events)} event(s).")
                if agenda.events:
                    lines.append(format_agenda(agenda))
            except Exception as exc:
                lines.append(f"- Calendar unavailable: {exc}.")

        tracked = _resolve(self._time_provider)
        if tracked is not None:
            try:
                report = tracked.report(resolved)
                minutes = report.total_seconds // 60
                lines.append(f"- Tracked time: {minutes} minute(s).")
            except Exception as exc:
                lines.append(f"- Time tracking unavailable: {exc}.")

        habits = _resolve(self._habits_provider)
        if habits is not None:
            try:
                summaries = habits.list(resolved)
                done = sum(1 for item in summaries if item.done_today)
                lines.append(
                    f"- Habits: {done}/{len(summaries)} checked in today."
                )
            except Exception as exc:
                lines.append(f"- Habits unavailable: {exc}.")

        return "\n".join(lines)

    def meeting(self, text):
        title, notes = parse_meeting_input(text)
        return format_meeting_summary(title, notes)


def _resolve(provider):
    if callable(provider):
        try:
            return provider()
        except Exception:
            return None
    return provider


def _day_value(value):
    if isinstance(value, date):
        return value
    from core.productivity import parse_when

    if not str(value or "").strip():
        return date.today()
    parsed = parse_when(str(value), prefer_future=False)
    if parsed is None:
        raise SummaryError("I couldn't understand that summary date")
    return parsed.date()
