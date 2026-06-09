"""LLM calls: extraction (backstory -> spine) and generation (spine -> events).

All output is schema-validated via structured outputs before use; malformed
output is retried, never written anywhere.

Note on temperature: the brief calls for low-temperature extraction and warmer
generation. Current Opus models (4.7+) do not accept sampling parameters, so
grounding is carried by the prompts themselves (the extraction prompt is
strictly grounded; the generation prompt is fenced by hard rules). If you
configure an older model that supports temperature, set
KIN_EXTRACT_TEMPERATURE / KIN_GENERATE_TEMPERATURE to restore the split.
"""

from __future__ import annotations

import json
import os
from importlib import resources
from typing import Optional, Type, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from .models import DuplicateCheck, GenerationResult, MentionScan, Spine

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000
MAX_ATTEMPTS = 3

T = TypeVar("T", bound=BaseModel)


def _load_prompt(name: str) -> str:
    return (resources.files("kin_calendar") / "prompts" / name).read_text(encoding="utf-8")


def _model() -> str:
    return os.environ.get("KIN_MODEL", DEFAULT_MODEL)


def _call(prompt: str, output_type: Type[T], temperature_env: str) -> T:
    client = anthropic.Anthropic()
    kwargs: dict = {}
    temp = os.environ.get(temperature_env)
    if temp:
        # Only for models that still accept sampling params; thinking and
        # temperature are mutually exclusive, so thinking stays off here.
        kwargs["temperature"] = float(temp)
    else:
        kwargs["thinking"] = {"type": "adaptive"}

    last_error: Exception | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            response = client.messages.parse(
                model=_model(),
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                output_format=output_type,
                **kwargs,
            )
            parsed = response.parsed_output
            if parsed is None:
                raise ValueError("model returned no parseable output")
            return parsed
        except (ValidationError, ValueError, anthropic.APIStatusError) as e:
            if isinstance(e, anthropic.APIStatusError) and e.status_code < 500:
                raise  # 4xx won't get better on retry
            last_error = e
    raise RuntimeError(f"LLM output failed validation after {MAX_ATTEMPTS} attempts") from last_error


def extract_spine(backstory: str) -> Spine:
    """LLM call 1: backstory text -> structured life spine, grounded in the text only."""
    prompt = _load_prompt("extraction.txt").replace("{{BACKSTORY}}", backstory)
    return _call(prompt, Spine, "KIN_EXTRACT_TEMPERATURE")


def generate_events(
    spine: Spine,
    week_start: str,
    weather_forecast: Optional[str] = None,
) -> GenerationResult:
    """LLM call 2: confirmed spine -> lean event list (+ optional routine note)."""
    prompt = (
        _load_prompt("generation.txt")
        .replace("{{CONFIRMED_SPINE}}", json.dumps(spine.model_dump(), indent=2))
        .replace("{{WEEK_START_DATE}}", week_start)
        .replace("{{WEATHER_FORECAST}}", weather_forecast or "(none provided)")
    )
    return _call(prompt, GenerationResult, "KIN_GENERATE_TEMPERATURE")


def detect_mentions(
    transcript: str,
    today: str,
    today_weekday: str,
    date_table: str,
    timezone: str,
) -> MentionScan:
    """Phase 2: chat transcript -> plannable future events the kin mentioned.

    Conservative by instruction: an empty candidate list is the expected
    answer for most transcripts.
    """
    prompt = (
        _load_prompt("mention.txt")
        .replace("{{TRANSCRIPT}}", transcript)
        .replace("{{TODAY}}", today)
        .replace("{{TODAY_WEEKDAY}}", today_weekday)
        .replace("{{DATE_TABLE}}", date_table)
        .replace("{{TIMEZONE}}", timezone)
    )
    return _call(prompt, MentionScan, "KIN_EXTRACT_TEMPERATURE")


def semantic_duplicate(candidate_json: str, existing_listing: str) -> DuplicateCheck:
    """Phase 2: is the candidate a reworded duplicate of an existing event?"""
    prompt = (
        _load_prompt("duplicate.txt")
        .replace("{{CANDIDATE}}", candidate_json)
        .replace("{{EXISTING}}", existing_listing)
    )
    return _call(prompt, DuplicateCheck, "KIN_EXTRACT_TEMPERATURE")
