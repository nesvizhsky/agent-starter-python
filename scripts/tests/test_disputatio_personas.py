"""Tests for disputatio/personas.py.

All offline — no LLM calls, no R2. The avatar URLs require R2_PUBLIC_BASE_URL
to be set but the URL string itself is always constructable.

    uv run pytest scripts/tests/test_disputatio_personas.py
"""

from __future__ import annotations

import re

from disputatio.personas import PERSONAS, Persona, all_keys, avatar_url, pick


def test_exactly_16_personas() -> None:
    assert len(PERSONAS) == 16


def test_all_keys_unique() -> None:
    keys = [p.key for p in PERSONAS]
    assert len(keys) == len(set(keys))


def test_all_keys_are_slugs() -> None:
    """Keys must be lowercase alphanumeric + underscore (safe for R2 paths and DB storage)."""
    for p in PERSONAS:
        assert re.fullmatch(r"[a-z0-9_]+", p.key), f"Bad key: {p.key!r}"


def test_all_personas_have_non_empty_fields() -> None:
    for p in PERSONAS:
        assert p.name.strip(), f"{p.key}: empty name"
        assert p.intro.strip(), f"{p.key}: empty intro"
        assert p.voice.strip(), f"{p.key}: empty voice"


def test_pick_returns_persona() -> None:
    result = pick()
    assert isinstance(result, Persona)


def test_pick_pinned_returns_correct_persona() -> None:
    for p in PERSONAS:
        result = pick(pinned=p.key)
        assert result.key == p.key


def test_pick_invalid_pinned_falls_back_to_random() -> None:
    result = pick(pinned="nonexistent_key")
    assert isinstance(result, Persona)


def test_pick_none_is_random() -> None:
    # Call many times — should not always return the same persona
    results = {pick().key for _ in range(50)}
    assert len(results) > 1, "pick() appears to always return the same persona"


def test_avatar_url_contains_key() -> None:
    for p in PERSONAS:
        url = avatar_url(p)
        assert p.key in url
        assert url.endswith(".png")


def test_all_keys_returns_all() -> None:
    keys = all_keys()
    assert set(keys) == {p.key for p in PERSONAS}
