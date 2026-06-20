"""Tests for elephant/digest.py.

Offline tests cover prompt formatting.
Integration tests fire a real LLM call to verify digest generation.

    uv run pytest scripts/tests/test_elephant_digest.py              # offline
    uv run pytest -m integration scripts/tests/test_elephant_digest.py
"""

from __future__ import annotations

import pytest

from elephant.digest import DigestOutput, _format_prompt, generate
from elephant.models import SourceView, Story


def _story(headline: str, *views: tuple[str, str, str, list[str]]) -> Story:
    """Helper: (source, url, summary, signals) tuples → Story."""
    return Story(
        headline=headline,
        source_views=[
            SourceView(source=s, url=u, summary=t, signals=sigs) for s, u, t, sigs in views
        ],
    )


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


def test_format_prompt_includes_headline() -> None:
    story = _story(
        "Missiles strike capital",
        ("BBC", "https://bbc.com/a", "Three civilians killed.", []),
    )
    prompt = _format_prompt([story])
    assert "Missiles strike capital" in prompt
    assert "BBC" in prompt
    assert "Three civilians killed" in prompt


def test_format_prompt_includes_signals() -> None:
    story = _story(
        "Strike on Kyiv",
        (
            "TASS",
            "https://tass.ru/a",
            "Precision operation conducted.",
            ["loaded vocabulary: 'precision'"],
        ),
    )
    prompt = _format_prompt([story])
    assert "loaded vocabulary" in prompt
    assert "precision" in prompt


def test_format_prompt_multiple_stories() -> None:
    stories = [
        _story("Story A", ("Reuters", "https://r.com", "Fact A.", [])),
        _story("Story B", ("AP", "https://ap.com", "Fact B.", [])),
    ]
    prompt = _format_prompt(stories)
    assert "Story A" in prompt
    assert "Story B" in prompt
    assert "Story 1" in prompt
    assert "Story 2" in prompt


def test_format_prompt_no_signals_omits_signals_line() -> None:
    story = _story("Headline", ("AP", "https://ap.com", "Just facts.", []))
    prompt = _format_prompt([story])
    assert "Rhetoric signals" not in prompt


def test_digest_output_model_defaults() -> None:
    d = DigestOutput(main="Hello world")
    assert d.overflow is None


# ---------------------------------------------------------------------------
# Integration: real LLM call
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_generate_returns_valid_structure() -> None:
    stories = [
        _story(
            "Russia strikes Kyiv",
            (
                "BBC",
                "https://bbc.com/a",
                "Russia fired missiles at Kyiv, killing three civilians.",
                ["passive construction: 'were killed' — by whom?"],
            ),
            (
                "TASS",
                "https://tass.ru/b",
                "Russia conducted a precision strike on military targets.",
                ["loaded vocabulary: 'precision strike'"],
            ),
        ),
    ]
    result = await generate(stories)

    assert isinstance(result, DigestOutput)
    assert result.main.strip()
    word_count = len(result.main.split())
    assert word_count <= 900, f"main is {word_count} words — should be ≤ 800"


@pytest.mark.integration
async def test_generate_empty_stories_returns_short_message() -> None:
    """No stories → short 'nothing new today' message with no invented specifics.

    The LLM has zero information about what was searched, so it must not
    fabricate dates, months, or source types/names — that's a hallucination,
    not a digest.
    """
    result = await generate([])

    assert result.main.strip()
    word_count = len(result.main.split())
    assert word_count <= 200, f"No-news message should be short, got {word_count} words"

    lowered = result.main.lower()
    months = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    for forbidden in ("aggregator", *months):
        assert forbidden not in lowered, f"hallucinated detail {forbidden!r} in {result.main!r}"


@pytest.mark.integration
async def test_generate_respects_feedback_notes() -> None:
    """Feedback notes should influence the output style."""
    stories = [
        _story(
            "New space telescope launched",
            ("NASA", "https://nasa.gov/a", "Artemis telescope launched successfully.", []),
        ),
    ]
    notes = "User prefers very short digests. No more than 3 sentences per story."

    result = await generate(stories, feedback_notes=notes)
    assert result.main.strip()
    assert len(result.main.split()) <= 600


@pytest.mark.integration
async def test_generate_in_russian() -> None:
    """Digest should be written in the requested language."""
    stories = [
        _story(
            "Volcano erupts in Iceland",
            ("BBC", "https://bbc.com/v", "A volcano erupted on Reykjanes Peninsula.", []),
        ),
    ]
    result = await generate(stories, language="Russian")
    assert result.main.strip()
    # Rough check: Cyrillic characters should appear
    assert any("Ѐ" <= c <= "ӿ" for c in result.main), "Expected Cyrillic in Russian output"
