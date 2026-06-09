"""Schemas for the life spine (extraction output) and event list (generation output).

All LLM output is parsed and validated against these models before anything
downstream touches it — never pipe raw model output into the calendar API.
"""

from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SourceTag = Literal["stated", "inferred", "unknown"]
Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

WEEKDAY_ORDER: List[str] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Spine (extraction output — Appendix A)
# ---------------------------------------------------------------------------


class SourcedValue(BaseModel):
    value: str
    source: SourceTag


class Relationship(BaseModel):
    who: str
    relation: str
    source: SourceTag


class StandingCommitment(BaseModel):
    what: str
    cadence: str
    time_of_day: str
    source: SourceTag


class DailyRhythm(BaseModel):
    wake: SourcedValue
    sleep: SourcedValue
    work_pattern: SourcedValue


class Spine(BaseModel):
    kin_name: str
    occupation: SourcedValue
    setting: SourcedValue
    temperament: SourcedValue
    relationships: List[Relationship] = Field(default_factory=list)
    interests: List[SourcedValue] = Field(default_factory=list)
    standing_commitments: List[StandingCommitment] = Field(default_factory=list)
    daily_rhythm: DailyRhythm
    availability_notes: str = ""
    open_questions: List[str] = Field(default_factory=list)

    def review_flags(self) -> List[str]:
        """Fields tagged inferred/unknown — the ones a human should check first."""
        flags: List[str] = []

        def check(label: str, source: str) -> None:
            if source != "stated":
                flags.append(f"{label} [{source}]")

        check("occupation", self.occupation.source)
        check("setting", self.setting.source)
        check("temperament", self.temperament.source)
        for r in self.relationships:
            check(f"relationship: {r.who} ({r.relation})", r.source)
        for i in self.interests:
            check(f"interest: {i.value}", i.source)
        for c in self.standing_commitments:
            check(f"commitment: {c.what}", c.source)
        check("daily_rhythm.wake", self.daily_rhythm.wake.source)
        check("daily_rhythm.sleep", self.daily_rhythm.sleep.source)
        check("daily_rhythm.work_pattern", self.daily_rhythm.work_pattern.source)
        return flags


# ---------------------------------------------------------------------------
# Events (generation output — Appendix B)
# ---------------------------------------------------------------------------


class GeneratedEvent(BaseModel):
    title: str
    type: Literal["standing", "oneoff"]
    days_of_week: Optional[List[Weekday]] = None
    date: Optional[str] = None
    start: str
    end: str
    location: Optional[str] = None
    gates_availability: bool
    description: str

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f"not a valid HH:MM time: {v!r}")
        return v

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _DATE_RE.match(v):
            raise ValueError(f"not a valid YYYY-MM-DD date: {v!r}")
        return v

    @model_validator(mode="after")
    def _type_fields_consistent(self) -> "GeneratedEvent":
        if self.type == "standing" and not self.days_of_week:
            raise ValueError(f"standing event {self.title!r} must list days_of_week")
        if self.type == "oneoff" and not self.date:
            raise ValueError(f"one-off event {self.title!r} must have a date")
        return self

    @property
    def crosses_midnight(self) -> bool:
        """end <= start is read as an overnight block (e.g. a night shift)."""
        return self.end <= self.start


class GenerationResult(BaseModel):
    suggested_routine_note: Optional[str] = None
    unusual_rhythm: bool = False
    events: List[GeneratedEvent] = Field(default_factory=list)


class GenerationOutput(BaseModel):
    """What `kin-calendar generate` writes to disk: the result plus the
    parameters needed to interpret it at write time."""

    kin_name: str
    week_start: str
    result: GenerationResult

    @field_validator("week_start")
    @classmethod
    def _valid_week_start(cls, v: str) -> str:
        if not _DATE_RE.match(v):
            raise ValueError(f"not a valid YYYY-MM-DD date: {v!r}")
        return v
