"""Tests for elephant/perspectives.py.

Offline tests cover helpers and the recovery safeguard.
Integration test fires a real LLM call to verify clustering.

    uv run pytest scripts/tests/test_elephant_perspectives.py             # offline
    uv run pytest -m integration scripts/tests/test_elephant_perspectives.py
"""

from __future__ import annotations

import pytest

from elephant.models import Article, SourceView, Story
from elephant.perspectives import (
    _cosine_similarity,
    _format_prompt,
    _merge_stories_by_similarity,
    cluster,
)


def _article(url: str, headline: str, source: str) -> Article:
    return Article(url=url, headline=headline, source=source, published_at=None, summary=headline)


def _story(headline: str, *sources: str) -> Story:
    return Story(
        headline=headline,
        source_views=[
            SourceView(source=s, url=f"https://{s.lower()}.example/a", summary=headline)
            for s in sources
        ],
    )


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


def test_format_prompt_strict_relevance_filter_when_topic_given() -> None:
    """When a topic is provided, the prompt must instruct the LLM to reject
    articles that are merely geographically or thematically adjacent — not just
    'not directly related'. The filter language drives correctness here."""
    articles = [_article("https://bbc.com/a", "Some headline", "BBC")]
    prompt = _format_prompt(articles, topic_name="Ukraine War", topic_description="military events")
    lower = prompt.lower()
    # Must include the "would exist without the topic" test
    assert "if this topic didn't exist" in lower or "topic didn't exist" in lower
    # Must not use weak language like "tangentially"
    assert "tangentially" not in lower


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


def test_cosine_similarity_identical_vectors() -> None:
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors() -> None:
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector_returns_zero() -> None:
    """Guards against a division by zero rather than raising."""
    assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_merge_stories_combines_near_duplicates() -> None:
    """Regression: dedup.py only checks new articles against history, never
    against each other within the same batch — two Meduza articles about the
    same event, worded just differently enough, both passed clustering as
    separate stories in production. This merge pass is the safety net."""
    stories = [
        _story("Satellite images confirm damage to Voronezh plant", "Meduza"),
        _story("Satellite images show two buildings damaged at Voronezh facility", "Meduza"),
    ]
    # Near-identical embeddings (as a real near-duplicate pair would have)
    embeddings = [[1.0, 0.0, 0.0], [0.99, 0.01, 0.0]]
    merged = _merge_stories_by_similarity(stories, embeddings)
    assert len(merged) == 1
    assert len(merged[0].source_views) == 1  # same source, deduped


def test_merge_stories_keeps_distinct_stories_separate() -> None:
    stories = [
        _story("Russia fires missiles at Kyiv", "BBC"),
        _story("New Neanderthal DNA study published", "ScienceDaily"),
    ]
    embeddings = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]  # orthogonal — clearly different
    merged = _merge_stories_by_similarity(stories, embeddings)
    assert len(merged) == 2


def test_merge_stories_combines_source_views_from_different_sources() -> None:
    """Same event, different outlets — should merge into one story with both
    source_views, not dedupe one of them away."""
    stories = [
        _story("Russia fires missiles at Kyiv", "BBC"),
        _story("Russia conducts precision strike on Kyiv", "TASS"),
    ]
    embeddings = [[1.0, 0.0], [0.999, 0.001]]
    merged = _merge_stories_by_similarity(stories, embeddings)
    assert len(merged) == 1
    assert {v.source for v in merged[0].source_views} == {"BBC", "TASS"}


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


@pytest.mark.integration
async def test_cluster_rejects_geographically_adjacent_off_topic_articles() -> None:
    """Articles from the same country/region but NOT about the specific topic
    must be filtered out by the relevance rule.

    Regression: production digest for 'Война в Украине' included a Russia-Rwanda
    nuclear deal and EU gas storage — both set in/around Russia/Europe but neither
    a direct development of the Ukraine war itself.
    """
    articles = [
        _article(
            "https://bbc.com/ukraine-strike",
            "Russia fires missiles at Kyiv, killing 3",
            "BBC",
        ),
        _article(
            "https://tass.ru/kyiv-operation",
            "Russia conducts precision strike on military targets in Kyiv",
            "TASS",
        ),
        # These two share the same country/region but are NOT about the Ukraine war
        _article(
            "https://rt.com/rwanda",
            "Russia and Rwanda sign nuclear cooperation roadmap",
            "RT",
        ),
        _article(
            "https://reuters.com/eu-gas",
            "EU gas storage reaches 80% ahead of winter season",
            "Reuters",
        ),
    ]

    stories = await cluster(
        articles,
        topic_name="Война в Украине",
        topic_description="военные действия и непосредственные последствия для Украины",
    )

    all_headlines = " ".join(s.headline.lower() for s in stories)
    # On-topic story must be present
    ukraine_keywords = {"kyiv", "strike", "missile", "киев", "удар"}
    assert any(any(kw in s.headline.lower() for kw in ukraine_keywords) for s in stories), (
        f"Expected Ukraine strike story; got: {[s.headline for s in stories]}"
    )
    # Off-topic stories must be absent
    assert "rwanda" not in all_headlines, f"Rwanda story leaked through filter: {all_headlines}"
    assert "gas" not in all_headlines, f"EU gas story leaked through filter: {all_headlines}"
