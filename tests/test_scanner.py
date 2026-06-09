from datetime import date, datetime

from kin_calendar import scanner as scanner_mod
from kin_calendar.kindroid import format_transcript, last_timestamp, normalize_message
from kin_calendar.models import GeneratedEvent, MentionCandidate, MentionScan, DuplicateCheck
from kin_calendar.reconcile import Decision, ExistingEvent, reconcile
from kin_calendar.scanner import scan_once
from kin_calendar.store import Store

WEEK_START = date(2026, 6, 8)  # a Monday


# -- kindroid helpers ---------------------------------------------------------


def test_normalize_message_handles_field_variants():
    assert normalize_message({"timestamp": 123, "sender": "ai", "message": "hi"}) == {
        "timestamp": "123", "sender": "ai", "text": "hi",
    }
    assert normalize_message({"created_at": "t1", "role": "user", "text": "yo"})["sender"] == "user"


def test_transcript_and_cursor():
    msgs = [
        {"timestamp": "1", "sender": "user", "text": "see you thursday?"},
        {"timestamp": "2", "sender": "ai", "text": "I've got that gig Thursday night."},
    ]
    assert "[ai] I've got that gig Thursday night." in format_transcript(msgs)
    assert last_timestamp(msgs) == "2"


# -- provenance: mentioned beats generated -------------------------------------


def _mentioned_candidate(**overrides):
    base = dict(
        title="Derek plays his gig at the Blue Room",
        type="oneoff",
        days_of_week=None,
        date="2026-06-11",
        start="20:00",
        end="23:00",
        location="Blue Room",
        gates_availability=True,
        description="Derek plays his Thursday night gig at the Blue Room.",
        quote="I've got that gig Thursday night.",
        confidence="high",
    )
    base.update(overrides)
    return MentionCandidate(**base)


def test_mentioned_replaces_clashing_generated():
    existing = [
        ExistingEvent(
            title="Derek practices at home",
            start=datetime(2026, 6, 11, 19, 0),
            end=datetime(2026, 6, 11, 21, 0),
            source="generated",
            event_id="gid123",
        )
    ]
    report = reconcile([_mentioned_candidate()], existing, WEEK_START, candidate_source="mentioned")
    (verdict,) = report.verdicts
    assert verdict.decision == Decision.REPLACE
    assert verdict.replaces.event_id == "gid123"


def test_mentioned_does_not_displace_mentioned():
    existing = [
        ExistingEvent(
            title="Derek meets Marcus",
            start=datetime(2026, 6, 11, 19, 0),
            end=datetime(2026, 6, 11, 21, 0),
            source="mentioned",
        )
    ]
    report = reconcile([_mentioned_candidate()], existing, WEEK_START, candidate_source="mentioned")
    assert report.verdicts[0].decision == Decision.REJECT_OVERLAP


def test_generated_never_displaces():
    existing = [
        ExistingEvent(
            title="Derek meets Marcus",
            start=datetime(2026, 6, 11, 19, 0),
            end=datetime(2026, 6, 11, 21, 0),
            source="generated",
        )
    ]
    candidate = GeneratedEvent(**{
        k: v for k, v in _mentioned_candidate().model_dump().items()
        if k not in ("quote", "confidence")
    })
    report = reconcile([candidate], existing, WEEK_START, candidate_source="generated")
    assert report.verdicts[0].decision == Decision.REJECT_OVERLAP


# -- full scan cycle with fakes -------------------------------------------------


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    def get_new_messages(self, start_after_timestamp=None):
        return self.messages


class FakeWriter:
    def __init__(self):
        self.written = []
        self.deleted = []

    def list_existing(self, *_):
        return [
            ExistingEvent(
                title="Derek practices at home",
                start=datetime(2026, 6, 11, 19, 0),
                end=datetime(2026, 6, 11, 21, 0),
                source="generated",
                event_id="gid123",
            )
        ]

    def write_event(self, event, week_start, provenance):
        self.written.append((event.title, provenance))

    def delete_event(self, event_id):
        self.deleted.append(event_id)


def test_scan_once_writes_mentioned_event(tmp_path, monkeypatch):
    store = Store(data_dir=tmp_path)
    store.update_state(kindroid_api_key="kn_test", kindroid_ai_id="ai1", calendar_id="cal1")

    monkeypatch.setattr(
        scanner_mod.llm, "detect_mentions",
        lambda **kw: MentionScan(candidates=[_mentioned_candidate()]),
    )
    monkeypatch.setattr(
        scanner_mod.llm, "semantic_duplicate",
        lambda *a, **kw: DuplicateCheck(duplicate_of=None),
    )

    writer = FakeWriter()
    messages = [{"timestamp": "42", "sender": "ai", "message": "I've got that gig Thursday night."}]
    result = scan_once(
        store,
        client=FakeClient(messages),
        writer_factory=lambda: writer,
        now=datetime(2026, 6, 8, 12, 0),
    )

    assert result.error is None
    assert result.written == 1
    assert result.replaced == 1
    assert writer.deleted == ["gid123"]
    assert writer.written == [("Derek plays his gig at the Blue Room", "mentioned")]
    assert store.state()["scanner_cursor"] == "42"


def test_scan_once_skips_semantic_duplicate(tmp_path, monkeypatch):
    store = Store(data_dir=tmp_path)
    store.update_state(kindroid_api_key="kn_test", kindroid_ai_id="ai1", calendar_id="cal1")

    monkeypatch.setattr(
        scanner_mod.llm, "detect_mentions",
        lambda **kw: MentionScan(candidates=[_mentioned_candidate()]),
    )
    monkeypatch.setattr(
        scanner_mod.llm, "semantic_duplicate",
        lambda *a, **kw: DuplicateCheck(duplicate_of=0, reason="same gig, reworded"),
    )

    writer = FakeWriter()
    messages = [{"timestamp": "43", "sender": "ai", "message": "Gig Thursday!"}]
    result = scan_once(
        store, client=FakeClient(messages), writer_factory=lambda: writer,
        now=datetime(2026, 6, 8, 12, 0),
    )

    assert result.written == 0
    assert writer.written == []
    assert any("semantic duplicate" in s for s in result.skipped)
    assert store.state()["scanner_cursor"] == "43"


def test_scan_once_filters_low_confidence(tmp_path, monkeypatch):
    store = Store(data_dir=tmp_path)
    store.update_state(kindroid_api_key="kn_test", kindroid_ai_id="ai1", calendar_id="cal1")

    monkeypatch.setattr(
        scanner_mod.llm, "detect_mentions",
        lambda **kw: MentionScan(candidates=[_mentioned_candidate(confidence="low")]),
    )

    writer = FakeWriter()
    messages = [{"timestamp": "44", "sender": "ai", "message": "maybe I'll go out sometime"}]
    result = scan_once(
        store, client=FakeClient(messages), writer_factory=lambda: writer,
        now=datetime(2026, 6, 8, 12, 0),
    )

    assert result.written == 0
    assert any("low-confidence" in s for s in result.skipped)


def test_scan_once_no_cursor_advance_on_error(tmp_path, monkeypatch):
    store = Store(data_dir=tmp_path)
    store.update_state(
        kindroid_api_key="kn_test", kindroid_ai_id="ai1", calendar_id="cal1",
        scanner_cursor="41",
    )

    def boom(**kw):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(scanner_mod.llm, "detect_mentions", boom)

    messages = [{"timestamp": "45", "sender": "ai", "message": "gig thursday"}]
    result = scan_once(
        store, client=FakeClient(messages), writer_factory=lambda: FakeWriter(),
        now=datetime(2026, 6, 8, 12, 0),
    )

    assert result.error
    assert store.state()["scanner_cursor"] == "41"  # unchanged — messages retried next cycle


def test_scan_once_requires_configuration(tmp_path):
    result = scan_once(Store(data_dir=tmp_path))
    assert result.error
