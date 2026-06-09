from datetime import date, datetime

from kin_calendar import refresh as refresh_mod
from kin_calendar.models import GeneratedEvent, GenerationResult, Spine
from kin_calendar.refresh import auto_refresh, refresh_due
from kin_calendar.reconcile import ExistingEvent
from kin_calendar.scanner import work_lock
from kin_calendar.store import Store
from kin_calendar.timeutil import is_valid_timezone, local_now, zone


# -- timezone helpers -----------------------------------------------------------


def test_zone_falls_back_to_utc_on_bad_name():
    assert str(zone("Not/AZone")) == "UTC"
    assert str(zone(None)) == "UTC"
    assert str(zone("America/New_York")) == "America/New_York"


def test_is_valid_timezone():
    assert is_valid_timezone("Europe/London")
    assert not is_valid_timezone("Middle/Nowhere")


def test_local_now_is_naive():
    assert local_now("America/New_York").tzinfo is None


# -- refresh scheduling -----------------------------------------------------------


def test_refresh_not_due_before_first_write():
    assert not refresh_due({"last_week_start": None}, date(2026, 6, 13))


def test_refresh_due_after_lead_days():
    state = {"last_week_start": "2026-06-08"}
    assert not refresh_due(state, date(2026, 6, 12))
    assert refresh_due(state, date(2026, 6, 13))


# -- auto refresh cycle -------------------------------------------------------------


SPINE = Spine.model_validate(
    {
        "kin_name": "Derek",
        "occupation": {"value": "instructor", "source": "stated"},
        "setting": {"value": "city", "source": "stated"},
        "temperament": {"value": "steady", "source": "stated"},
        "relationships": [],
        "interests": [],
        "standing_commitments": [],
        "daily_rhythm": {
            "wake": {"value": "unknown", "source": "unknown"},
            "sleep": {"value": "unknown", "source": "unknown"},
            "work_pattern": {"value": "days", "source": "stated"},
        },
        "availability_notes": "",
        "open_questions": [],
    }
)


def _generated_week():
    return GenerationResult(
        suggested_routine_note=None,
        unusual_rhythm=False,
        events=[
            GeneratedEvent(
                title="Derek teaches his class",
                type="standing",
                days_of_week=["Tue"],
                date=None,
                start="18:00",
                end="19:30",
                location=None,
                gates_availability=True,
                description="Derek heads out to teach.",
            ),
            GeneratedEvent(
                title="Derek tries the new coffee place",
                type="oneoff",
                days_of_week=None,
                date="2026-06-17",
                start="10:00",
                end="11:00",
                location=None,
                gates_availability=False,
                description="Derek checks out the new roastery.",
            ),
        ],
    )


class FakeWriter:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.written = []

    def list_existing(self, *_):
        return self.existing

    def write_event(self, event, week_start, provenance):
        self.written.append((event.title, provenance))


def test_auto_refresh_writes_new_week_and_skips_existing_standing(tmp_path, monkeypatch):
    store = Store(data_dir=tmp_path)
    store.update_state(calendar_id="cal1", last_week_start="2026-06-08")
    store.write_text(store.spine_path, SPINE.model_dump_json())

    monkeypatch.setattr(refresh_mod.llm, "generate_events", lambda *a, **kw: _generated_week())

    # The standing commitment from last week is already on the calendar.
    existing = [
        ExistingEvent(
            title="Derek teaches his class",
            start=datetime(2026, 6, 16, 18, 0),
            end=datetime(2026, 6, 16, 19, 30),
            source="generated",
        )
    ]
    writer = FakeWriter(existing)
    result = auto_refresh(store, writer_factory=lambda: writer, now=datetime(2026, 6, 15, 9, 0))

    assert result.error is None and result.ran
    assert result.written == 1
    assert writer.written == [("Derek tries the new coffee place", "generated")]
    assert any("skip_duplicate" in s for s in result.skipped)
    assert store.state()["last_week_start"] == "2026-06-15"


def test_auto_refresh_requires_spine(tmp_path):
    store = Store(data_dir=tmp_path)
    store.update_state(calendar_id="cal1")
    result = auto_refresh(store, writer_factory=FakeWriter, now=datetime(2026, 6, 15, 9, 0))
    assert result.error


def test_auto_refresh_keeps_state_on_generation_failure(tmp_path, monkeypatch):
    store = Store(data_dir=tmp_path)
    store.update_state(calendar_id="cal1", last_week_start="2026-06-08")
    store.write_text(store.spine_path, SPINE.model_dump_json())

    def boom(*a, **kw):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(refresh_mod.llm, "generate_events", boom)
    result = auto_refresh(store, writer_factory=FakeWriter, now=datetime(2026, 6, 15, 9, 0))

    assert result.error
    assert store.state()["last_week_start"] == "2026-06-08"  # still due; retried later


# -- shared work lock -----------------------------------------------------------------


def test_lock_prevents_concurrent_jobs(tmp_path):
    from kin_calendar.scanner import scan_once

    store = Store(data_dir=tmp_path)
    store.update_state(
        kindroid_api_key="kn_test", kindroid_ai_id="ai1", calendar_id="cal1",
        last_week_start="2026-06-01",
    )
    store.write_text(store.spine_path, SPINE.model_dump_json())

    assert work_lock.acquire(blocking=False)
    try:
        scan = scan_once(store)
        assert scan.error and "already running" in scan.error
        refresh = auto_refresh(store, writer_factory=FakeWriter)
        assert refresh.error and "already running" in refresh.error
    finally:
        work_lock.release()
