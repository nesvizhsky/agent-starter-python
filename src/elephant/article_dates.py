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

# Surveyed real undated articles across every active production topic to find
# every URL date shape actually in use, rather than patching one outlet at a
# time as reports trickled in. Four shapes cover the large majority:
#
# 1. /YYYY/MM/DD/ — slash-separated segments (nytimes.com, most WordPress sites)
# 2. YYYY-MM-DD — a dash-joined token anywhere in the path, not necessarily its
#    own segment (newsroom.ibm.com "/2026-06-22-ibm-...", iz.ru "/2026-06-24/",
#    reuters.com "...-2026-06-23/")
# 3. <month-name>-DD-YYYY — a month name (full or abbreviated) embedded in a
#    slug (understandingwar.org "...-june-22-2026/", fdd.org "/june-22-2026/")
# 4. YYYYMMDD — 8 consecutive digits forming a date, bounded so it doesn't
#    match into a longer digit run (butlereagle.com "/20260619/", archive.org
#    "RT_20260623_210000_News", acaps.org "/20260622_ACAPS_...")
# 5. /YYYY/MM/<YYMMDD...>.ext — year/month as normal slash segments, day
#    embedded in a numeric filename whose first 6 digits repeat YY+MM+DD
#    (sciencedaily.com "/releases/2026/06/260603023914.htm")
_URL_DATE_SLASH_RE = re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)")
_URL_DATE_DASH_RE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
_URL_DATE_COMPACT_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
)
_URL_DATE_SLUG_MONTH_RE = re.compile(
    rf"(?<![a-z])({_MONTH_NAMES})-(\d{{1,2}})-(20\d{{2}})(?:/|$|[^0-9a-z])", re.I
)
_URL_DATE_FILENAME_RE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})(\d{2})(\d{2})\d*\.\w+$")
_MONTH_NAME_TO_NUM = {
    name: i + 1
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ]
    )
    for name in names
}

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
    path = urlparse(url).path

    match = _URL_DATE_SLASH_RE.search(path)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            pass

    match = _URL_DATE_DASH_RE.search(path)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            pass

    match = _URL_DATE_SLUG_MONTH_RE.search(path)
    if match:
        month_name, day, year = match.groups()
        try:
            return datetime(int(year), _MONTH_NAME_TO_NUM[month_name.lower()], int(day), tzinfo=UTC)
        except ValueError:
            pass

    match = _URL_DATE_COMPACT_RE.search(path)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            pass

    match = _URL_DATE_FILENAME_RE.search(path)
    if match:
        year_full, month, year_suffix, month_repeat, day = match.groups()
        # Sanity-check the filename's leading YYMMDD against the URL's own
        # year/month segments before trusting it — only trust a real date
        # match, not a coincidental numeric prefix.
        if year_suffix == year_full[2:] and month_repeat == month:
            try:
                return datetime(int(year_full), int(month), int(day), tzinfo=UTC)
            except ValueError:
                pass

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
