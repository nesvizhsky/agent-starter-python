"""Tests for elephant/fetch.py.

Offline tests cover XML parsing logic against synthetic sitemap/RSS/Atom content,
without hitting any network. Integration tests hit real outlets (BBC, TASS) —
chosen because scripts/experiments/probe_feed_discovery.py confirmed both expose
a real sitemap, and TASS's specifically lacks `news:title` (exercises the title
recovery path).

    uv run pytest scripts/tests/test_elephant_fetch.py              # offline only
    uv run pytest -m integration scripts/tests/test_elephant_fetch.py  # live too
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from elephant.fetch import (
    _Candidate,
    _extract_excerpt,
    _localname,
    _looks_like_feed,
    _parse_dt,
    _parse_feed,
    _parse_url_entry,
    _prioritize_news,
    fetch_recent,
    resolve_domain,
)
from elephant.models import Article  # noqa: F401 — used for type reference in docstrings below

# ---------------------------------------------------------------------------
# Offline: small helpers
# ---------------------------------------------------------------------------


def test_localname_strips_namespace() -> None:
    assert _localname("{http://www.sitemaps.org/schemas/sitemap/0.9}urlset") == "urlset"
    assert _localname("urlset") == "urlset"


def test_parse_dt_handles_iso_and_rfc822() -> None:
    assert _parse_dt("2026-06-23T10:00:00Z") == datetime(2026, 6, 23, 10, 0, 0, tzinfo=UTC)
    assert _parse_dt("Tue, 23 Jun 2026 10:00:00 GMT") == datetime(2026, 6, 23, 10, 0, 0, tzinfo=UTC)
    assert _parse_dt("not a date") is None


def test_prioritize_news_puts_news_sitemaps_first() -> None:
    urls = [
        "https://x.com/sitemaps/archive.xml",
        "https://x.com/sitemaps/news.xml",
        "https://x.com/sitemaps/video.xml",
    ]
    ordered = _prioritize_news(urls)
    assert ordered[0] == "https://x.com/sitemaps/news.xml"
    assert set(ordered) == set(urls)


def test_prioritize_news_keeps_order_stable_with_no_news_match() -> None:
    urls = ["https://x.com/a.xml", "https://x.com/b.xml"]
    assert _prioritize_news(urls) == urls


# ---------------------------------------------------------------------------
# Offline: feed-discovery acceptance check (regression for the meduza.io bug —
# a 200 + XML content-type with a genuinely empty body was wrongly accepted)
# ---------------------------------------------------------------------------


def test_looks_like_feed_accepts_real_rss_body() -> None:
    assert _looks_like_feed(200, '<?xml version="1.0"?><rss version="2.0">') is True


def test_looks_like_feed_accepts_real_atom_body() -> None:
    assert _looks_like_feed(200, '<feed xmlns="http://www.w3.org/2005/Atom">') is True


def test_looks_like_feed_rejects_empty_body_despite_200() -> None:
    assert _looks_like_feed(200, "") is False


def test_looks_like_feed_rejects_non_200() -> None:
    assert _looks_like_feed(404, '<rss version="2.0">') is False


# ---------------------------------------------------------------------------
# Offline: sitemap entry parsing
# ---------------------------------------------------------------------------


def _xml_element(xml: str):  # noqa: ANN202 — returns defusedxml Element, untyped on purpose
    from defusedxml import ElementTree

    return ElementTree.fromstring(xml)


def test_parse_url_entry_with_news_namespace() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    xml = """
    <url xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
        <loc>https://bbc.com/news/articles/abc123</loc>
        <news:news>
            <news:publication_date>2026-06-23T10:00:00Z</news:publication_date>
            <news:title>Real headline from the sitemap</news:title>
        </news:news>
    </url>
    """
    candidate = _parse_url_entry(_xml_element(xml), since)
    assert candidate is not None
    assert candidate.headline == "Real headline from the sitemap"
    assert candidate.headline_is_real is True
    assert candidate.published_at == datetime(2026, 6, 23, 10, 0, 0, tzinfo=UTC)


def test_parse_url_entry_without_news_namespace_falls_back_to_url() -> None:
    """TASS's sitemap shape: just loc + lastmod, no news: namespace at all."""
    since = datetime(2026, 1, 1, tzinfo=UTC)
    xml = """
    <url>
        <loc>https://tass.com/politics/2149717</loc>
        <lastmod>2026-06-22T10:50:00+03:00</lastmod>
    </url>
    """
    candidate = _parse_url_entry(_xml_element(xml), since)
    assert candidate is not None
    assert candidate.headline_is_real is False
    assert candidate.headline == "2149717"  # url-derived fallback, recovered later via page fetch


def test_parse_url_entry_drops_entries_before_since() -> None:
    since = datetime(2026, 6, 1, tzinfo=UTC)
    xml = """
    <url>
        <loc>https://example.com/old-article</loc>
        <lastmod>2026-01-01T00:00:00Z</lastmod>
    </url>
    """
    assert _parse_url_entry(_xml_element(xml), since) is None


def test_parse_url_entry_skips_missing_loc() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    xml = "<url><lastmod>2026-06-23T00:00:00Z</lastmod></url>"
    assert _parse_url_entry(_xml_element(xml), since) is None


# ---------------------------------------------------------------------------
# Offline: RSS/Atom feed parsing
# ---------------------------------------------------------------------------


def test_parse_feed_rss() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    xml = b"""<?xml version="1.0"?>
    <rss version="2.0">
        <channel>
            <item>
                <title>A real RSS headline</title>
                <link>https://example.com/article-1</link>
                <pubDate>Tue, 23 Jun 2026 10:00:00 GMT</pubDate>
            </item>
            <item>
                <title>Too old</title>
                <link>https://example.com/article-2</link>
                <pubDate>Tue, 23 Jun 2020 10:00:00 GMT</pubDate>
            </item>
        </channel>
    </rss>
    """
    candidates = _parse_feed(xml, since)
    assert len(candidates) == 1
    assert candidates[0].headline == "A real RSS headline"
    assert candidates[0].headline_is_real is True


def test_parse_feed_atom() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    xml = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <title>A real Atom headline</title>
            <link rel="alternate" href="https://example.com/atom-article"/>
            <updated>2026-06-23T10:00:00Z</updated>
        </entry>
    </feed>
    """
    candidates = _parse_feed(xml, since)
    assert len(candidates) == 1
    assert candidates[0].url == "https://example.com/atom-article"
    assert candidates[0].headline == "A real Atom headline"


def test_parse_feed_unknown_root_returns_empty() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_feed(b"<not-a-feed></not-a-feed>", since) == []


def test_parse_feed_malformed_xml_returns_empty() -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_feed(b"<rss><channel><item><broken", since) == []


# ---------------------------------------------------------------------------
# Offline: _Candidate defaults
# ---------------------------------------------------------------------------


def test_candidate_defaults_headline_is_real_true() -> None:
    c = _Candidate(url="https://x.com/a", headline="Real title", published_at=datetime.now(UTC))
    assert c.headline_is_real is True


def test_candidate_excerpt_defaults_to_none() -> None:
    c = _Candidate(url="https://x.com/a", headline="Real title", published_at=datetime.now(UTC))
    assert c.excerpt is None


# ---------------------------------------------------------------------------
# Offline: excerpt extraction ("open the article and read what's inside")
# ---------------------------------------------------------------------------


def test_extract_excerpt_from_og_description() -> None:
    html = '<head><meta property="og:description" content="Russia launched new strikes."></head>'
    assert _extract_excerpt(html) == "Russia launched new strikes."


def test_extract_excerpt_from_name_description() -> None:
    html = '<head><meta name="description" content="A plain meta description."></head>'
    assert _extract_excerpt(html) == "A plain meta description."


def test_extract_excerpt_prefers_og_over_plain_description() -> None:
    html = (
        '<head><meta property="og:description" content="OG version.">'
        '<meta name="description" content="Plain version."></head>'
    )
    assert _extract_excerpt(html) == "OG version."


def test_extract_excerpt_returns_none_when_missing() -> None:
    assert _extract_excerpt("<head><title>No description here</title></head>") is None


# ---------------------------------------------------------------------------
# Integration: real network calls
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_resolve_domain_for_known_outlet() -> None:
    domain = await resolve_domain("BBC")
    assert domain is not None
    assert "bbc" in domain


@pytest.mark.integration
async def test_resolve_domain_unknown_outlet_returns_none() -> None:
    domain = await resolve_domain("Definitely Not A Real News Outlet XYZ123")
    assert domain is None


@pytest.mark.integration
async def test_fetch_recent_bbc_returns_real_dated_articles() -> None:
    """BBC has a confirmed news sitemap with real titles — should return relevant,
    dated, real-URL articles for a broad topic with no LLM-recall guessing."""
    since = datetime.now(UTC) - timedelta(hours=72)
    articles = await fetch_recent("BBC", "football news", "football transfers and results", since)
    assert articles is not None, "BBC should have a usable sitemap"
    for a in articles:
        assert a.url.startswith("http")
        assert a.headline
        assert a.published_at is not None
        assert a.published_at >= since
    # At least some real articles should have a recovered excerpt ("read what's
    # inside") rather than every single one falling back to context=None.
    with_excerpt = [a for a in articles if a.context]
    assert with_excerpt, "expected at least one article with a recovered excerpt"


@pytest.mark.integration
async def test_fetch_recent_tass_recovers_titles() -> None:
    """TASS's sitemap has no news:title at all — this exercises the title-recovery
    page-fetch path, confirmed necessary by scripts/experiments/probe_feed_discovery.py."""
    since = datetime.now(UTC) - timedelta(hours=72)
    articles = await fetch_recent(
        "TASS", "Russia-Ukraine war", "military operations and political developments", since
    )
    assert articles is not None, "TASS should have a usable sitemap"
    for a in articles:
        # A recovered title is real prose, not the URL-derived numeric-ID fallback.
        assert not a.headline.isdigit(), f"Got an unrecovered fallback headline: {a.headline!r}"


@pytest.mark.integration
async def test_fetch_recent_meduza_finds_real_feed_not_empty_routing_artifact() -> None:
    """Regression test: meduza.io's /rss/all/all/ 200s with an XML content-type but
    a genuinely empty body — _looks_like_feed() must reject it and discovery must
    keep trying until it finds the real /rss/all feed. Found via a live test against
    a real tracked topic in the dev bot's database, not a synthetic case."""
    since = datetime.now(UTC) - timedelta(hours=72)
    articles = await fetch_recent(
        "медуза", "Russia-Ukraine war", "military operations and political developments", since
    )
    assert articles is not None, "Meduza should have a usable RSS feed"
    assert len(articles) > 0, "Meduza publishes daily on this topic — zero is a regression"


@pytest.mark.integration
async def test_fetch_recent_returns_none_for_outlet_with_no_feed() -> None:
    since = datetime.now(UTC) - timedelta(hours=72)
    articles = await fetch_recent(
        "Definitely Not A Real News Outlet XYZ123", "any topic", "any description", since
    )
    assert articles is None
