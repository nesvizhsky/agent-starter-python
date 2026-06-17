"""Tests for disputatio/perspectives.py.

Offline tests cover helpers and the recovery safeguard.
Integration test fires a real LLM call to verify clustering.

    uv run pytest scripts/tests/test_disputatio_perspectives.py             # offline
    uv run pytest -m integration scripts/tests/test_disputatio_perspectives.py
"""

from __future__ import annotations

import pytest

from disputatio.models import Article, SourceView, Story
from disputatio.perspectives import _format_prompt, _recover_missing, cluster


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
    prompt = _format_prompt(articles)
    assert "BBC" in prompt
    assert "TASS" in prompt
    assert "Ceasefire talks begin" in prompt
    assert "Russia proposes peace" in prompt
    assert "https://bbc.com/a" in prompt


def test_recover_missing_adds_dropped_articles() -> None:
    articles = [
        _article("https://bbc.com/a", "Headline A", "BBC"),
        _article("https://bbc.com/b", "Headline B", "BBC"),
    ]
    # LLM returned only the first article
    stories = [
        Story(
            headline="Headline A",
            source_views=[SourceView(source="BBC", url="https://bbc.com/a", summary="A")],
        )
    ]
    recovered = _recover_missing(articles, stories)
    all_urls = {v.url for s in recovered for v in s.source_views}
    assert "https://bbc.com/a" in all_urls
    assert "https://bbc.com/b" in all_urls
    assert len(recovered) == 2


def test_recover_missing_leaves_complete_output_unchanged() -> None:
    articles = [_article("https://example.com/a", "Story", "Src")]
    stories = [
        Story(
            headline="Story",
            source_views=[SourceView(source="Src", url="https://example.com/a", summary="Story")],
        )
    ]
    recovered = _recover_missing(articles, stories)
    assert len(recovered) == 1


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

    # Should produce 2 stories: Ukraine strike + Neanderthal
    headlines = [s.headline for s in stories]
    assert len(stories) >= 2, f"Expected at least 2 stories, got {len(stories)}: {headlines}"

    # Every input URL must appear in output
    input_urls = {a.url for a in articles}
    output_urls = {v.url for s in stories for v in s.source_views}
    assert input_urls == output_urls, f"Missing: {input_urls - output_urls}"

    # The Ukraine story should have 2 source views (BBC + TASS)
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

    assert len(stories) == 3
    input_urls = {a.url for a in articles}
    output_urls = {v.url for s in stories for v in s.source_views}
    assert input_urls == output_urls
