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
from pydantic import BaseModel
from pydantic_ai import Agent

from agent.services.llm import Research, build_model
from agent.services.llm import research as _research
from elephant.article_dates import attach_published_dates
from elephant.models import Article, Side, Topic


def lookback_label(hours: int) -> str:
    """Turn an hour count into a human/Perplexity-friendly lookback string.

    e.g. 48 -> "2 days", 30 -> "30 hours". Used both in research prompts and
    as the freshness hint shown to perspectives.cluster().
    """
    if hours >= 24 and hours % 24 == 0:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours} hours"


def _recency_filter(hours: int) -> str:
    """Map an hour count to Perplexity's search_recency_filter bucket.

    Belt-and-suspenders alongside search_after_date_filter — more reliably
    forwarded by OpenRouter than the date-based filter.
    """
    if hours <= 24:
        return "day"
    if hours <= 24 * 7:
        return "week"
    return "month"


# Domains excluded from the *general* query results only.
# User-configured sources are never filtered this way.
_BLOCKED_GENERAL_DOMAINS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "instagram.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "tiktok.com",
        "reddit.com",
        "threads.net",
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


async def gather(topic: Topic, lookback_hours: int = 48) -> list[Article]:
    """Fetch articles for *topic* from all active sources.

    Fires queries concurrently. Logs and skips any source that fails rather
    than aborting the whole digest. Drops roundup/listicle articles globally.
    lookback_hours: how far back to search — derived by jobs.py from whichever
    checkup slot triggered this run.
    """
    excluded = {s.lower() for s in topic.excluded_sources}
    active = [s for s in topic.sources if s.lower() not in excluded]
    lookback = lookback_label(lookback_hours)

    # Side outlets: up to _MAX_OUTLETS_PER_SIDE per identified side, skipping anything
    # already covered by a user-tracked source or explicitly excluded.
    already_tracked = {s.lower() for s in active} | excluded
    side_outlets: list[str] = []
    for side in topic.sides:
        for outlet in side.outlets[:_MAX_OUTLETS_PER_SIDE]:
            if outlet.lower() not in already_tracked:
                side_outlets.append(outlet)
                already_tracked.add(outlet.lower())

    # Use description as the research query when set — it's the user's detailed focus.
    # Fall back to name so the short label still produces sensible results.
    query_subject = topic.description or topic.name
    source_instr = topic.source_guidance or _GENERAL_QUERY_INSTRUCTIONS
    today = datetime.now(UTC).strftime("%B %d, %Y")
    recency = _recency_filter(lookback_hours)
    # User-tracked sources bypass domain filtering (they chose it deliberately).
    # Auto-suggested side outlets get the same quality bar as the general query.
    coros = [_query_source(query_subject, src, lookback, today, recency) for src in active]
    coros += [
        _query_source(
            query_subject, src, lookback, today, recency, block_domains=_BLOCKED_GENERAL_DOMAINS
        )
        for src in side_outlets
    ]
    coros.append(_query_general(query_subject, lookback, today, source_instr, recency))

    raw = await asyncio.gather(*coros, return_exceptions=True)

    articles: list[Article] = []
    seen: set[str] = set()
    source_labels = active + side_outlets + ["general"]
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

    await attach_published_dates(articles)

    logger.info(
        "gathered {} articles for topic {!r} ({} tracked + {} side-outlet sources + general, "
        "{} roundups dropped)",
        len(articles),
        topic.name,
        len(active),
        len(side_outlets),
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
    topic_name: str,
    source: str,
    lookback: str,
    today: str,
    recency: str = "week",
    block_domains: frozenset[str] | None = None,
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
    articles = _parse(result, block_domains=block_domains)
    logger.debug(
        "source {!r} → {} articles: {}", source, len(articles), [a.headline[:60] for a in articles]
    )
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
        "You generate source guidance for a news research bot. Given a topic, write 2-4 sentences "
        "naming the most relevant outlets to prioritise. Think carefully about:\n"
        "1. Topic-specific specialist publications (e.g. IEEE Spectrum/Ars Technica for tech, "
        "The Lancet/NEJM for medicine, IAEA for nuclear, Variety/Deadline for film)\n"
        "2. Regional and national outlets in the LOCAL LANGUAGE if the topic is geographically "
        "specific (e.g. for Japan: Nikkei Asia, NHK World, Mainichi; for Brazil: Folha de S.Paulo, "
        "O Globo, Agência Brasil; for France: Le Monde, Le Figaro)\n"
        "3. If the topic involves two or more conflicting parties (states, factions, government "
        "vs. opposition, regulator vs. industry, etc.): identify each distinct party and name a "
        "MIX of outlets that lean toward or report favorably on that party's position — official "
        "state media or government channels where they exist, plus other sympathetic or aligned "
        "press — not just outside commentary about that side. Do this symmetrically for every "
        "party, never just some.\n"
        "4. Wire services and major international press (Reuters, AP, BBC, Guardian) as a neutral, "
        "outside-the-conflict layer\n"
        "5. Academic or institutional primary sources where relevant (arXiv, PubMed, IAEA, WHO)\n\n"
        "Always include specialist/regional, every conflicting party's own-leaning coverage, AND "
        "international coverage together — never omit a party's side. "
        "Start directly with 'Prioritise:' — no preamble. "
        "Example for a two-state conflict: 'Prioritise: [State A]'s state/government-aligned "
        "outlets plus 1-2 independent [State A] outlets; [State B]'s state/government-aligned "
        "outlets plus 1-2 independent [State B] outlets; Reuters, AP, BBC for international "
        "coverage.' Replace the bracketed placeholders with the actual countries/parties involved "
        "and real outlet names — never output literal brackets."
    ),
)


_MAX_OUTLETS_PER_SIDE = 4


class _SidesOutput(BaseModel):
    sides: list[Side]


_sides_identifier: Agent[None, _SidesOutput] = Agent(
    build_model("balanced"),
    output_type=_SidesOutput,
    system_prompt=(
        "You identify the distinct parties/perspectives involved in a news topic, if any. "
        "Sides are not always nation-states — they can be government vs. opposition, "
        "regulator vs. industry, factions within a country, or any other clearly opposed "
        "or distinct parties relevant to the topic.\n\n"
        "Rules:\n"
        "1. If the topic has no inherent sides (e.g. archaeology, scientific discoveries, "
        "general technology trends), return an EMPTY list. Do not invent sides that don't exist.\n"
        "2. If sides exist, return one entry per side with 2-4 example outlet names that lean "
        "toward or report favorably on that side's position — official state/institutional "
        "media where it exists, plus other sympathetic press. Never name only one side; if you "
        "identify any side, identify all of them.\n"
        "3. Keep side names short and neutral, e.g. 'Russia', 'Ukraine', 'Government', "
        "'Opposition', 'Industry', 'Regulators', 'Local residents'.\n"
        "4. Outlet names only — no URLs, no descriptions."
    ),
)


async def identify_sides(description: str, topic_name: str) -> list[Side]:
    """Identify the distinct parties/perspectives in a topic, each with example outlets.

    Returns an empty list for topics with no inherent sides, or if the LLM call fails.
    """
    prompt = f"Topic: {topic_name}\nDescription: {description}"
    try:
        result = await _sides_identifier.run(prompt)
        return result.output.sides
    except Exception:  # noqa: BLE001
        logger.warning("side identification failed for {!r} — assuming none", topic_name)
        return []


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
    articles = _parse(result, block_domains=_BLOCKED_GENERAL_DOMAINS, is_general_query=True)
    logger.debug(
        "general query → {} articles: {}", len(articles), [a.headline[:60] for a in articles]
    )
    return articles


def _parse(
    result: Research,
    block_domains: frozenset[str] | None = None,
    is_general_query: bool = False,
) -> list[Article]:
    """Turn a Research result into Article objects.

    Each cited URL becomes one Article. The source is always the citation's actual
    domain — never the outlet we asked about — because Perplexity frequently cites
    unrelated domains even when asked specifically about one outlet; trusting the
    query target would mislabel those as if they came from the outlet we asked for.
    The headline comes from the citation title.
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
            logger.debug("blocked domain: {}", domain)
            continue
        headline = src.title or _headline_from_url(src.url)
        articles.append(
            Article(
                url=src.url,
                headline=headline,
                source=domain,
                published_at=None,  # filled in by attach_published_dates() after gathering
                summary=headline,
                context=result.text,
                is_general_query=is_general_query,
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
