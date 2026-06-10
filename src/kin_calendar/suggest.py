"""Merging accepted suggestions into the spine.

Selection keys come from the review form: "commitment:<i>", "interest:<i>",
or "work_pattern". Accepted items are tagged source="user" — human-approved
canon, distinct from what the backstory stated or the extractor inferred.
"""

from __future__ import annotations

from typing import Iterable

from .models import SourcedValue, Spine, StandingCommitment, SuggestionSet


def apply_suggestions(spine: Spine, suggestions: SuggestionSet, selected: Iterable[str]) -> Spine:
    spine = spine.model_copy(deep=True)
    for key in selected:
        kind, _, index = key.partition(":")
        if kind == "commitment":
            s = suggestions.standing_commitments[int(index)]
            spine.standing_commitments.append(
                StandingCommitment(
                    what=s.what, cadence=s.cadence, time_of_day=s.time_of_day, source="user"
                )
            )
        elif kind == "interest":
            s = suggestions.interests[int(index)]
            spine.interests.append(SourcedValue(value=s.value, source="user"))
        elif kind == "work_pattern" and suggestions.work_pattern_suggestion:
            spine.daily_rhythm.work_pattern = SourcedValue(
                value=suggestions.work_pattern_suggestion, source="user"
            )
    return spine
