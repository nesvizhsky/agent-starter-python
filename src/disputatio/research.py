"""Gather articles for a topic from its tracked sources.

One concurrent Perplexity Sonar query per source, plus one general query that
catches perspectives not on the tracked list. Returns a deduplicated list of
Article objects ready for the freshness filter.

Known limitation: Perplexity indexes the public web. Sources behind paywalls,
or that Perplexity rarely crawls (some Russian state media, niche outlets), may
not surface reliably. Direct-URL fetching is v2 scope.
"""

from __future__ import annotations

import asyncio
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
    than aborting the whole digest.
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

    for label, result in zip(source_labels, raw, strict=True):
        if isinstance(result, BaseException):
            logger.warning("research failed for {!r}: {}", label, result)
            continue
        for article in result:
            if article.url not in seen:
                seen.add(article.url)
                articles.append(article)

    logger.info(
        "gathered {} articles for topic {!r} ({} sources + general)",
        len(articles),
        topic.name,
        len(active),
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


async def _query_general(topic_name: str, lookback: str, today: str) -> list[Article]:
    query = (
        f"Today is {today}. What specifically happened with {topic_name!r} in the last {lookback}? "
        f"Only include events from this time window — not older background or context. "
        f"List concrete recent events, decisions, or developments from multiple sources. "
        f"Include a range of viewpoints and cite specific articles."
    )
    try:
        result = await _research(query)
    except Exception:
        logger.exception("_research() general call failed for topic {!r}", topic_name)
        return []
    return _parse(result, default_source="general")


def _parse(result: Research, default_source: str) -> list[Article]:
    """Turn a Research result into Article objects.

    Each cited URL becomes one Article. The headline comes from the citation title.
    summary = headline (used for embedding-based dedup).
    context = the full Perplexity answer prose, attached to every article from
    this query so perspectives.py has real content to write about.
    """
    articles = []
    for src in result.sources:
        if not src.url:
            continue
        headline = src.title or _headline_from_url(src.url)
        source = _source_from_url(src.url) if default_source == "general" else default_source
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
