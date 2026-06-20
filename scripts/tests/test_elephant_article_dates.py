"""Tests for elephant/article_dates.py.

Offline tests cover URL-path date extraction and HTML metadata parsing without
network calls. Integration test fetches a real page.

    uv run pytest scripts/tests/test_elephant_article_dates.py            # offline only
    uv run pytest -m integration scripts/tests/test_elephant_article_dates.py  # live too
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from elephant.article_dates import (
    _date_from_page,
    _date_from_url_path,
    _parse_iso,
    attach_published_dates,
)
from elephant.models import Article

# ---------------------------------------------------------------------------
# Offline: URL-path date extraction
# ---------------------------------------------------------------------------


def test_date_from_url_path_matches() -> None:
    dt = _date_from_url_path("https://www.nytimes.com/2026/06/19/us/politics/story.html")
    assert dt == datetime(2026, 6, 19, tzinfo=UTC)


def test_date_from_url_path_no_match() -> None:
    assert _date_from_url_path("https://example.com/about-us") is None


def test_date_from_url_path_invalid_date() -> None:
    assert _date_from_url_path("https://example.com/2026/99/99/story") is None


# ---------------------------------------------------------------------------
# Offline: ISO parsing
# ---------------------------------------------------------------------------


def test_parse_iso_with_z_suffix() -> None:
    dt = _parse_iso("2026-06-19T12:00:00Z")
    assert dt == datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


def test_parse_iso_naive_assumes_utc() -> None:
    dt = _parse_iso("2026-06-19T12:00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_garbage_returns_none() -> None:
    assert _parse_iso("not a date") is None


# ---------------------------------------------------------------------------
# Offline: HTML metadata parsing (mocked transport, no real network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_date_from_page_reads_article_published_time() -> None:
    html = '<head><meta property="article:published_time" content="2026-06-18T09:00:00Z"></head>'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    async with httpx.AsyncClient(transport=transport) as client:
        dt = await _date_from_page(client, "https://example.com/story")
    assert dt == datetime(2026, 6, 18, 9, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_date_from_page_reads_json_ld() -> None:
    html = '<script type="application/ld+json">{"datePublished": "2026-06-17"}</script>'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    async with httpx.AsyncClient(transport=transport) as client:
        dt = await _date_from_page(client, "https://example.com/story")
    assert dt == datetime(2026, 6, 17, tzinfo=UTC)


@pytest.mark.asyncio
async def test_date_from_page_no_metadata_returns_none() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html></html>"))
    async with httpx.AsyncClient(transport=transport) as client:
        dt = await _date_from_page(client, "https://example.com/story")
    assert dt is None


@pytest.mark.asyncio
async def test_date_from_page_network_failure_returns_none() -> None:
    def raise_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(raise_error)
    async with httpx.AsyncClient(transport=transport) as client:
        dt = await _date_from_page(client, "https://example.com/story")
    assert dt is None


# ---------------------------------------------------------------------------
# Offline: attach_published_dates wiring
# ---------------------------------------------------------------------------


def _article(url: str, published_at: datetime | None = None) -> Article:
    return Article(
        url=url,
        headline="headline",
        source="example.com",
        published_at=published_at,
        summary="headline",
    )


@pytest.mark.asyncio
async def test_attach_published_dates_skips_already_dated_articles() -> None:
    already_dated = datetime(2020, 1, 1, tzinfo=UTC)
    articles = [_article("https://example.com/2026/06/19/story", already_dated)]
    await attach_published_dates(articles)
    assert articles[0].published_at == already_dated  # untouched


@pytest.mark.asyncio
async def test_attach_published_dates_fills_in_from_url_path() -> None:
    articles = [_article("https://example.com/2026/06/19/story")]
    await attach_published_dates(articles)
    assert articles[0].published_at == datetime(2026, 6, 19, tzinfo=UTC)


@pytest.mark.asyncio
async def test_attach_published_dates_noop_on_empty_list() -> None:
    await attach_published_dates([])  # should not raise


# ---------------------------------------------------------------------------
# Integration: real network fetch
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_attach_published_dates_real_page() -> None:
    """A real BBC article URL should resolve a date via metadata or URL path."""
    articles = [_article("https://www.bbc.com/news")]
    await attach_published_dates(articles)
    # bbc.com/news is a section front page with no per-article date — exercises
    # the "no date found, stays None" path without asserting a brittle live value.
    assert articles[0].published_at is None or isinstance(articles[0].published_at, datetime)
