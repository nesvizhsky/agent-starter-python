"""Recover a real publication date for a cited article URL.

Perplexity (via OpenRouter) only returns `url` + `title` per citation — no date —
so the freshness filter in dedup.py had nothing to check against and old articles
could slip into a digest meant to cover a narrow recent window. This fills that gap
for free, using two techniques every major news CMS already supports for SEO:

1. The URL path itself (most outlets embed the date, e.g. "/2026/06/19/...").
2. Standard publish-date metadata in the page <head> (article:published_time,
   JSON-LD datePublished, or a <time datetime=...> tag) — one short GET per URL.

If neither works, the URL is left undated (same as today's behavior) rather than
guessed at or dropped outright — most sites do have one of these, so this should
be the exception, not the rule.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from loguru import logger

from elephant.models import Article

_FETCH_TIMEOUT = 5.0
_MAX_CONCURRENT_FETCHES = 10
_HEAD_BYTES_TO_SCAN = 20_000  # publish-date metadata always lives in <head>

_URL_DATE_RE = re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)")

_META_DATE_PATTERNS = (
    re.compile(r'property=["\']article:published_time["\']\s+content=["\']([^"\']+)', re.I),
    re.compile(r'name=["\']date["\']\s+content=["\']([^"\']+)', re.I),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
    re.compile(r"<time[^>]+datetime=[\"']([^\"']+)", re.I),
)


def _parse_iso(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _date_from_url_path(url: str) -> datetime | None:
    match = _URL_DATE_RE.search(urlparse(url).path)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


async def _date_from_page(client: httpx.AsyncClient, url: str) -> datetime | None:
    try:
        resp = await client.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 — any network failure just means "unknown"
        logger.debug("date fetch failed for {!r}: {}", url, exc)
        return None
    head = resp.text[:_HEAD_BYTES_TO_SCAN]
    for pattern in _META_DATE_PATTERNS:
        match = pattern.search(head)
        if match:
            parsed = _parse_iso(match.group(1))
            if parsed:
                return parsed
    return None


async def _date_for_url(
    client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore
) -> datetime | None:
    from_path = _date_from_url_path(url)
    if from_path:
        return from_path
    async with semaphore:
        return await _date_from_page(client, url)


async def attach_published_dates(articles: list[Article]) -> None:
    """Fill in `published_at` for each article that has none, in place.

    Fetches are capped at _MAX_CONCURRENT_FETCHES at a time and fail open per-URL
    (a fetch error just leaves published_at=None — same as before this existed).
    """
    undated = [a for a in articles if a.published_at is None]
    if not undated:
        return

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)
    async with httpx.AsyncClient() as client:
        dates = await asyncio.gather(*[_date_for_url(client, a.url, semaphore) for a in undated])

    found = 0
    for article, dt in zip(undated, dates, strict=True):
        if dt:
            article.published_at = dt
            found += 1

    logger.info("article_dates: resolved {} of {} undated articles", found, len(undated))
