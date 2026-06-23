"""Fetch real, currently-published articles directly from an outlet's own site,
instead of asking an LLM to recall what it published — research.py's existing
per-outlet Perplexity query ("what did {outlet} publish recently?") trusts
Perplexity's crawl of that specific outlet, which is documented as unreliable for
some outlets (Russian state media, paywalled sites).

Two real, deterministic discovery methods, tried in order:
1. News sitemap (the Google News Sitemap standard) — exact published_at + real
   headline + URL, no LLM call. Confirmed via scripts/experiments/
   probe_feed_discovery.py that the outlets Perplexity is weakest on (BBC,
   Reuters, TASS) all expose this, even where they killed RSS years ago.
2. RSS/Atom feed at a common path — works well for smaller/tech outlets
   (TechCrunch, Ars Technica, Habr) that never had a reason to drop RSS.

An outlet name (e.g. "TASS") isn't a domain, so the first step is resolving one —
cached globally in elephant_outlet_domains (store.py), verified by a live HTTP
check before being trusted, since it never depends on which topic is asking.

Either method can return far more articles than relate to one topic (a sitemap
mixes sport/weather/politics), so one cheap fast-tier LLM call classifies which
headlines are actually about the topic — classifying real headlines is a much
smaller, lower-risk ask than research.py's old approach of asking a model to
recall article content from memory. For the small set that survives that filter,
a real excerpt (meta description) is fetched from the article page itself — "open
it and read what's inside", not just judge it by headline — giving downstream
clustering/analysis the same kind of real prose Perplexity's path provides.

fetch_recent() returns None when neither method finds a usable feed/sitemap at
all, so callers (research.py) can fall back to the existing Perplexity query —
this is additive, not a replacement. An outlet WITH a working feed but nothing
published in the lookback window correctly returns [], not None.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element  # noqa: S405 — type only; parsing uses defusedxml

import httpx
from defusedxml import ElementTree as _DefusedET
from loguru import logger
from pydantic import BaseModel
from pydantic_ai import Agent

from agent.services.llm import build_model
from elephant import store
from elephant.models import Article

_FETCH_TIMEOUT = 8.0
_MAX_SITEMAP_LEAVES = 5  # bounds cost for high-volume outlets; see known limitation below
_MAX_TOP_LEVEL_SITEMAPS = 3
_MAX_CANDIDATES = 80  # capped before the relevance classifier, for cost + context size
_USER_AGENT = "Mozilla/5.0 (compatible; EatTheElephantBot/1.0)"

_COMMON_FEED_PATHS = (
    "/feed",
    "/feed/",
    "/rss",
    "/rss.xml",
    "/rss/all",
    "/rss/all/all/",
    "/feeds/posts/default",
)


class _Candidate(BaseModel):
    url: str
    headline: str
    published_at: datetime
    # False when `headline` is the URL-derived fallback, not a real title from the
    # feed/sitemap itself (some sitemaps, e.g. TASS's, carry no title at all) — these
    # need a one-off page fetch to recover a real title before relevance filtering
    # can work; an LLM can't judge relevance from an opaque article ID.
    headline_is_real: bool = True
    # Filled in only for candidates that survive relevance filtering — "actually
    # open the article and read what's inside" (a real excerpt), not just headline.
    excerpt: str | None = None


# ---------------------------------------------------------------------------
# Domain resolution
# ---------------------------------------------------------------------------

_domain_guesser: Agent[None, str] = Agent(
    build_model("fast"),
    output_type=str,
    system_prompt=(
        "Given a news outlet name, respond with ONLY its primary homepage domain, "
        "e.g. 'bbc.com' or 'tass.com'. No URL scheme, no path, no explanation. "
        "If you don't recognise the outlet, respond with exactly 'unknown'."
    ),
)


async def _guess_domain(outlet: str) -> str | None:
    try:
        result = await _domain_guesser.run(outlet)
    except Exception:  # noqa: BLE001
        return None
    domain = (
        result.output.strip()
        .lower()
        .removeprefix("https://")
        .removeprefix("http://")
        .removeprefix("www.")
        .rstrip("/")
    )
    return None if domain in ("", "unknown") else domain


async def resolve_domain(outlet: str) -> str | None:
    """Resolve an outlet name to a verified, reachable domain.

    Cached globally (elephant_outlet_domains) — resolved once per outlet name,
    ever, regardless of how many topics track it. The LLM guess is unverified on
    its own, so it's checked with a live HTTP request before being trusted or
    cached; a wrong guess just fails the check and returns None (caller falls
    back to Perplexity), it's never silently cached.
    """
    cached = await store.get_cached_domain(outlet)
    if cached:
        return cached
    guess = await _guess_domain(outlet)
    if not guess:
        return None
    async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}) as client:
        try:
            resp = await client.get(
                f"https://{guess}", timeout=_FETCH_TIMEOUT, follow_redirects=True
            )
        except Exception:  # noqa: BLE001
            logger.debug("domain guess {!r} for {!r} unreachable", guess, outlet)
            return None
        if resp.status_code >= 400:
            logger.debug("domain guess {!r} for {!r} returned {}", guess, outlet, resp.status_code)
            return None
    await store.cache_domain(outlet, guess)
    return guess


# ---------------------------------------------------------------------------
# XML helpers (namespace-agnostic — sitemaps/feeds are inconsistent about
# declaring namespaces on every element)
# ---------------------------------------------------------------------------


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(elem: Element, name: str) -> Element | None:
    for c in elem:
        if _localname(c.tag) == name:
            return c
    return None


def _children(elem: Element | None, name: str) -> list[Element]:
    if elem is None:
        return []
    return [c for c in elem if _localname(c.tag) == name]


def _parse_dt(raw: str) -> datetime | None:
    """ISO 8601 (sitemaps, Atom) or RFC 822 (RSS pubDate) — try both."""
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _headline_from_url(url: str) -> str:
    """Last resort when a feed entry has no title: turn the URL path into one."""
    path = url.rstrip("/").rsplit("/", 1)[-1]
    return path.replace("-", " ").replace("_", " ").title() or url


async def _fetch_xml(client: httpx.AsyncClient, url: str) -> Element | None:
    try:
        resp = await client.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return _DefusedET.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001 — any failure just means "skip this one"
        logger.debug("xml fetch failed for {!r}: {}", url, exc)
        return None


# ---------------------------------------------------------------------------
# News sitemap (Google News Sitemap standard)
# ---------------------------------------------------------------------------


async def _sitemap_urls_from_robots(client: httpx.AsyncClient, domain: str) -> list[str]:
    try:
        resp = await client.get(
            f"https://{domain}/robots.txt", timeout=_FETCH_TIMEOUT, follow_redirects=True
        )
    except Exception:  # noqa: BLE001
        return []
    return [
        line.split(":", 1)[1].strip()
        for line in resp.text.splitlines()
        if line.strip().lower().startswith("sitemap:")
    ]


def _parse_url_entry(url_el: Element, since: datetime) -> _Candidate | None:
    loc = _child(url_el, "loc")
    if loc is None or not loc.text:
        return None

    headline: str | None = None
    published_at: datetime | None = None
    news = _child(url_el, "news")
    if news is not None:
        pub_el = _child(news, "publication_date")
        title_el = _child(news, "title")
        if pub_el is not None and pub_el.text:
            published_at = _parse_dt(pub_el.text)
        if title_el is not None and title_el.text:
            headline = title_el.text.strip()

    if published_at is None:
        lastmod = _child(url_el, "lastmod")
        if lastmod is not None and lastmod.text:
            published_at = _parse_dt(lastmod.text)

    if published_at is None or published_at < since:
        return None

    url = loc.text.strip()
    return _Candidate(
        url=url,
        headline=headline or _headline_from_url(url),
        published_at=published_at,
        headline_is_real=headline is not None,
    )


def _prioritize_news(urls: list[str]) -> list[str]:
    """Outlets typically expose several sitemaps (archive, video, topics, news...);
    only the one(s) naming "news" reliably contain recent articles with real dates —
    others (e.g. a full historical archive) can dominate the candidate list with
    stale or undated entries if walked first. Prefer "news"-named ones, but keep
    the rest as a fallback for outlets that don't follow this naming convention."""
    news_first = [u for u in urls if "news" in u.lower()]
    rest = [u for u in urls if "news" not in u.lower()]
    return news_first + rest


async def _walk_sitemap(
    client: httpx.AsyncClient, sitemap_url: str, since: datetime, depth: int = 0
) -> list[_Candidate]:
    root = await _fetch_xml(client, sitemap_url)
    if root is None:
        return []
    root_tag = _localname(root.tag)

    if root_tag == "sitemapindex":
        if depth >= 1:  # one level of recursion is enough for every outlet we tested
            return []
        # Known limitation: leaf sitemaps aren't reliably ordered by article recency,
        # only by when the leaf file itself was last regenerated — walking only the
        # _MAX_SITEMAP_LEAVES most-recently-modified leaves bounds cost for high-volume
        # outlets, at the risk of missing some in-window articles on very large sites.
        entries: list[tuple[str, datetime | None]] = []
        for sm in _children(root, "sitemap"):
            loc = _child(sm, "loc")
            if loc is None or not loc.text:
                continue
            lastmod = _child(sm, "lastmod")
            dt = _parse_dt(lastmod.text) if lastmod is not None and lastmod.text else None
            entries.append((loc.text.strip(), dt))
        entries.sort(key=lambda e: e[1] or datetime.min.replace(tzinfo=UTC), reverse=True)
        ordered = _prioritize_news([loc for loc, _dt in entries])

        results: list[_Candidate] = []
        for loc in ordered[:_MAX_SITEMAP_LEAVES]:
            results.extend(await _walk_sitemap(client, loc, since, depth + 1))
        return results

    if root_tag == "urlset":
        return [c for url_el in _children(root, "url") if (c := _parse_url_entry(url_el, since))]

    return []


async def _try_sitemap(
    client: httpx.AsyncClient, domain: str, since: datetime
) -> tuple[bool, list[_Candidate]]:
    """Returns (a sitemap was found at all, candidates within the lookback window)."""
    sitemap_urls = await _sitemap_urls_from_robots(client, domain)
    if not sitemap_urls:
        return False, []
    candidates: list[_Candidate] = []
    for url in _prioritize_news(sitemap_urls)[:_MAX_TOP_LEVEL_SITEMAPS]:
        candidates.extend(await _walk_sitemap(client, url, since))
    return True, candidates


# ---------------------------------------------------------------------------
# RSS / Atom fallback
# ---------------------------------------------------------------------------


def _looks_like_feed(status_code: int, body_head: str) -> bool:
    """A 200 + XML content-type alone isn't enough — confirmed against
    meduza.io's /rss/all/all/, which 200s with an XML content-type but a
    genuinely empty body (a routing artifact, not a feed). Require an actual
    root tag in the body itself."""
    head = body_head.lower()
    return status_code == 200 and ("<rss" in head or "<feed" in head)


async def _discover_rss(client: httpx.AsyncClient, domain: str) -> str | None:
    for path in _COMMON_FEED_PATHS:
        url = f"https://{domain}{path}"
        try:
            resp = await client.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
        except Exception:  # noqa: BLE001
            continue
        if _looks_like_feed(resp.status_code, resp.text[:500]):
            return url
    return None


def _parse_rss_item(item: Element, since: datetime) -> _Candidate | None:
    link_el = _child(item, "link")
    if link_el is None or not link_el.text:
        return None
    date_el = _child(item, "pubDate")
    published_at = _parse_dt(date_el.text) if date_el is not None and date_el.text else None
    if published_at is None or published_at < since:
        return None
    title_el = _child(item, "title")
    url = link_el.text.strip()
    headline = title_el.text.strip() if title_el is not None and title_el.text else None
    return _Candidate(
        url=url,
        headline=headline or _headline_from_url(url),
        published_at=published_at,
        headline_is_real=headline is not None,
    )


def _parse_atom_entry(entry: Element, since: datetime) -> _Candidate | None:
    links = _children(entry, "link")
    link_el = next(
        (link for link in links if link.attrib.get("rel", "alternate") == "alternate"), None
    )
    href = link_el.attrib.get("href") if link_el is not None else None
    if not href:
        return None
    # NOT `_child(entry, "updated") or _child(entry, "published")` — an Element with
    # no child elements (just text, like <updated>...</updated>) is falsy under
    # ElementTree's deprecated truthiness rules, so `or` would wrongly skip a found one.
    date_el = _child(entry, "updated")
    if date_el is None:
        date_el = _child(entry, "published")
    published_at = _parse_dt(date_el.text) if date_el is not None and date_el.text else None
    if published_at is None or published_at < since:
        return None
    title_el = _child(entry, "title")
    headline = title_el.text.strip() if title_el is not None and title_el.text else None
    return _Candidate(
        url=href,
        headline=headline or _headline_from_url(href),
        published_at=published_at,
        headline_is_real=headline is not None,
    )


def _parse_feed(content: bytes, since: datetime) -> list[_Candidate]:
    try:
        root = _DefusedET.fromstring(content)
    except Exception:  # noqa: BLE001
        return []
    root_tag = _localname(root.tag)

    if root_tag == "rss":
        channel = _child(root, "channel")
        return [c for item in _children(channel, "item") if (c := _parse_rss_item(item, since))]

    if root_tag == "feed":
        return [c for entry in _children(root, "entry") if (c := _parse_atom_entry(entry, since))]

    return []


async def _try_rss(
    client: httpx.AsyncClient, domain: str, since: datetime
) -> tuple[bool, list[_Candidate]]:
    """Returns (a feed was found at all, candidates within the lookback window)."""
    feed_url = await _discover_rss(client, domain)
    if not feed_url:
        return False, []
    try:
        resp = await client.get(feed_url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
    except Exception:  # noqa: BLE001
        return True, []
    return True, _parse_feed(resp.content, since)


# ---------------------------------------------------------------------------
# Title recovery (some sitemaps, e.g. TASS's, carry no title at all — only a
# bare URL/lastmod — so the relevance filter would otherwise see opaque IDs)
# ---------------------------------------------------------------------------

_TITLE_FETCH_TIMEOUT = 5.0
_MAX_CONCURRENT_TITLE_FETCHES = 10
_TITLE_PATTERNS = (
    re.compile(r'property=["\']og:title["\']\s+content=["\']([^"\']+)', re.I),
    re.compile(r"<title[^>]*>([^<]+)</title>", re.I),
)


async def _recover_title(
    client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore
) -> str | None:
    async with semaphore:
        try:
            resp = await client.get(url, timeout=_TITLE_FETCH_TIMEOUT, follow_redirects=True)
        except Exception:  # noqa: BLE001 — any failure just means "keep the fallback headline"
            return None
    head = resp.text[:20_000]
    for pattern in _TITLE_PATTERNS:
        match = pattern.search(head)
        if match:
            return match.group(1).strip()
    return None


async def _recover_titles(client: httpx.AsyncClient, candidates: list[_Candidate]) -> None:
    """Fill in real titles for candidates whose headline is the URL-fallback, in
    place. Bounded to the already-capped candidate list, so cost stays predictable
    regardless of how large the source sitemap/feed was."""
    needs_recovery = [c for c in candidates if not c.headline_is_real]
    if not needs_recovery:
        return
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_TITLE_FETCHES)
    titles = await asyncio.gather(
        *[_recover_title(client, c.url, semaphore) for c in needs_recovery]
    )
    recovered = 0
    for candidate, title in zip(needs_recovery, titles, strict=True):
        if title:
            candidate.headline = title
            candidate.headline_is_real = True
            recovered += 1
    logger.debug("fetch: recovered {} of {} missing titles", recovered, len(needs_recovery))


# ---------------------------------------------------------------------------
# Excerpt fetching — "open the article and read what's inside", not just the
# headline. Only run on candidates that already survived relevance filtering
# (a small set), since fetching every candidate's full page would be wasteful.
# ---------------------------------------------------------------------------

_EXCERPT_FETCH_TIMEOUT = 5.0
_MAX_CONCURRENT_EXCERPT_FETCHES = 10
_EXCERPT_PATTERNS = (
    re.compile(r'property=["\']og:description["\']\s+content=["\']([^"\']+)', re.I),
    re.compile(r'name=["\']description["\']\s+content=["\']([^"\']+)', re.I),
    re.compile(r'name=["\']twitter:description["\']\s+content=["\']([^"\']+)', re.I),
)


def _extract_excerpt(html: str) -> str | None:
    head = html[:20_000]
    for pattern in _EXCERPT_PATTERNS:
        match = pattern.search(head)
        if match:
            return match.group(1).strip()
    return None


async def _fetch_excerpt(
    client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore
) -> str | None:
    async with semaphore:
        try:
            resp = await client.get(url, timeout=_EXCERPT_FETCH_TIMEOUT, follow_redirects=True)
        except Exception:  # noqa: BLE001 — any failure just means "no excerpt, headline only"
            return None
    return _extract_excerpt(resp.text)


async def _fetch_excerpts(client: httpx.AsyncClient, candidates: list[_Candidate]) -> None:
    """Fill in a real excerpt (meta description) for each candidate, in place.
    Almost every real news site has one for SEO/social-sharing, so this should
    succeed far more often than not; a miss just leaves context=None downstream,
    same as before this existed."""
    if not candidates:
        return
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_EXCERPT_FETCHES)
    excerpts = await asyncio.gather(*[_fetch_excerpt(client, c.url, semaphore) for c in candidates])
    found = 0
    for candidate, excerpt in zip(candidates, excerpts, strict=True):
        if excerpt:
            candidate.excerpt = excerpt
            found += 1
    logger.debug("fetch: recovered {} of {} article excerpts", found, len(candidates))


# ---------------------------------------------------------------------------
# Topic relevance filtering
# ---------------------------------------------------------------------------


class _RelevantIndices(BaseModel):
    indices: list[int]


_relevance_filter: Agent[None, _RelevantIndices] = Agent(
    build_model("fast"),
    output_type=_RelevantIndices,
    system_prompt=(
        "You are given a numbered list of real headlines from one news outlet, and a "
        "topic. Return the 0-based indices of headlines genuinely relevant to the "
        "topic — actual coverage of it, not just tangentially related. Return an empty "
        "list if none are relevant. Never invent an index outside the given range."
    ),
)


async def _filter_relevant(
    candidates: list[_Candidate], topic_name: str, description: str
) -> list[_Candidate]:
    if not candidates:
        return []
    listing = "\n".join(f"{i}. {c.headline}" for i, c in enumerate(candidates))
    prompt = (
        f"Topic: {topic_name}\nDescription: {description or topic_name}\n\nHeadlines:\n{listing}"
    )
    try:
        result = await _relevance_filter.run(prompt)
    except Exception:  # noqa: BLE001
        logger.warning("fetch: relevance filtering failed — treating outlet as no matches")
        return []
    return [candidates[i] for i in result.output.indices if 0 <= i < len(candidates)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_recent(
    outlet: str,
    topic_name: str,
    description: str,
    since: datetime,
    max_items: int = _MAX_CANDIDATES,
) -> list[Article] | None:
    """Try to fetch real recent articles directly from *outlet*'s own site.

    Returns None if no direct method (sitemap or RSS) is available at all for this
    outlet — callers should fall back to a Perplexity per-outlet query in that
    case. An outlet WITH a working feed but nothing in the lookback window
    correctly returns [], not None — that's a real answer, not a failure.
    """
    try:
        domain = await resolve_domain(outlet)
        if domain is None:
            return None

        async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}) as client:
            found, candidates = await _try_sitemap(client, domain, since)
            method = "sitemap"
            if not found:
                found, candidates = await _try_rss(client, domain, since)
                method = "rss"
            if not found:
                return None

            candidates = candidates[:max_items]
            await _recover_titles(client, candidates)

        relevant = await _filter_relevant(candidates, topic_name, description)
        # "Open the article and read what's inside" — only for the small relevant
        # set, not all candidates, so cost stays bounded regardless of how many
        # headlines the outlet published.
        async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}) as client:
            await _fetch_excerpts(client, relevant)

        logger.info(
            "fetch.fetch_recent: {!r} via {} -> {} candidates, {} relevant",
            outlet,
            method,
            len(candidates),
            len(relevant),
        )
        return [
            Article(
                url=c.url,
                headline=c.headline,
                source=domain,
                published_at=c.published_at,
                summary=c.headline,
                context=c.excerpt,
                is_general_query=False,
            )
            for c in relevant
        ]
    except Exception:  # noqa: BLE001 — never let one outlet's fetch break the whole gather()
        logger.exception("fetch.fetch_recent failed unexpectedly for {!r}", outlet)
        return None
