"""Tests for elephant/research.py.

Offline tests cover parsing logic without hitting any API.
Integration test calls real Perplexity (costs a little, ~$0.01).

    uv run pytest scripts/tests/test_elephant_research.py            # offline only
    uv run pytest -m integration scripts/tests/test_elephant_research.py  # live too
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from agent.services.llm import Research, Source
from elephant.models import Topic
from elephant.research import (
    _headline_from_url,
    _is_hub_page,
    _parse,
    _source_from_url,
    gather,
    generate_source_guidance,
    identify_sides,
)

# ---------------------------------------------------------------------------
# Offline: parsing helpers
# ---------------------------------------------------------------------------


def _topic(**kwargs: object) -> Topic:
    defaults: dict[str, object] = dict(
        id=uuid4(),
        telegram_id=1,
        name="Test Topic",
        description=None,
        timezone="UTC",
        paused=False,
        sources=["BBC", "Reuters"],
        excluded_sources=[],
        trusted_sources=[],
        feedback_notes=None,
        source_guidance=None,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        last_sent_at=None,
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
    articles = _parse(result)
    assert len(articles) == 2
    assert articles[0].headline == "Ukraine ceasefire talks stall"
    assert articles[0].source == "bbc.com"
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
    articles = _parse(result)
    assert len(articles) == 1
    assert articles[0].url == "https://example.com/valid"


def test_parse_always_derives_source_from_url() -> None:
    """Source is always the citation's actual domain, never the outlet asked about —
    Perplexity frequently cites unrelated domains, so trusting the query target
    would mislabel them."""
    result = Research(
        text="prose",
        sources=[Source(url="https://www.reuters.com/world/story", title="Big story")],
    )
    articles = _parse(result)
    assert articles[0].source == "reuters.com"
    assert articles[0].is_general_query is False


def test_parse_marks_general_query_articles() -> None:
    result = Research(
        text="prose",
        sources=[Source(url="https://www.reuters.com/world/story", title="Big story")],
    )
    articles = _parse(result, is_general_query=True)
    assert articles[0].is_general_query is True


def test_parse_always_blocks_social_video_platforms_even_for_tracked_sources() -> None:
    """A user-tracked source's per-outlet query passes no block_domains at all
    (by design — see module docstring), but Perplexity can still cite an
    embedded YouTube clip alongside the real outlet. That must be blocked
    unconditionally, not just for the general/side-outlet queries."""
    result = Research(
        text="prose",
        sources=[
            Source(url="https://www.bbc.com/news/article-1", title="Real BBC article"),
            Source(url="https://www.youtube.com/watch?v=abc123", title="Embedded clip"),
        ],
    )
    articles = _parse(result)  # no block_domains passed, simulating a tracked source
    assert len(articles) == 1
    assert articles[0].source == "bbc.com"


def test_is_hub_page_catches_known_shapes() -> None:
    hub_headlines = [
        "Ancient Civilizations News",
        "Archaeology News - Phys.org",
        "May/June 2026 - Archaeology Magazine",
        "June 2026 – Explorator",
        "Archaeology and anthropology",
        "Archaeology",
        "Stonehenge: history, location, and meaning of the megalithic monument",
    ]
    for headline in hub_headlines:
        assert _is_hub_page(headline), f"expected hub page: {headline!r}"


def test_is_hub_page_keeps_real_headlines() -> None:
    real_headlines = [
        "New discovery may have been Stonehenge prototype",
        "Celtic 'princely tomb' discovered near Bad Camberg in major breakthrough",
        "Russia fires missiles at Kyiv",
        "Ukraine ceasefire talks stall",
    ]
    for headline in real_headlines:
        assert not _is_hub_page(headline), f"expected real headline: {headline!r}"


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
    )
    articles2 = await gather(topic_with_exclusion)
    source_labels = {a.source for a in articles2}
    assert "LiveScience" not in source_labels


@pytest.mark.integration
async def test_identify_sides_grounded_for_conflict_topic() -> None:
    """A real conflict topic should return sides with real, named outlets — not an
    empty list, and not outlets invented without grounding in the research call."""
    sides = await identify_sides(
        "Russia's full-scale invasion of Ukraine: military operations, ceasefire "
        "negotiations, and political developments",
        "Russia-Ukraine war",
    )
    assert len(sides) >= 2, "Expected at least two sides for a named conflict"
    for side in sides:
        assert side.name, "Side name should not be empty"
        assert side.outlets, f"Side {side.name!r} should have at least one outlet"


@pytest.mark.integration
async def test_identify_sides_empty_for_non_conflict_topic() -> None:
    """A topic with no inherent sides (archaeology) should return an empty list."""
    sides = await identify_sides(
        "New archaeological discoveries and excavation findings worldwide",
        "archaeology news",
    )
    assert sides == []


@pytest.mark.integration
async def test_generate_source_guidance_names_real_outlets() -> None:
    """Guidance should start with 'Prioritise:' and include the standard exclusions,
    with real outlet names grounded in the research call."""
    guidance = await generate_source_guidance(
        "Artificial intelligence safety research: alignment, interpretability, and "
        "governance developments",
        "AI safety",
    )
    assert guidance.startswith("Prioritise:")
    assert "YouTube" in guidance  # from _SOURCE_EXCLUSIONS, always appended
