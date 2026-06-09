"""Reconciliation: check candidate events before anything is written.

Mechanical checks (time overlap, the 20-event budget, exact duplicates) are
plain code — LLMs are unreliable at time math. The semantic layer (reworded
duplicates, chat-canon contradictions) is a stub the phase-2 reactive poller
will fill in; the interface and provenance rules exist from day one so it
slots in cleanly.

Provenance resolution: chat-`mentioned` beats `generated`; newer beats older.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import List, Optional

from .models import WEEKDAY_ORDER, GeneratedEvent

# Kindroid surfaces at most 20 events in the -24h/+7d window and randomly
# samples beyond that, hiding events. Stay well under: hard-stop before the
# window total reaches HARD_CAP, and warn above the lean target.
HARD_CAP = 18
LEAN_TARGET_PER_DAY = 3


class Decision(str, Enum):
    ADD = "add"
    SKIP_DUPLICATE = "skip_duplicate"
    REJECT_OVERLAP = "reject_overlap"
    EVICT_BUDGET = "evict_budget"
    # Reserved for the phase-2 semantic layer:
    UPDATE = "update"
    REPLACE = "replace"
    REJECT_CANON = "reject_canon"


@dataclass
class ExistingEvent:
    """An event already on the calendar (any provenance).

    `created`/`updated` are Google's own timestamps (UTC), carried for the
    phase-2 "newer beats older" resolution; the MVP doesn't branch on them.
    """

    title: str
    start: datetime
    end: datetime
    source: str = "generated"  # "generated" | "mentioned"
    created: Optional[datetime] = None
    updated: Optional[datetime] = None


@dataclass
class Occurrence:
    event: GeneratedEvent
    start: datetime
    end: datetime


@dataclass
class Verdict:
    event: GeneratedEvent
    decision: Decision
    reason: str = ""


@dataclass
class ReconcileReport:
    verdicts: List[Verdict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def accepted(self) -> List[GeneratedEvent]:
        return [v.event for v in self.verdicts if v.decision == Decision.ADD]

    @property
    def rejected(self) -> List[Verdict]:
        return [v for v in self.verdicts if v.decision != Decision.ADD]


def _parse_time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def first_date_for_weekday(week_start: date, weekday: str) -> date:
    """First occurrence of `weekday` on or after week_start."""
    target = WEEKDAY_ORDER.index(weekday)
    delta = (target - week_start.weekday()) % 7
    return week_start + timedelta(days=delta)


def expand_occurrences(event: GeneratedEvent, week_start: date) -> List[Occurrence]:
    """Concrete (start, end) datetimes for an event within the 7 days from week_start.

    Overnight blocks (end <= start) end on the following day.
    """
    days: List[date] = []
    if event.type == "oneoff":
        days = [date.fromisoformat(event.date)]
    else:
        days = [first_date_for_weekday(week_start, wd) for wd in event.days_of_week or []]

    occurrences = []
    for d in days:
        start = datetime.combine(d, _parse_time(event.start))
        end = datetime.combine(d, _parse_time(event.end))
        if event.crosses_midnight:
            end += timedelta(days=1)
        occurrences.append(Occurrence(event=event, start=start, end=end))
    return occurrences


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _is_exact_duplicate(occ: Occurrence, existing: List[ExistingEvent]) -> Optional[ExistingEvent]:
    for ex in existing:
        if ex.title.strip().lower() == occ.event.title.strip().lower() and ex.start == occ.start:
            return ex
    return None


def semantic_review(candidate: GeneratedEvent, existing: List[ExistingEvent]) -> Optional[Verdict]:
    """Phase 2: LLM-backed checks — reworded duplicates, chat-canon
    contradictions, routine-implied events. Returns None (no objection) in the
    MVP; chat-`mentioned` events will outrank `generated` ones here."""
    return None


def reconcile(
    candidates: List[GeneratedEvent],
    existing: List[ExistingEvent],
    week_start: date,
) -> ReconcileReport:
    """Decide add / skip / reject / evict for each candidate, in order."""
    report = ReconcileReport()
    accepted_occurrences: List[Occurrence] = []
    window_count = len(existing)

    for event in candidates:
        occurrences = expand_occurrences(event, week_start)

        dup = next(
            (d for occ in occurrences if (d := _is_exact_duplicate(occ, existing))), None
        )
        if dup:
            report.verdicts.append(
                Verdict(event, Decision.SKIP_DUPLICATE, f"already on calendar: {dup.title!r}")
            )
            continue

        clash = next(
            (
                (occ, other)
                for occ in occurrences
                for other in accepted_occurrences
                if _overlaps(occ.start, occ.end, other.start, other.end)
            ),
            None,
        )
        if clash:
            occ, other = clash
            report.verdicts.append(
                Verdict(
                    event,
                    Decision.REJECT_OVERLAP,
                    f"overlaps {other.event.title!r} at {occ.start:%a %Y-%m-%d %H:%M}",
                )
            )
            continue

        ex_clash = next(
            (
                (occ, ex)
                for occ in occurrences
                for ex in existing
                if _overlaps(occ.start, occ.end, ex.start, ex.end)
            ),
            None,
        )
        if ex_clash:
            occ, ex = ex_clash
            report.verdicts.append(
                Verdict(
                    event,
                    Decision.REJECT_OVERLAP,
                    f"overlaps existing {ex.title!r} at {occ.start:%a %Y-%m-%d %H:%M}",
                )
            )
            continue

        objection = semantic_review(event, existing)
        if objection:
            report.verdicts.append(objection)
            continue

        if window_count + len(occurrences) > HARD_CAP:
            report.verdicts.append(
                Verdict(
                    event,
                    Decision.EVICT_BUDGET,
                    f"window budget full ({window_count}/{HARD_CAP} occurrences)",
                )
            )
            continue

        window_count += len(occurrences)
        accepted_occurrences.extend(occurrences)
        report.verdicts.append(Verdict(event, Decision.ADD))

    _density_warnings(report, accepted_occurrences)
    return report


def _density_warnings(report: ReconcileReport, occurrences: List[Occurrence]) -> None:
    per_day: dict = {}
    for occ in occurrences:
        per_day.setdefault(occ.start.date(), 0)
        per_day[occ.start.date()] += 1
    for day, count in sorted(per_day.items()):
        if count > LEAN_TARGET_PER_DAY:
            report.warnings.append(
                f"{day:%a %Y-%m-%d} has {count} events — over the ~{LEAN_TARGET_PER_DAY}/day lean target; consider cutting"
            )


def coverage_check(occurrences: List[Occurrence], now: datetime) -> List[str]:
    """Kindroid's integration needs at least one event in the past 24h and one
    in the next 24h to register. Report gaps so the user can fix them."""
    notes = []
    if not any(now - timedelta(hours=24) <= o.start <= now for o in occurrences):
        notes.append(
            "no event starts in the PAST 24h — the integration may not register; "
            "consider a week start of yesterday so a standing commitment lands there"
        )
    if not any(now <= o.start <= now + timedelta(hours=24) for o in occurrences):
        notes.append("no event starts in the NEXT 24h — the integration may not register")
    return notes
