"""Automatic weekly refresh.

Once a first week has been written, the background loop keeps the calendar
textured without user input: a few days before the generated week runs out,
it regenerates a fresh week from the confirmed spine and writes it through
the same reconciliation as everything else. Standing commitments already on
the calendar are skipped as duplicates; only new one-off "deltas" land.

The confirmed spine stays the human-reviewed source of truth — auto-refresh
never re-extracts or edits it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional

from . import llm
from .models import Spine
from .reconcile import Decision, reconcile
from .scanner import _default_writer, work_lock
from .store import Store
from .timeutil import local_now, zone

# Regenerate this many days after the last week start, so the +7d window the
# kin sees never empties out before the next batch arrives.
REFRESH_AFTER_DAYS = 5


@dataclass
class RefreshResult:
    ran: bool = False
    written: int = 0
    skipped: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return f"refresh error: {self.error}"
        if not self.ran:
            return "refresh: not due"
        parts = [f"refresh: {self.written} new event(s) written"]
        parts.extend(self.skipped)
        return ", ".join(parts)


def refresh_due(state: dict, today: date) -> bool:
    """Due once a first week exists and it's REFRESH_AFTER_DAYS old."""
    last = state.get("last_week_start")
    if not last:
        return False  # nothing written yet — the user starts the first week
    return today >= date.fromisoformat(last) + timedelta(days=REFRESH_AFTER_DAYS)


def auto_refresh(
    store: Store,
    writer_factory: Optional[Callable] = None,
    now: Optional[datetime] = None,
) -> RefreshResult:
    """Generate and write the next week. Caller checks `refresh_due` first."""
    if not work_lock.acquire(blocking=False):
        return RefreshResult(error="another scan or refresh is already running")
    try:
        return _refresh_locked(store, writer_factory, now)
    finally:
        work_lock.release()


def _refresh_locked(
    store: Store,
    writer_factory: Optional[Callable],
    now: Optional[datetime],
) -> RefreshResult:
    result = RefreshResult()
    tz = store.timezone()
    now = now or local_now(tz)

    raw_spine = store.read_text(store.spine_path)
    if raw_spine is None:
        result.error = "no confirmed spine to generate from"
        return result
    if not store.state().get("calendar_id") and writer_factory is None:
        result.error = "no target calendar designated"
        return result

    week_start = now.date()
    spine = Spine.model_validate_json(raw_spine)

    try:
        generation = llm.generate_events(spine, week_start.isoformat())
    except Exception as e:  # noqa: BLE001
        result.error = f"generation failed: {e}"
        return result

    try:
        writer = writer_factory() if writer_factory else _default_writer(store)
        tzinfo = zone(tz)
        window_start = (now - timedelta(hours=24)).replace(tzinfo=tzinfo)
        window_end = (now + timedelta(days=8)).replace(tzinfo=tzinfo)
        existing = writer.list_existing(window_start, window_end)

        report = reconcile(generation.events, existing, week_start)
        for verdict in report.verdicts:
            if verdict.decision == Decision.ADD:
                writer.write_event(verdict.event, week_start, provenance="generated")
                result.written += 1
            else:
                result.skipped.append(
                    f"{verdict.decision.value}: {verdict.event.title!r}"
                )
    except Exception as e:  # noqa: BLE001
        result.error = f"calendar write failed: {e}"
        return result

    store.update_state(last_week_start=week_start.isoformat())
    result.ran = True
    return result
