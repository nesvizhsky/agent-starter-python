"""Shared Pydantic models for Eat the Elephant.

These mirror the DB schema and are passed between modules.
Article is the research pipeline's unit; everything else maps to a DB table.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class User(BaseModel):
    telegram_id: int
    first_name: str
    username: str | None
    created_at: datetime


class Side(BaseModel):
    """One party/perspective in a topic (a state, a faction, government vs. opposition,
    regulator vs. industry, etc.) with a few example outlets that lean toward it."""

    name: str
    outlets: list[str] = Field(default_factory=list)


class Slot(BaseModel):
    """One recurring checkup: which day(s), what time, and how often.

    A topic can have any number of slots — e.g. two "every day" slots at different
    times (twice daily, arbitrary gap), or two single-day slots ("Mon 12:00" +
    "Thu 10:00"). Each slot tracks its own last_sent_at so slots don't interfere
    with each other's due-ness.
    """

    id: UUID | None = None  # None until persisted
    days: list[int] = Field(default_factory=list)  # 0=Mon…6=Sun; empty = every day
    hour: int
    minute: int = 0
    every_n_weeks: int = 0  # 0 = fire on every matching day; >=1 = gate to once every N weeks
    last_sent_at: datetime | None = None


class Topic(BaseModel):
    id: UUID
    telegram_id: int
    name: str
    description: str | None
    display_name: str | None = None  # translated display label (UI only)
    display_description: str | None = None  # translated description (UI only)
    timezone: str
    paused: bool
    sources: list[str]
    excluded_sources: list[str]
    trusted_sources: list[str]
    feedback_notes: str | None
    source_guidance: str | None
    sides_json: str | None = None  # JSON-encoded list[Side]; use the `sides` property
    created_at: datetime
    last_sent_at: datetime | None  # most recent send across all slots; display only
    slots: list[Slot] = Field(default_factory=list)  # populated separately by store.py

    @property
    def shown_name(self) -> str:
        return self.display_name or self.name

    @property
    def shown_description(self) -> str | None:
        return self.display_description or self.description

    @property
    def sides(self) -> list[Side]:
        """Parsed sides list. Empty for topics with no identified sides, or on bad data."""
        if not self.sides_json:
            return []
        try:
            return [Side.model_validate(s) for s in json.loads(self.sides_json)]
        except (ValueError, TypeError):
            return []


class Article(BaseModel):
    """One article from the research step. Not stored directly — store.py takes these."""

    url: str
    headline: str
    source: str
    published_at: datetime | None
    summary: str
    context: str | None = None  # full Perplexity research answer from the query that found this
    is_general_query: bool = False  # came from the catch-all query, not a specific outlet


class SourceView(BaseModel):
    """One source's take on a story. signals is populated by propaganda.py."""

    source: str
    url: str
    summary: str
    signals: list[str] = Field(default_factory=list)
    # Research prose attached after clustering; excluded from LLM-facing outputs.
    context: str | None = Field(default=None, exclude=True)


class Contradiction(BaseModel):
    """A direct factual conflict between two sources on the same story."""

    source_a: str
    claim_a: str
    source_b: str
    claim_b: str


class Story(BaseModel):
    """One real-world event covered from multiple source perspectives."""

    headline: str = Field(
        description=(
            "Sentence-case headline (lowercase except first word and proper nouns). "
            "Must tell the reader what specifically happened — not a category label. "
            "Good: 'Russia tightens small-business taxes to fund the war' "
            "Bad: 'Russian Economic Policy Changes'"
        )
    )
    event_date: str | None = Field(
        default=None,
        description=(
            "Approximate date of the event, extracted from research context. "
            "Format: 'Jun 15' or 'June 17, 2026'. None if genuinely unknown."
        ),
    )
    importance: int = Field(
        default=2,
        description=(
            "1 = concrete breaking event (attack, arrest, vote, launch, death, signed deal). "
            "2 = development or trend (economic data, ongoing operation, social shift). "
            "3 = analysis, report, anniversary, or background context."
        ),
    )
    source_views: list[SourceView]
    contradictions: list[Contradiction] = Field(default_factory=list)
