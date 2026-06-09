from datetime import date, datetime

from kin_calendar.models import GeneratedEvent
from kin_calendar.reconcile import (
    Decision,
    ExistingEvent,
    coverage_check,
    expand_occurrences,
    first_date_for_weekday,
    reconcile,
)

WEEK_START = date(2026, 6, 8)  # a Monday


def make_event(**overrides):
    base = dict(
        title="Derek teaches his self-defense class",
        type="standing",
        days_of_week=["Tue", "Thu"],
        date=None,
        start="18:00",
        end="19:30",
        location=None,
        gates_availability=True,
        description="Derek heads out to teach his self-defense class.",
    )
    base.update(overrides)
    return GeneratedEvent(**base)


def test_first_date_for_weekday():
    assert first_date_for_weekday(WEEK_START, "Mon") == date(2026, 6, 8)
    assert first_date_for_weekday(WEEK_START, "Sun") == date(2026, 6, 14)


def test_expand_standing_occurrences():
    occs = expand_occurrences(make_event(), WEEK_START)
    assert [o.start.date() for o in occs] == [date(2026, 6, 9), date(2026, 6, 11)]


def test_expand_overnight_occurrence():
    ev = make_event(
        title="Derek works the night shift",
        days_of_week=["Mon"],
        start="22:00",
        end="06:00",
    )
    (occ,) = expand_occurrences(ev, WEEK_START)
    assert occ.end == datetime(2026, 6, 9, 6, 0)


def test_reconcile_accepts_clean_events():
    report = reconcile([make_event()], [], WEEK_START)
    assert [v.decision for v in report.verdicts] == [Decision.ADD]


def test_reconcile_rejects_overlap_between_candidates():
    a = make_event()
    b = make_event(title="Derek meets a friend", days_of_week=["Tue"], start="18:30", end="20:00")
    report = reconcile([a, b], [], WEEK_START)
    assert report.verdicts[0].decision == Decision.ADD
    assert report.verdicts[1].decision == Decision.REJECT_OVERLAP


def test_reconcile_skips_exact_duplicate():
    existing = [
        ExistingEvent(
            title="Derek teaches his self-defense class",
            start=datetime(2026, 6, 9, 18, 0),
            end=datetime(2026, 6, 9, 19, 30),
        )
    ]
    report = reconcile([make_event()], existing, WEEK_START)
    assert report.verdicts[0].decision == Decision.SKIP_DUPLICATE


def test_reconcile_rejects_overlap_with_existing():
    existing = [
        ExistingEvent(
            title="Derek covers a shift",
            start=datetime(2026, 6, 9, 17, 0),
            end=datetime(2026, 6, 9, 19, 0),
        )
    ]
    report = reconcile([make_event()], existing, WEEK_START)
    assert report.verdicts[0].decision == Decision.REJECT_OVERLAP


def test_reconcile_evicts_when_budget_full():
    existing = [
        ExistingEvent(title=f"filler {i}", start=datetime(2026, 6, 9, i, 0), end=datetime(2026, 6, 9, i, 30))
        for i in range(18)
    ]
    ev = make_event(days_of_week=["Sat"])
    report = reconcile([ev], existing, WEEK_START)
    assert report.verdicts[0].decision == Decision.EVICT_BUDGET


def test_density_warning_over_lean_target():
    events = [
        make_event(title=f"Derek does thing {i}", days_of_week=["Tue"], start=f"{8 + 2 * i:02d}:00", end=f"{9 + 2 * i:02d}:00")
        for i in range(4)
    ]
    report = reconcile(events, [], WEEK_START)
    assert report.warnings


def test_coverage_check_flags_empty_windows():
    notes = coverage_check([], datetime(2026, 6, 8, 12, 0))
    assert len(notes) == 2


def test_coverage_check_passes_with_anchors():
    occs = expand_occurrences(
        make_event(days_of_week=["Mon", "Tue"], start="10:00", end="11:00"), WEEK_START
    )
    notes = coverage_check(occs, datetime(2026, 6, 8, 12, 0))
    assert notes == []


def test_google_timestamps_parse_and_compare():
    from datetime import timezone

    from kin_calendar.calendar_writer import _parse_google_ts

    older = _parse_google_ts("2026-06-08T10:00:00.000Z")
    newer = _parse_google_ts("2026-06-09T10:00:00.000Z")
    assert older.tzinfo == timezone.utc
    assert newer > older
    assert _parse_google_ts(None) is None


def test_existing_event_carries_timestamps():
    from kin_calendar.calendar_writer import _parse_google_ts

    ev = ExistingEvent(
        title="Derek covers a shift",
        start=datetime(2026, 6, 9, 17, 0),
        end=datetime(2026, 6, 9, 19, 0),
        source="mentioned",
        created=_parse_google_ts("2026-06-08T10:00:00.000Z"),
        updated=_parse_google_ts("2026-06-09T10:00:00.000Z"),
    )
    assert ev.updated > ev.created
