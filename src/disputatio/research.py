"""Gather articles for a topic from its tracked sources.

One concurrent Perplexity Sonar query per source, plus one general query that
catches perspectives not on the tracked list. Returns a deduplicated list of
Article objects ready for the freshness filter.

Source quality policy
---------------------
- The *general* (catch-all) query is restricted to quality journalism and
  scientific publications. Company blogs, YouTube, marketing sites, and social
  media are excluded via both prompt instructions and a domain blocklist.
- User-configured sources bypass all domain filtering — if you added a source,
  the bot queries it regardless.
- Roundup/listicle headlines ("Top 10 AI stories", "Weekly digest", etc.) are
  dropped from ALL results because they aggregate rather than report.

Known limitation: Perplexity indexes the public web. Sources behind paywalls,
or that Perplexity rarely crawls (some Russian state media, niche outlets), may
not surface reliably. Direct-URL fetching is v2 scope.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from loguru import logger
from pydantic_ai import Agent

from agent.services.llm import Research, build_model
from agent.services.llm import research as _research
from disputatio.models import Article, Topic

_LOOKBACK: dict[str, str] = {
    "daily": "48 hours",
    "twice_daily": "24 hours",
    "weekdays": "48 hours",
    "mwf": "72 hours",
    "tuth": "72 hours",
    "custom_days": "72 hours",
    "weekly": "7 days",
    "biweekly": "14 days",
}

# Domains excluded from the *general* query results only.
# User-configured sources are never filtered this way.
_BLOCKED_GENERAL_DOMAINS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "medium.com",
        "substack.com",
        "marketingprofs.com",
        "buildfastwithai.com",
        "promptailearning.com",
        "unrot.com",
        "verdantix.com",
    }
)

# Headlines matching these patterns are non-news: roundups, listicles, predictions.
# Applied to ALL results regardless of source.
_ROUNDUP_RE = re.compile(
    r"\b("
    r"top\s+\d+|"
    r"\d+\s+(biggest|best|worst|prediction|story|stories|thing|way|tip|trend|takeaway)s?\b|"
    r"(weekly|daily|monthly)\s+(roundup|digest|recap|summary|wrap.?up)|"
    r"\bai\s+(this|last)\s+week\b|"
    r"\bthe\s+week\s+in\b|"
    r"\byear\s+in\s+review\b|"
    r"\bwhat\s+to\s+expect\b|"
    r"\beverything\s+you\s+need\s+to\s+know\b|"
    r"\bpredictions?\s+for\s+\d{4}\b"
    r")",
    re.IGNORECASE,
)

_expander: Agent[None, str] = Agent(
    build_model("fast"),
    output_type=str,
    system_prompt=(
        "Refine a user's rough topic description into a focused research query for a news monitoring bot. "  # noqa: E501
        "Output one or two plain sentences describing exactly what to track: the specific domain, "
        "key actors, and types of events or developments to look for. "
        "Preserve the user's intent fully — do not narrow the scope or add assumptions. "
        "If the input is already precise and specific, keep it with minimal changes. "
        "No bullet points. No intro phrases like 'Track' or 'Monitor'. Just the query.\n\n"
        "Examples:\n"
        "- 'war in israel-palestine' → "
        "'Israel-Palestine conflict: military operations, ceasefire negotiations, "
        "civilian casualties, and political developments in Gaza and the West Bank'\n"
        "- 'ecology laws' → "
        "'International environmental legislation: new laws, regulations, and policy "
        "changes on ecology, biodiversity, and climate'\n"
        "- 'AI stuff' → "
        "'Artificial intelligence: new model releases, regulation, safety research, "
        "industry moves, and major applications'"
    ),
)


async def expand_query(raw: str, topic_name: str) -> str:
    """Refine a rough user description into a focused research query.

    Falls back to the raw input if the LLM call fails.
    """
    prompt = f"Topic name: {topic_name or raw}\nUser description: {raw}"
    try:
        result = await _expander.run(prompt)
        return result.output.strip()
    except Exception:  # noqa: BLE001
        logger.warning("query expansion failed for {!r} — using raw input", topic_name)
        return raw


async def gather(topic: Topic) -> list[Article]:
    """Fetch articles for *topic* from all active sources.

    Fires queries concurrently. Logs and skips any source that fails rather
    than aborting the whole digest. Drops roundup/listicle articles globally.
    """
    excluded = {s.lower() for s in topic.excluded_sources}
    active = [s for s in topic.sources if s.lower() not in excluded]
    lookback = _LOOKBACK.get(topic.frequency, "48 hours")

    # Use description as the research query when set — it's the user's detailed focus.
    # Fall back to name so the short label still produces sensible results.
    query_subject = topic.description or topic.name
    today = datetime.now(UTC).strftime("%B %d, %Y")
    coros = [_query_source(query_subject, src, lookback, today) for src in active]
    coros.append(_query_general(query_subject, lookback, today))

    raw = await asyncio.gather(*coros, return_exceptions=True)

    articles: list[Article] = []
    seen: set[str] = set()
    source_labels = active + ["general"]
    dropped = 0

    for label, result in zip(source_labels, raw, strict=True):
        if isinstance(result, BaseException):
            logger.warning("research failed for {!r}: {}", label, result)
            continue
        for article in result:
            if article.url in seen:
                continue
            if _ROUNDUP_RE.search(article.headline):
                logger.debug("dropped roundup/listicle: {!r}", article.headline)
                dropped += 1
                continue
            seen.add(article.url)
            articles.append(article)

    logger.info(
        "gathered {} articles for topic {!r} ({} sources + general, {} roundups dropped)",
        len(articles),
        topic.name,
        len(active),
        dropped,
    )
    return articles


async def _query_source(topic_name: str, source: str, lookback: str, today: str) -> list[Article]:
    query = (
        f"Today is {today}. What specifically happened with {topic_name!r} according to {source} "
        f"in the last {lookback}? "
        f"Only include events that occurred within this window — not older background or context. "
        f"List concrete recent events, statements, or decisions and cite specific article headlines."  # noqa: E501
    )
    try:
        result = await _research(query)
    except Exception:
        logger.exception("_research() call failed for source {!r}", source)
        return []
    return _parse(result, default_source=source)


_GENERAL_QUERY_INSTRUCTIONS = (
    "Prioritise established news outlets (newspapers, wire services such as Reuters, AP, AFP, "
    "broadcast news), scientific publications (Nature, Science, Cell, arXiv, The Lancet, "
    "NEJM, ScienceDaily, PhysOrg, New Scientist), and official institutional or government sources. "  # noqa: E501
    "Include new research findings, studies, and scientific discoveries when relevant. "
    "Do not cite: YouTube videos, company blogs, marketing or promotional content, "
    "social media posts, analyst market-research reports, or aggregator listicles. "
    "Each citation must be a primary news report or original publication, not a summary of other news."  # noqa: E501
)


async def _query_general(topic_name: str, lookback: str, today: str) -> list[Article]:
    query = (
        f"Today is {today}. What specifically happened with {topic_name!r} in the last {lookback}? "
        f"Only include events from this time window — not older background or context. "
        f"List concrete recent events, decisions, or developments from multiple perspectives. "
        f"{_GENERAL_QUERY_INSTRUCTIONS}"
    )
    try:
        result = await _research(query)
    except Exception:
        logger.exception("_research() general call failed for topic {!r}", topic_name)
        return []
    return _parse(result, default_source="general", block_domains=_BLOCKED_GENERAL_DOMAINS)


def _parse(
    result: Research,
    default_source: str,
    block_domains: frozenset[str] | None = None,
) -> list[Article]:
    """Turn a Research result into Article objects.

    Each cited URL becomes one Article. The headline comes from the citation title.
    summary = headline (used for embedding-based dedup).
    context = the full Perplexity answer prose, attached to every article from
    this query so perspectives.py has real content to write about.
    block_domains: if set, URLs from these domains are silently skipped.
    """
    articles = []
    for src in result.sources:
        if not src.url:
            continue
        domain = _source_from_url(src.url)
        if block_domains and any(domain.endswith(d) for d in block_domains):
            logger.debug("blocked domain from general query: {}", domain)
            continue
        headline = src.title or _headline_from_url(src.url)
        source = domain if default_source == "general" else default_source
        articles.append(
            Article(
                url=src.url,
                headline=headline,
                source=source,
                published_at=None,  # Perplexity citations don't include dates
                summary=headline,
                context=result.text,
            )
        )
    return articles


def _source_from_url(url: str) -> str:
    """Extract a clean domain from a URL, e.g. 'bbc.com' from 'https://www.bbc.com/...'"""
    try:
        host = urlparse(url).netloc
        return host.removeprefix("www.") or url
    except Exception:
        return url


def _headline_from_url(url: str) -> str:
    """Last resort: turn the URL path into a rough headline."""
    try:
        path = urlparse(url).path.rstrip("/").split("/")[-1]
        return path.replace("-", " ").replace("_", " ").title() or url
    except Exception:
        return url
