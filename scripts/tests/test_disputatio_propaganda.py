"""Tests for disputatio/propaganda.py.

Offline tests cover prompt formatting and signal-merging logic.
Integration tests fire a real LLM call to verify signal extraction.

    uv run pytest scripts/tests/test_disputatio_propaganda.py              # offline
    uv run pytest -m integration scripts/tests/test_disputatio_propaganda.py
"""

from __future__ import annotations

import pytest

from disputatio.models import SourceView, Story
from disputatio.propaganda import SourceSignals, _format_prompt, _merge, analyze


def _story(headline: str, *views: tuple[str, str, str]) -> Story:
    """Helper: (source, url, summary) tuples → Story."""
    return Story(
        headline=headline,
        source_views=[SourceView(source=s, url=u, summary=t) for s, u, t in views],
    )


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


def test_format_prompt_includes_all_sources() -> None:
    story = _story(
        "Missiles strike capital",
        ("BBC", "https://bbc.com/a", "Russia fired missiles at Kyiv, killing three civilians."),
        ("TASS", "https://tass.ru/b", "Russia conducted a precision strike on military targets."),
    )
    prompt = _format_prompt(story)
    assert "BBC" in prompt
    assert "TASS" in prompt
    assert "precision strike" in prompt
    assert "killing three civilians" in prompt
    assert "Missiles strike capital" in prompt


def test_merge_fills_signals() -> None:
    story = _story(
        "Strike on capital",
        ("BBC", "https://bbc.com/a", "Civilians killed."),
        ("TASS", "https://tass.ru/b", "Precision military operation conducted."),
    )
    analyses = [
        SourceSignals(
            source="BBC",
            signals=["passive construction: 'civilians killed' — by whom?"],
        ),
        SourceSignals(
            source="TASS",
            signals=["loaded vocabulary: 'precision' implies surgical accuracy"],
        ),
    ]
    _merge(story, analyses)
    assert story.source_views[0].signals == ["passive construction: 'civilians killed' — by whom?"]
    assert story.source_views[1].signals == [
        "loaded vocabulary: 'precision' implies surgical accuracy"
    ]


def test_merge_case_insensitive_fallback() -> None:
    """LLM returns 'bbc' but the source_view has 'BBC' — should still match."""
    story = _story(
        "Headline",
        ("BBC", "https://bbc.com/a", "Some text."),
    )
    analyses = [SourceSignals(source="bbc", signals=["one-sided framing"])]
    _merge(story, analyses)
    assert story.source_views[0].signals == ["one-sided framing"]


def test_merge_unknown_source_leaves_empty() -> None:
    """If the model returns an extra source we don't recognise, views stay empty."""
    story = _story(
        "Headline",
        ("Reuters", "https://reuters.com/a", "Facts."),
    )
    analyses = [SourceSignals(source="SomeUnknownOutlet", signals=["x"])]
    _merge(story, analyses)
    assert story.source_views[0].signals == []


def test_merge_no_signals_leaves_empty_list() -> None:
    story = _story(
        "Headline",
        ("AP", "https://ap.com/a", "Just the facts."),
    )
    analyses = [SourceSignals(source="AP", signals=[])]
    _merge(story, analyses)
    assert story.source_views[0].signals == []


async def test_analyze_empty_returns_empty() -> None:
    result = await analyze([])
    assert result == []


# ---------------------------------------------------------------------------
# Integration: real LLM call
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_analyze_finds_signals_in_biased_text() -> None:
    """BBC vs TASS coverage of the same event — at least one source should have signals."""
    story = _story(
        "Russian forces strike Kyiv",
        (
            "BBC",
            "https://bbc.com/ukraine",
            "Russia fired a barrage of missiles at Kyiv overnight, killing at least three "
            "civilians and wounding dozens more in what Ukrainian officials called a deliberate "
            "terror attack on residential areas.",
        ),
        (
            "TASS",
            "https://tass.ru/kyiv",
            "Russian armed forces conducted a precision strike against military infrastructure "
            "facilities in Kyiv. The Ministry of Defence confirmed all targets were of military "
            "nature. The operation achieved its stated objectives.",
        ),
    )

    results = await analyze([story])

    assert len(results) == 1
    updated = results[0]

    # Every view should have had signals attempted (list may be empty for neutral text)
    for view in updated.source_views:
        assert isinstance(view.signals, list)

    # Between BBC and TASS on this topic, we expect at least one signal across both
    all_signals = [s for view in updated.source_views for s in view.signals]
    assert len(all_signals) >= 1, (
        "Expected at least one rhetorical signal across BBC and TASS coverage; "
        f"got: {[v.signals for v in updated.source_views]}"
    )


@pytest.mark.integration
async def test_analyze_neutral_text_returns_valid_structure() -> None:
    """A plain factual report — the agent returns valid structure (may have few signals)."""
    story = _story(
        "Archaeological discovery in Sicily",
        (
            "AP",
            "https://ap.com/archaeology",
            "Archaeologists announced Tuesday the discovery of a Roman-era shipwreck "
            "off the coast of Sicily. The vessel, estimated to date from the 1st century BCE, "
            "was found at a depth of 80 metres. Researchers said initial analysis suggests "
            "it carried amphorae, possibly wine or olive oil.",
        ),
    )

    results = await analyze([story])

    assert len(results) == 1
    view = results[0].source_views[0]
    assert isinstance(view.signals, list)
    # Each signal, if any, should be a non-empty string
    for sig in view.signals:
        assert isinstance(sig, str) and sig.strip()


@pytest.mark.integration
async def test_analyze_multiple_stories_concurrently() -> None:
    """Two unrelated stories processed together — both get valid output."""
    stories = [
        _story(
            "Ceasefire talks begin",
            ("Reuters", "https://reuters.com/a", "Peace negotiators met in Geneva on Monday."),
            (
                "RT",
                "https://rt.com/b",
                "Russian delegation arrived in Geneva for crucial peace negotiations "
                "to end the conflict provoked by NATO expansion.",
            ),
        ),
        _story(
            "New volcano eruption in Iceland",
            (
                "BBC",
                "https://bbc.com/iceland",
                "A volcano erupted on Iceland's Reykjanes "
                "Peninsula, prompting evacuations of nearby towns.",
            ),
        ),
    ]

    results = await analyze(stories)

    assert len(results) == 2
    for story in results:
        for view in story.source_views:
            assert isinstance(view.signals, list)

    # RT's framing ("provoked by NATO expansion") should trigger at least one signal
    rt_view = next(v for v in results[0].source_views if v.source == "RT")
    assert len(rt_view.signals) >= 1, f"Expected signals on RT text, got: {rt_view.signals}"
