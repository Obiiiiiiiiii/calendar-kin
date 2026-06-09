import pytest
from pydantic import ValidationError

from kin_calendar.models import GeneratedEvent, GenerationResult, Spine


def make_event(**overrides):
    base = dict(
        title="Derek teaches his self-defense class",
        type="standing",
        days_of_week=["Tue", "Thu"],
        date=None,
        start="18:00",
        end="19:30",
        location="Eastside gym",
        gates_availability=True,
        description="Derek heads out to teach his Tuesday/Thursday self-defense class at the Eastside gym.",
    )
    base.update(overrides)
    return GeneratedEvent(**base)


def test_standing_event_requires_days():
    with pytest.raises(ValidationError):
        make_event(days_of_week=None)


def test_oneoff_event_requires_date():
    with pytest.raises(ValidationError):
        make_event(type="oneoff", days_of_week=None, date=None)


def test_oneoff_event_valid():
    ev = make_event(type="oneoff", days_of_week=None, date="2026-06-11")
    assert ev.date == "2026-06-11"


def test_bad_time_rejected():
    with pytest.raises(ValidationError):
        make_event(start="25:00")


def test_overnight_block_detected():
    ev = make_event(start="22:00", end="06:00")
    assert ev.crosses_midnight


def test_generation_result_roundtrip():
    result = GenerationResult(
        suggested_routine_note=None, unusual_rhythm=False, events=[make_event()]
    )
    parsed = GenerationResult.model_validate_json(result.model_dump_json())
    assert parsed.events[0].title == result.events[0].title


def test_spine_review_flags():
    spine = Spine.model_validate(
        {
            "kin_name": "Derek",
            "occupation": {"value": "self-defense instructor", "source": "stated"},
            "setting": {"value": "a mid-size city", "source": "inferred"},
            "temperament": {"value": "steady, protective", "source": "stated"},
            "relationships": [],
            "interests": [{"value": "boxing", "source": "unknown"}],
            "standing_commitments": [],
            "daily_rhythm": {
                "wake": {"value": "unknown", "source": "unknown"},
                "sleep": {"value": "unknown", "source": "unknown"},
                "work_pattern": {"value": "days", "source": "stated"},
            },
            "availability_notes": "",
            "open_questions": ["Which days does he teach?"],
        }
    )
    flags = spine.review_flags()
    assert any("setting" in f for f in flags)
    assert any("boxing" in f for f in flags)
    assert not any("occupation" in f for f in flags)
