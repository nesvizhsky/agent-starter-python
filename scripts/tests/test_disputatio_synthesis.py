"""Tests for disputatio/synthesis.py.

Offline tests cover prompt formatting and the thin-coverage guard.
Integration test fires a real LLM call with synthetic digest history.

    uv run pytest scripts/tests/test_disputatio_synthesis.py              # offline
    uv run pytest -m integration scripts/tests/test_disputatio_synthesis.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from disputatio.models import Digest
from disputatio.synthesis import SynthesisOutput, _format_prompt, generate


def _digest(content: str, persona: str = "socrates", days_ago: int = 0) -> Digest:
    return Digest(
        id=uuid4(),
        topic_id=uuid4(),
        telegram_id=999,
        content=content,
        persona=persona,
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


def test_format_prompt_includes_topic_and_content() -> None:
    digests = [
        _digest("Ceasefire talks started Monday.", "socrates", days_ago=3),
        _digest("Talks collapsed by Wednesday.", "terminator", days_ago=1),
    ]
    prompt = _format_prompt("Ukraine war", digests)
    assert "Ukraine war" in prompt
    assert "Ceasefire talks started" in prompt
    assert "Talks collapsed" in prompt
    assert "socrates" in prompt
    assert "terminator" in prompt


def test_format_prompt_includes_dates() -> None:
    digests = [_digest("Some news.", days_ago=5)]
    prompt = _format_prompt("Test topic", digests)
    # Should contain a date string in YYYY-MM-DD format
    import re

    assert re.search(r"\d{4}-\d{2}-\d{2}", prompt)


async def test_generate_returns_none_for_single_digest() -> None:
    result = await generate("Any topic", [_digest("Just one digest.")])
    assert result is None


async def test_generate_returns_none_for_empty() -> None:
    result = await generate("Any topic", [])
    assert result is None


def test_synthesis_output_structure() -> None:
    s = SynthesisOutput(
        contested_facts=["Claim A was disputed"],
        confirmed_facts=["Fact B was confirmed"],
        narrative_drift="The story shifted from X to Y.",
        source_patterns=["RT consistently omitted civilian casualties"],
        summary="A week of coverage revealed Z.",
    )
    assert len(s.contested_facts) == 1
    assert s.narrative_drift
    assert s.summary


# ---------------------------------------------------------------------------
# Integration: real LLM call
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_generate_produces_valid_synthesis() -> None:
    """Feed synthetic digests and verify all output fields are populated."""
    digests = [
        _digest(
            "Monday: Ceasefire talks opened in Geneva. Russia demanded NATO withdrawal "
            "from eastern Europe. Ukraine said this was a non-starter. BBC reported "
            "shelling continued despite talks. TASS called it a 'constructive first session'.",
            persona="socrates",
            days_ago=6,
        ),
        _digest(
            "Wednesday: Talks paused after Russia accused Ukraine of violating the "
            "provisional agreement. Ukraine denied any violation. The UN expressed concern. "
            "Russian state media called the pause 'expected given Western pressure'.",
            persona="terminator",
            days_ago=4,
        ),
        _digest(
            "Friday: Talks collapsed. Both sides blamed each other. Western governments "
            "announced new sanctions. Russian oil prices fell 3%. Independent analysts said "
            "the failure was predictable given the structural gap on territorial demands.",
            persona="monk",
            days_ago=2,
        ),
    ]

    result = await generate("Ukraine-Russia ceasefire negotiations", digests)

    assert result is not None
    assert isinstance(result, SynthesisOutput)

    # All fields populated
    assert result.contested_facts, "Expected at least one contested fact"
    assert result.confirmed_facts, "Expected at least one confirmed fact"
    assert result.narrative_drift.strip()
    assert result.source_patterns, "Expected at least one source pattern"
    assert result.summary.strip()

    # Narrative drift should reference the collapse or the shift from talks to breakdown
    drift_lower = result.narrative_drift.lower()
    assert any(
        word in drift_lower for word in ("collapse", "broke", "failed", "shifted", "escalat")
    ), f"narrative_drift doesn't seem to capture the week's arc: {result.narrative_drift[:200]}"
