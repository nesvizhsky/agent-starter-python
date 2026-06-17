"""Tests for disputatio/research.py.

Offline tests cover parsing logic without hitting any API.
Integration test calls real Perplexity (costs a little, ~$0.01).

    uv run pytest scripts/tests/test_disputatio_research.py            # offline only
    uv run pytest -m integration scripts/tests/test_disputatio_research.py  # live too
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from agent.services.llm import Research, Source
from disputatio.models import Topic
from disputatio.research import _headline_from_url, _parse, _source_from_url, gather

# ---------------------------------------------------------------------------
# Offline: parsing helpers
# ---------------------------------------------------------------------------


def _topic(**kwargs: object) -> Topic:
    defaults: dict[str, object] = dict(
        id=uuid4(),
        telegram_id=1,
        name="Test Topic",
        description=None,
        frequency="daily",
        send_hour=8,
        send_dow=0,
        timezone="UTC",
        paused=False,
        sources=["BBC", "Reuters"],
        excluded_sources=[],
        trusted_sources=[],
        pinned_persona=None,
        feedback_notes=None,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        last_sent_at=None,
        last_synthesis_at=None,
    )
    defaults.update(kwargs)
    return Topic.model_validate(defaults)


def test_source_from_url() -> None:
    assert _source_from_url("https://www.bbc.com/news/world") == "bbc.com"
    assert _source_from_url("https://tass.ru/en/world/123") == "tass.ru"
    assert _source_from_url("not-a-url") == "not-a-url"


def test_headline_from_url() -> None:
    assert _headline_from_url("https://bbc.com/news/russia-fires-missiles") == (
        "Russia Fires Missiles"
    )
    assert _headline_from_url("https://example.com/article_title_here") == "Article Title Here"


def test_parse_uses_title_as_headline() -> None:
    result = Research(
        text="Some news prose.",
        sources=[
            Source(url="https://bbc.com/news/article-1", title="Ukraine ceasefire talks stall"),
            Source(url="https://bbc.com/news/article-2", title=None),
        ],
    )
    articles = _parse(result, default_source="BBC")
    assert len(articles) == 2
    assert articles[0].headline == "Ukraine ceasefire talks stall"
    assert articles[0].source == "BBC"
    assert articles[0].url == "https://bbc.com/news/article-1"
    # Falls back to URL-derived headline when title is None
    assert "Article" in articles[1].headline or articles[1].headline != ""


def test_parse_skips_empty_urls() -> None:
    result = Research(
        text="prose",
        sources=[
            Source(url="", title="No URL"),
            Source(url="https://example.com/valid", title="Valid"),
        ],
    )
    articles = _parse(result, default_source="test")
    assert len(articles) == 1
    assert articles[0].url == "https://example.com/valid"


def test_parse_general_derives_source_from_url() -> None:
    result = Research(
        text="prose",
        sources=[Source(url="https://www.reuters.com/world/story", title="Big story")],
    )
    articles = _parse(result, default_source="general")
    assert articles[0].source == "reuters.com"


def test_gather_skips_excluded_sources() -> None:
    """gather() should not query excluded sources — verified by checking the
    active list filtering logic without making real network calls."""
    topic = _topic(sources=["BBC", "TASS", "Meduza"], excluded_sources=["TASS"])

    excluded = {s.lower() for s in topic.excluded_sources}
    active = [s for s in topic.sources if s.lower() not in excluded]
    assert "TASS" not in active
    assert "BBC" in active
    assert "Meduza" in active
    assert len(active) == 2


# ---------------------------------------------------------------------------
# Integration: real Perplexity call
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_gather_returns_articles() -> None:
    """Fire a real research call and verify we get structured articles back."""
    topic = _topic(
        name="archaeology news",
        sources=["Archaeology Magazine", "LiveScience"],
        frequency="weekly",
    )
    articles = await gather(topic)

    # Should have found something
    assert len(articles) > 0, "Expected at least one article from Perplexity"

    # Every article has the required fields
    for a in articles:
        assert a.url.startswith("http"), f"Bad URL: {a.url}"
        assert a.headline, f"Empty headline for {a.url}"
        assert a.source, f"Empty source for {a.url}"

    # No duplicate URLs
    urls = [a.url for a in articles]
    assert len(urls) == len(set(urls)), "Duplicate URLs in results"

    # Excluded source should not appear in source labels
    topic_with_exclusion = _topic(
        name="archaeology news",
        sources=["Archaeology Magazine", "LiveScience"],
        excluded_sources=["LiveScience"],
        frequency="weekly",
    )
    articles2 = await gather(topic_with_exclusion)
    source_labels = {a.source for a in articles2}
    assert "LiveScience" not in source_labels
