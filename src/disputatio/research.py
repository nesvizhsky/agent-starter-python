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
from datetime import UTC, datetime, timedelta
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

# Maps frequency to Perplexity's search_recency_filter value.
# Belt-and-suspenders alongside search_after_date_filter — more reliably
# forwarded by OpenRouter than the date-based filter.
_RECENCY_FILTER: dict[str, str] = {
    "twice_daily": "day",
    "daily": "day",       # "day" is 24h; prompt+date filter cover the 24-48h gap
    "weekdays": "day",
    "mwf": "week",
    "tuth": "week",
    "custom_days": "week",
    "weekly": "week",
    "biweekly": "month",
}

# Domains excluded from the *general* query results only.
# User-configured sources are never filtered this way.
_BLOCKED_GENERAL_DOMAINS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "marketingprofs.com",
        "buildfastwithai.com",
        "promptailearning.com",
        "unrot.com",
        "verdantix.com",
        "dentro.de",
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
    source_instr = topic.source_guidance or _GENERAL_QUERY_INSTRUCTIONS
    today = datetime.now(UTC).strftime("%B %d, %Y")
    recency = _RECENCY_FILTER.get(topic.frequency, "week")
    coros = [_query_source(query_subject, src, lookback, today, recency) for src in active]
    coros.append(_query_general(query_subject, lookback, today, source_instr, recency))

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
                logger.info("dropped roundup/listicle: {!r}", article.headline)
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


def _cutoff_date(lookback: str) -> str:
    """Compute the API-level search cutoff date from a lookback string like '7 days' or '48 hours'.

    Returns a date in "MM/DD/YYYY" format for Perplexity's search_after_date_filter.
    """
    parts = lookback.split()
    n = int(parts[0])
    unit = parts[1] if len(parts) > 1 else "days"
    days = max(1, n // 24) if "hour" in unit else n
    dt = datetime.now(UTC) - timedelta(days=days)
    return dt.strftime("%m/%d/%Y")


def _source_label(source: str) -> str:
    """Strip user annotations from a source name, keeping only the outlet name.

    Users sometimes store sources as 'медуза - российская оппозиция' for their
    own reference. Only the part before ' - ' or ' — ' is the actual outlet name.
    """
    for sep in (" — ", " - "):
        if sep in source:
            return source.split(sep, 1)[0].strip()
    return source.strip()


async def _query_source(
    topic_name: str, source: str, lookback: str, today: str, recency: str = "week"
) -> list[Article]:
    cutoff = _cutoff_date(lookback)
    outlet = _source_label(source)
    query = (
        f"Today is {today}. Only include articles published after {cutoff}. "
        f"What specifically happened with {topic_name!r} according to {outlet} "
        f"in the last {lookback}? "
        f"Do not include older background or historical context — only concrete recent events, "
        f"statements, or decisions. Cite specific article headlines."
    )
    try:
        result = await _research(query, search_after_date=cutoff, search_recency_filter=recency)
    except Exception:
        logger.exception("_research() call failed for source {!r}", source)
        return []
    articles = _parse(result, default_source=source)
    logger.debug("source {!r} → {} articles: {}", source, len(articles),
                 [a.headline[:60] for a in articles])
    return articles


_SOURCE_EXCLUSIONS = (
    "Do not cite: YouTube, company press releases or blogs, marketing content, "
    "social media posts, analyst market-research reports, or aggregator listicles. "
    "Each citation must be a primary news report or original publication — not a roundup of other news."  # noqa: E501
)

# Fallback used only when a topic has no stored source_guidance.
_GENERAL_QUERY_INSTRUCTIONS = (
    "Prioritise: established newspapers and wire services (Reuters, AP, AFP, BBC, Guardian, NYT, FT); "  # noqa: E501
    "specialist technology and science journalism (Ars Technica, Wired, MIT Technology Review, "
    "The Verge, TechCrunch, IEEE Spectrum, VentureBeat, New Scientist, Nature, Science, "
    "ScienceDaily, PhysOrg, The Lancet, NEJM, arXiv); and official institutional or government sources. "  # noqa: E501
    "Include new research findings, studies, and scientific discoveries when relevant. "
    f"{_SOURCE_EXCLUSIONS}"
)

_source_profiler: Agent[None, str] = Agent(
    build_model("fast"),
    output_type=str,
    system_prompt=(
        "You generate source guidance for a news research bot. Given a topic, write 1-3 sentences "
        "naming the most relevant outlets to prioritise. Think carefully about:\n"
        "1. Topic-specific specialist publications (e.g. IEEE Spectrum/Ars Technica for tech, "
        "The Lancet/NEJM for medicine, IAEA for nuclear, Variety/Deadline for film)\n"
        "2. Regional and national outlets in the LOCAL LANGUAGE if the topic is geographically "
        "specific (e.g. for Israel/Palestine: Haaretz, Al Jazeera Arabic, Ynet; for Japan: "
        "Nikkei Asia, NHK World, Mainichi; for Brazil: Folha de S.Paulo, O Globo, Agência Brasil; "
        "for France: Le Monde, Le Figaro; for Russia: Meduza, The Insider, iStories)\n"
        "3. Wire services and major international press (Reuters, AP, BBC, Guardian) as a base\n"
        "4. Academic or institutional primary sources where relevant (arXiv, PubMed, INAH, WHO)\n\n"
        "Always include both specialist/regional AND international coverage. "
        "Start directly with 'Prioritise:' — no preamble. "
        "Example: 'Prioritise: Haaretz, Al Jazeera, Times of Israel, Reuters, AP, BBC. "
        "Include Arabic-language sources (Al Jazeera Arabic, Asharq Al-Awsat) and Hebrew-language "
        "sources (Ynet, Maariv) for regional perspectives.'"
    ),
)


async def generate_source_guidance(description: str, topic_name: str) -> str:
    """Generate topic-specific source guidance for the research query.

    Falls back to the generic instructions if the LLM call fails.
    """
    prompt = f"Topic: {topic_name}\nResearch query: {description}"
    try:
        result = await _source_profiler.run(prompt)
        guidance = result.output.strip()
        # Always append the quality exclusions so guidance stays consistent.
        return f"{guidance} {_SOURCE_EXCLUSIONS}"
    except Exception:  # noqa: BLE001
        logger.warning("source guidance generation failed for {!r} — using defaults", topic_name)
        return _GENERAL_QUERY_INSTRUCTIONS


async def _query_general(
    topic_name: str, lookback: str, today: str, source_instr: str, recency: str = "week"
) -> list[Article]:
    cutoff = _cutoff_date(lookback)
    query = (
        f"Today is {today}. Only include articles published after {cutoff}. "
        f"What specifically happened with {topic_name!r} in the last {lookback}? "
        f"Do not include older background or historical context — only concrete recent events, "
        f"decisions, or developments from multiple perspectives. "
        f"{source_instr}"
    )
    try:
        result = await _research(query, search_after_date=cutoff, search_recency_filter=recency)
    except Exception:
        logger.exception("_research() general call failed for topic {!r}", topic_name)
        return []
    articles = _parse(result, default_source="general", block_domains=_BLOCKED_GENERAL_DOMAINS)
    logger.debug("general query → {} articles: {}", len(articles),
                 [a.headline[:60] for a in articles])
    return articles


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
