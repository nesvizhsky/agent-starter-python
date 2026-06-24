"""Offline tests for elephant/bot.py — pure-function logic only, no Telegram API.

uv run pytest scripts/tests/test_elephant_bot.py
"""

from __future__ import annotations

from elephant.bot import _filter_valid_sources, _is_bare_platform_domain, _split_sources


def test_split_sources_on_commas() -> None:
    assert _split_sources("BBC, TASS, Al Jazeera") == ["BBC", "TASS", "Al Jazeera"]


def test_split_sources_on_newlines() -> None:
    """Regression: a user pasting one source per line (no commas) previously
    produced a single unsplit blob, since .split(",") found no separator —
    and a rendered button label silently swallows newlines, showing the
    domains jammed together with nothing visible between them."""
    raw = "ria.ru\ntass.ru\nrbc.ru\nkommersant.ru"
    assert _split_sources(raw) == ["ria.ru", "tass.ru", "rbc.ru", "kommersant.ru"]


def test_split_sources_on_semicolons() -> None:
    assert _split_sources("ria.ru;tass.ru;rbc.ru") == ["ria.ru", "tass.ru", "rbc.ru"]


def test_split_sources_preserves_multi_word_names() -> None:
    """Must NOT split on plain whitespace — some outlets are legitimately
    referred to by a multi-word name."""
    raw = "BBC\nThe Guardian\nAl Jazeera"
    assert _split_sources(raw) == ["BBC", "The Guardian", "Al Jazeera"]


def test_split_sources_strips_and_drops_empties() -> None:
    assert _split_sources(" BBC ,, TASS ,\n\n") == ["BBC", "TASS"]


def test_bare_platform_domain_rejected() -> None:
    assert _is_bare_platform_domain("youtube.com") is True
    assert _is_bare_platform_domain("https://youtube.com/") is True
    assert _is_bare_platform_domain("instagram.com") is True


def test_specific_channel_on_platform_allowed() -> None:
    """A specific channel/account IS trackable — only the bare platform isn't."""
    assert _is_bare_platform_domain("youtube.com/c/SomeNewsChannel") is False


def test_normal_outlet_never_flagged_as_bare_platform() -> None:
    assert _is_bare_platform_domain("BBC") is False
    assert _is_bare_platform_domain("bbc.com") is False


def test_filter_valid_sources_separates_bare_platforms() -> None:
    valid, rejected = _filter_valid_sources(
        ["BBC", "youtube.com", "youtube.com/c/SomeChannel", "instagram.com"]
    )
    assert valid == ["BBC", "youtube.com/c/SomeChannel"]
    assert rejected == ["youtube.com", "instagram.com"]
