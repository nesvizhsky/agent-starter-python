"""Tests for disputatio/perspectives.py.

Offline tests cover helpers and the recovery safeguard.
Integration test fires a real LLM call to verify clustering.

    uv run pytest scripts/tests/test_disputatio_perspectives.py             # offline
    uv run pytest -m integration scripts/tests/test_disputatio_perspectives.py
"""

from __future__ import annotations

import pytest

from disputatio.models import Article
from disputatio.perspectives import _format_prompt, cluster


def _article(url: str, headline: str, source: str) -> Article:
    return Article(url=url, headline=headline, source=source, published_at=None, summary=headline)


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


def test_format_prompt_includes_all_articles() -> None:
    articles = [
        _article("https://bbc.com/a", "Ceasefire talks begin", "BBC"),
        _article("https://tass.ru/b", "Russia proposes peace", "TASS"),
    ]
    prompt = _format_prompt(articles, topic_name="Russia-Ukraine")
    assert "BBC" in prompt
    assert "TASS" in prompt
    assert "Ceasefire talks begin" in prompt
    assert "Russia proposes peace" in prompt
    assert "https://bbc.com/a" in prompt
    assert "Russia-Ukraine" in prompt


async def test_cluster_empty_returns_empty() -> None:
    result = await cluster([])
    assert result == []


# ---------------------------------------------------------------------------
# Integration: real LLM call
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cluster_groups_same_event() -> None:
    """Three articles: two about the same event from different sources, one unrelated."""
    articles = [
        _article(
            "https://bbc.com/ukraine-strike",
            "Russia fires missiles at Kyiv, killing 3",
            "BBC",
        ),
        _article(
            "https://tass.ru/kyiv-operation",
            "Russian forces conduct precision strike on military infrastructure in Kyiv",
            "TASS",
        ),
        _article(
            "https://sciencedaily.com/neanderthal",
            "New Neanderthal DNA study rewrites human migration timeline",
            "ScienceDaily",
        ),
    ]

    stories = await cluster(articles)

    # Should produce at least 1 story (Ukraine strike should always be grouped)
    assert len(stories) >= 1, f"Expected at least 1 story, got {stories}"

    # The Ukraine story should have 2 source views (BBC + TASS merged)
    ukraine_story = next(
        (s for s in stories if len(s.source_views) > 1),
        None,
    )
    assert ukraine_story is not None, "Expected a story with multiple source views"
    assert len(ukraine_story.source_views) == 2

    # Each story has a non-empty neutral headline
    for story in stories:
        assert story.headline.strip()
        for view in story.source_views:
            assert view.url
            assert view.source
            assert view.summary


@pytest.mark.integration
async def test_cluster_solo_articles_each_become_story() -> None:
    """Completely unrelated articles should each be their own story."""
    articles = [
        _article("https://a.com/1", "Volcano erupts in Iceland", "Reuters"),
        _article("https://b.com/2", "Ancient Roman ship found off Sicily", "AP"),
        _article("https://c.com/3", "New quantum computing record set", "Nature"),
    ]
    stories = await cluster(articles)

    # All unrelated articles should each become their own story
    assert len(stories) == 3
