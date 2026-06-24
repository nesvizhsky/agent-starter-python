"""Offline tests for elephant/bot.py — pure-function logic only, no Telegram API.

uv run pytest scripts/tests/test_elephant_bot.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from telegram import Bot

from elephant import jobs
from elephant.bot import (
    _filter_valid_sources,
    _is_bare_platform_domain,
    _run_digest_guarded,
    _split_sources,
)
from elephant.models import Topic


def _topic() -> Topic:
    return Topic(
        id=uuid4(),
        telegram_id=1,
        name="Test",
        description=None,
        timezone="UTC",
        paused=False,
        sources=[],
        excluded_sources=[],
        trusted_sources=[],
        feedback_notes=None,
        source_guidance=None,
        created_at=datetime.now(UTC),
        last_sent_at=None,
        slots=[],
    )


class _FakeBot:
    async def send_message(self, *args: object, **kwargs: object) -> None:
        pass


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


async def test_run_digest_guarded_rejects_concurrent_check_for_same_topic(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression: two overlapping "Check" taps for the same topic used to both
    run the pipeline, both read store.get_seen_urls() before either had
    written back, and both send a digest — confirmed in production via a
    duplicate elephant_seen row for the same URL, 40s apart. The second
    concurrent call must bail out instead of racing the first."""
    started = asyncio.Event()

    async def _slow_digest(topic: Topic, bot: object, *, slot: object = None) -> None:
        started.set()
        await asyncio.sleep(0.2)

    monkeypatch.setattr(jobs, "_run_digest", _slow_digest)

    topic = _topic()
    bot = cast(Bot, _FakeBot())

    first = asyncio.ensure_future(_run_digest_guarded(topic, bot, topic.telegram_id, "English"))
    await started.wait()
    second = await _run_digest_guarded(topic, bot, topic.telegram_id, "English")

    assert second is False  # bailed out while the first was still running
    assert await first is True  # the first one actually ran to completion


async def test_run_digest_guarded_allows_sequential_checks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A second check AFTER the first has finished should run normally —
    the lock must release, not stay held forever."""

    async def _fast_digest(topic: Topic, bot: object, *, slot: object = None) -> None:
        pass

    monkeypatch.setattr(jobs, "_run_digest", _fast_digest)

    topic = _topic()
    bot = cast(Bot, _FakeBot())
    assert await _run_digest_guarded(topic, bot, topic.telegram_id, "English") is True
    assert await _run_digest_guarded(topic, bot, topic.telegram_id, "English") is True
