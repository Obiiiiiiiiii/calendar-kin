"""Phase 2: reactive capture.

One scan cycle reads new chat messages since the saved cursor, asks the LLM
for plannable future events the kin mentioned, runs each candidate through
semantic + mechanical reconciliation, and writes survivors to the calendar
tagged `source = mentioned`. Chat is ground truth: a mentioned event displaces
a clashing generated one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional

from . import llm
from .kindroid import KindroidClient, format_transcript, last_timestamp, normalize_message
from .models import WEEKDAY_ORDER, MentionCandidate
from .reconcile import Decision, ExistingEvent, reconcile
from .store import Store

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
DEFAULT_MIN_CONFIDENCE = "medium"
DEFAULT_MAX_POLL_MINUTES = 240


def next_poll_minutes(base_minutes: int, idle_streak: int, max_minutes: Optional[int] = None) -> int:
    """Adaptive backoff: poll at the base rate while chat is active, and double
    the interval after each quiet scan, capped at KIN_POLL_MAX_MINUTES.

    Keeps same-day mentions timely without pinging Kindroid all night: with a
    15-minute base, a quiet stretch backs off 15 -> 30 -> 60 -> 120 -> 240 min,
    and one new message snaps it back to 15.
    """
    if max_minutes is None:
        max_minutes = int(os.environ.get("KIN_POLL_MAX_MINUTES", str(DEFAULT_MAX_POLL_MINUTES)))
    return min(base_minutes * (2 ** idle_streak), max(max_minutes, base_minutes))


@dataclass
class ScanResult:
    messages_seen: int = 0
    candidates: int = 0
    written: int = 0
    replaced: int = 0
    skipped: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return f"error: {self.error}"
        if self.messages_seen == 0:
            return "no new messages"
        parts = [f"{self.messages_seen} new message(s)", f"{self.candidates} candidate(s)"]
        if self.written:
            parts.append(f"{self.written} written")
        if self.replaced:
            parts.append(f"{self.replaced} replaced a generated event")
        parts.extend(self.skipped)
        return ", ".join(parts)


def _date_table(today: date) -> str:
    lines = []
    for offset in range(8):
        d = today + timedelta(days=offset)
        label = "today" if offset == 0 else ("tomorrow" if offset == 1 else "")
        lines.append(f"  {WEEKDAY_ORDER[d.weekday()]} = {d.isoformat()}" + (f" ({label})" if label else ""))
    return "\n".join(lines)


def _existing_listing(existing: List[ExistingEvent]) -> str:
    return "\n".join(
        f"  [{i}] {ex.title} — {ex.start:%a %Y-%m-%d %H:%M} to {ex.end:%H:%M} (source: {ex.source})"
        for i, ex in enumerate(existing)
    ) or "  (calendar is empty)"


def scan_once(
    store: Store,
    min_confidence: str = DEFAULT_MIN_CONFIDENCE,
    client: Optional[KindroidClient] = None,
    writer_factory: Optional[Callable] = None,
    now: Optional[datetime] = None,
) -> ScanResult:
    """Run one poll cycle. `client`/`writer_factory` are injectable for tests."""
    result = ScanResult()
    now = now or datetime.now()
    state = store.state()

    api_key = store.setting("kindroid_api_key", "KINDROID_API_KEY")
    ai_id = store.setting("kindroid_ai_id", "KINDROID_AI_ID")
    if not api_key or not ai_id:
        result.error = "Kindroid API key / AI id not configured"
        return result
    if not state.get("calendar_id") and writer_factory is None:
        result.error = "no target calendar designated"
        return result

    client = client or KindroidClient(api_key, ai_id)

    try:
        messages = [normalize_message(m) for m in client.get_new_messages(state.get("scanner_cursor"))]
    except Exception as e:  # noqa: BLE001 — surface any API failure in the log
        result.error = f"Kindroid fetch failed: {e}"
        return result

    result.messages_seen = len(messages)
    if not messages:
        return result

    cursor = last_timestamp(messages)
    transcript = format_transcript(messages)
    if not transcript.strip():
        _advance(store, cursor)
        return result

    try:
        scan = llm.detect_mentions(
            transcript=transcript,
            today=now.date().isoformat(),
            today_weekday=WEEKDAY_ORDER[now.date().weekday()],
            date_table=_date_table(now.date()),
            timezone=store.timezone(),
        )
    except Exception as e:  # noqa: BLE001
        result.error = f"mention detection failed: {e}"
        return result  # cursor NOT advanced — retry these messages next cycle

    candidates = [
        c for c in scan.candidates
        if _CONFIDENCE_RANK[c.confidence] >= _CONFIDENCE_RANK.get(min_confidence, 1)
    ]
    for c in scan.candidates:
        if c not in candidates:
            result.skipped.append(f"skipped low-confidence: {c.title!r}")
    result.candidates = len(scan.candidates)

    if not candidates:
        _advance(store, cursor)
        return result

    try:
        writer = writer_factory() if writer_factory else _default_writer(store)
        window_start = now.astimezone() - timedelta(hours=24)
        window_end = now.astimezone() + timedelta(days=8)
        existing = writer.list_existing(window_start, window_end)

        survivors: List[MentionCandidate] = []
        for candidate in candidates:
            check = llm.semantic_duplicate(
                candidate.model_dump_json(indent=2), _existing_listing(existing)
            )
            if check.duplicate_of is not None:
                result.skipped.append(
                    f"semantic duplicate: {candidate.title!r} ({check.reason})"
                )
            else:
                survivors.append(candidate)

        report = reconcile(survivors, existing, now.date(), candidate_source="mentioned")

        for verdict in report.verdicts:
            if verdict.decision == Decision.ADD:
                writer.write_event(verdict.event, now.date(), provenance="mentioned")
                result.written += 1
            elif verdict.decision == Decision.REPLACE:
                if verdict.replaces and verdict.replaces.event_id:
                    writer.delete_event(verdict.replaces.event_id)
                writer.write_event(verdict.event, now.date(), provenance="mentioned")
                result.written += 1
                result.replaced += 1
            else:
                result.skipped.append(
                    f"{verdict.decision.value}: {verdict.event.title!r} — {verdict.reason}"
                )
    except Exception as e:  # noqa: BLE001
        result.error = f"calendar write failed: {e}"
        return result  # cursor NOT advanced

    _advance(store, cursor)
    return result


def _advance(store: Store, cursor: Optional[str]) -> None:
    if cursor:
        store.update_state(scanner_cursor=cursor)


def _default_writer(store: Store):
    from .calendar_writer import CalendarWriter, service_from_token

    state = store.state()
    service = service_from_token(str(store.token_path))
    return CalendarWriter(service, state["calendar_id"], store.timezone())


def log_scan(store: Store, result: ScanResult) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    store.append_log(f"[{stamp}] {result.summary()}")
