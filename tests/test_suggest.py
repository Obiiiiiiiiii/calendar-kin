from kin_calendar.models import Spine, SuggestionSet
from kin_calendar.suggest import apply_suggestions

SPINE = Spine.model_validate(
    {
        "kin_name": "Tate",
        "occupation": {"value": "personal assistant AI", "source": "stated"},
        "setting": {"value": "Star Wars universe", "source": "stated"},
        "temperament": {"value": "snarky, loyal", "source": "stated"},
        "relationships": [],
        "interests": [{"value": "witty banter", "source": "inferred"}],
        "standing_commitments": [],
        "daily_rhythm": {
            "wake": {"value": "unknown", "source": "unknown"},
            "sleep": {"value": "unknown", "source": "unknown"},
            "work_pattern": {"value": "unknown", "source": "unknown"},
        },
        "availability_notes": "",
        "open_questions": [],
    }
)

SUGGESTIONS = SuggestionSet.model_validate(
    {
        "standing_commitments": [
            {
                "what": "Runs full diagnostics and defrag cycle",
                "cadence": "weekly, Sun",
                "time_of_day": "late night",
                "rationale": "an AI would need maintenance windows",
            },
            {
                "what": "Sweeps the holonet for intel relevant to Magenta",
                "cadence": "daily",
                "time_of_day": "unknown",
                "rationale": "his job is assisting Magenta",
            },
        ],
        "interests": [
            {"value": "archiving old memory files", "rationale": "his history with Camda"}
        ],
        "work_pattern_suggestion": "always-on, background tasks during Magenta's sleep hours",
    }
)


def test_apply_selected_commitment_and_interest():
    merged = apply_suggestions(SPINE, SUGGESTIONS, ["commitment:0", "interest:0"])
    assert len(merged.standing_commitments) == 1
    assert merged.standing_commitments[0].what == "Runs full diagnostics and defrag cycle"
    assert merged.standing_commitments[0].source == "user"
    assert any(i.value == "archiving old memory files" and i.source == "user" for i in merged.interests)
    # unticked suggestion not added
    assert not any("holonet" in c.what for c in merged.standing_commitments)


def test_apply_work_pattern_replaces():
    merged = apply_suggestions(SPINE, SUGGESTIONS, ["work_pattern"])
    assert merged.daily_rhythm.work_pattern.value.startswith("always-on")
    assert merged.daily_rhythm.work_pattern.source == "user"


def test_apply_nothing_leaves_spine_unchanged():
    merged = apply_suggestions(SPINE, SUGGESTIONS, [])
    assert merged == SPINE


def test_original_spine_not_mutated():
    apply_suggestions(SPINE, SUGGESTIONS, ["commitment:0", "commitment:1", "interest:0"])
    assert SPINE.standing_commitments == []
    assert len(SPINE.interests) == 1


def test_empty_property():
    assert SuggestionSet().empty
    assert not SUGGESTIONS.empty
