"""Shared Pydantic models for Disputatio.

These mirror the DB schema and are passed between modules.
Article is the research pipeline's unit; everything else maps to a DB table.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class User(BaseModel):
    telegram_id: int
    first_name: str
    username: str | None
    created_at: datetime


class Topic(BaseModel):
    id: UUID
    telegram_id: int
    name: str
    description: str | None
    frequency: str  # 'daily' | 'twice_daily' | 'weekly'
    send_hour: int
    timezone: str
    paused: bool
    sources: list[str]
    excluded_sources: list[str]
    trusted_sources: list[str]
    pinned_persona: str | None
    feedback_notes: str | None
    created_at: datetime
    last_sent_at: datetime | None
    last_synthesis_at: datetime | None


class Article(BaseModel):
    """One article from the research step. Not stored directly — store.py takes these."""

    url: str
    headline: str
    source: str
    published_at: datetime | None
    summary: str
    context: str | None = None  # full Perplexity research answer from the query that found this


class SourceView(BaseModel):
    """One source's take on a story. signals is populated by propaganda.py."""

    source: str
    url: str
    summary: str
    signals: list[str] = Field(default_factory=list)


class Story(BaseModel):
    """One real-world event covered from multiple source perspectives."""

    headline: str  # neutral description of the event
    source_views: list[SourceView]


class Digest(BaseModel):
    id: UUID
    topic_id: UUID
    telegram_id: int
    content: str
    persona: str
    created_at: datetime
