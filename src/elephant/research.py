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
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from agent.services.llm import Research, build_model
from agent.services.llm import research as _research
from elephant import fetch
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


# Pure social/video platforms — never a real news citation, even when Perplexity
# tacks one onto a query for a source the user explicitly chose to track (e.g. a
# news article's embedded YouTube clip cited alongside the actual outlet). Applied
# to EVERY query, including user-tracked sources — unlike _BLOCKED_GENERAL_DOMAINS
# below, "you chose this source" was never meant to mean "accept any domain
# Perplexity happens to cite while researching it."
_ALWAYS_BLOCKED_DOMAINS: frozenset[str] = frozenset(
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
    }
)

# Domains excluded from the *general* and auto-suggested-side-outlet query results
# only. User-configured sources are never filtered by THIS set — only by
# _ALWAYS_BLOCKED_DOMAINS above.
_BLOCKED_GENERAL_DOMAINS: frozenset[str] = frozenset(
    {
        "marketingprofs.com",
        "buildfastwithai.com",
        "promptailearning.com",
        "unrot.com",
        "verdantix.com",
        "dentro.de",
        # Encyclopedias/reference sites: background material, never news.
        "britannica.com",
        "wikipedia.org",
        "dictionary.com",
        "merriam-webster.com",
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
    r"\bnews\s+of\s+the\s+week\b|"
    r"\byear\s+in\s+review\b|"
    r"\bwhat\s+to\s+expect\b|"
    r"\beverything\s+you\s+need\s+to\s+know\b|"
    r"\bpredictions?\s+for\s+\d{4}\b"
    r")",
    re.IGNORECASE,
)

# Headlines that are hub/category index pages or reference overviews rather than
# a report of one specific happening — e.g. "Archaeology News", "Archaeology and
# anthropology", "May/June 2026 - Archaeology Magazine", "Stonehenge: history,
# location, and meaning of...". Backstop for domains we don't otherwise block.
# Checked as several narrow patterns (not one combined regex) because each shape
# needs different anchoring — a bare "X News" must match end-to-end, but the
# "Title: history, location... of <object>" shape has free text after "of".
_HUB_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[\w&',\s]{1,40}\s+news(\s*[-–]\s*[\w.\s]+)?$", re.IGNORECASE),
    re.compile(r"^[\w&',\s]{1,40}\s+magazine$", re.IGNORECASE),
    re.compile(
        r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(/[a-z]+)?"
        r"\s+\d{4}\s*[-–]\s*[\w.\s]+$",
        re.IGNORECASE,
    ),
    re.compile(r"^\w+\s+and\s+\w+$", re.IGNORECASE),  # bare "X and Y" category label
    re.compile(
        r":\s+(history|location|meaning|definition|overview|facts|significance|guide)"
        r"(\s*,?\s*(and\s+)?(history|location|meaning|definition|overview|facts|significance|guide))*"  # noqa: E501
        r"\s+(of|to|about)\b",
        re.IGNORECASE,
    ),
)


def _is_hub_page(headline: str) -> bool:
    """A bare category label (≤2 words) or one of the _HUB_PATTERNS shapes."""
    if len(headline.split()) <= 2:
        return True
    return any(p.search(headline) for p in _HUB_PATTERNS)


_expander: Agent[None, str] = Agent(
    build_model("fast"),
    output_type=str,
    system_prompt=(
        "Refine a user's rough topic description into a focused research query for a news monitoring bot. "  # noqa: E501
        "Output one or two plain sentences describing exactly what to track: the specific domain, "
        "key actors, and types of events or developments to look for. "
        "Preserve the user's intent fully — do not narrow the scope or add assumptions. "
        "If the input is already precise and specific, keep it with minimal changes. "
        "Respond in the SAME language the user wrote their description in — never translate "
        "to English. This is shown back to the user, so it must match what they typed in.\n"
        "No bullet points. No intro phrases like 'Track' or 'Monitor'. Just the query.\n\n"
        "Examples (English input only, for style — match the user's own language otherwise):\n"
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


_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")

_english_translator: Agent[None, str] = Agent(
    build_model("fast"),
    output_type=str,
    system_prompt=(
        "Translate the given text to English. Return ONLY the translated text — "
        "no preamble, no quotes, no explanation."
    ),
)


async def _to_english(text: str) -> str:
    """Translate *text* to English for use as the general/catch-all search
    query. Perplexity's search matches on the literal query text, so a
    non-English query retrieves predominantly same-language results even
    with English meta-instructions layered on top — confirmed: a Russian
    archaeology topic's query returned ~9/10 Russian/Ukrainian sources;
    the same topic translated to English returned genuinely international
    specialist sources. Skips the LLM call entirely for already-ASCII text
    (cheap, common case), and falls back to the original text on failure.
    """
    if not _NON_ASCII_RE.search(text):
        return text
    try:
        result = await _english_translator.run(text)
        return result.output.strip()
    except Exception:  # noqa: BLE001
        logger.warning("translation to English failed for general query — using original")
        return text


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
    since = datetime.now(UTC) - timedelta(hours=lookback_hours)

    # Side outlets: up to _MAX_OUTLETS_PER_SIDE per identified side, skipping anything
    # already covered by a user-tracked source or explicitly excluded.
    already_tracked = {s.lower() for s in active} | excluded
    side_outlets: list[str] = []
    for side in topic.sides:
        for outlet in side.outlets[:_MAX_OUTLETS_PER_SIDE]:
            if outlet.lower() not in already_tracked:
                side_outlets.append(outlet)
                already_tracked.add(outlet.lower())

    # Default outlets: auto-identified general-purpose outlets for topics with
    # no user-added sources (or to supplement a thin list) — queried the same
    # way as a tracked source (sitemap/RSS direct fetch first), so a topic the
    # user never added sources to still gets comprehensive coverage instead of
    # relying solely on the general query's AI-search recall.
    default_outlets = [o for o in topic.default_outlets if o.lower() not in already_tracked]
    already_tracked.update(o.lower() for o in default_outlets)

    # Use description as the research query when set — it's the user's detailed focus.
    # Fall back to name so the short label still produces sensible results.
    query_subject = topic.description or topic.name
    source_instr = topic.source_guidance or _GENERAL_QUERY_INSTRUCTIONS
    today = datetime.now(UTC).strftime("%B %d, %Y")
    recency = _recency_filter(lookback_hours)
    # User-tracked sources bypass domain filtering (they chose it deliberately) —
    # is_user_tracked=True lets a specifically-tracked channel on an
    # otherwise-always-blocked platform (e.g. youtube.com/c/SomeChannel) through.
    # Auto-suggested side outlets get the same quality bar as the general query.
    coros = [
        _query_source(
            query_subject, src, lookback, today, recency, since=since, is_user_tracked=True
        )
        for src in active
    ]
    coros += [
        _query_source(
            query_subject,
            src,
            lookback,
            today,
            recency,
            block_domains=_BLOCKED_GENERAL_DOMAINS,
            since=since,
        )
        for src in side_outlets + default_outlets
    ]
    general_subject = await _to_english(query_subject)
    coros.append(_query_general(general_subject, lookback, today, source_instr, recency))

    raw = await asyncio.gather(*coros, return_exceptions=True)

    articles: list[Article] = []
    seen: set[str] = set()
    source_labels = active + side_outlets + default_outlets + ["general"]
    dropped = 0

    for label, result in zip(source_labels, raw, strict=True):
        if isinstance(result, BaseException):
            logger.warning("research failed for {!r}: {}", label, result)
            continue
        kept, batch_dropped = _filter_batch(result, seen)
        articles.extend(kept)
        dropped += batch_dropped

    await attach_published_dates(articles)

    logger.info(
        "gathered {} articles for topic {!r} ({} tracked + {} side-outlet + {} default-outlet "
        "sources + general, {} roundups dropped)",
        len(articles),
        topic.name,
        len(active),
        len(side_outlets),
        len(default_outlets),
        dropped,
    )

    # Coverage check: a single extra question asking specifically what's NOT
    # already in the list above. Each concurrent query above only sees its own
    # slice (one outlet, or one broad catch-all phrasing) — this is the one
    # point in the pipeline that looks at everything actually found so far and
    # asks a search engine to find the gap, rather than just searching blind
    # again. Sequential (needs the headline list first), so it always costs
    # one extra research() call per digest — same cost class as the general
    # query, run once, not per-source.
    coverage_raw = await _check_coverage(
        topic.name, general_subject, articles, lookback, today, recency
    )
    new_from_coverage, coverage_dropped = _filter_batch(coverage_raw, seen)
    if new_from_coverage:
        await attach_published_dates(new_from_coverage)
        articles.extend(new_from_coverage)
        logger.info(
            "coverage check found {} article(s) for {!r} the main gather missed "
            "({} dropped as roundup/hub)",
            len(new_from_coverage),
            topic.name,
            coverage_dropped,
        )

    return articles


def _filter_batch(raw_articles: list[Article], seen: set[str]) -> tuple[list[Article], int]:
    """Apply the roundup/hub-page/duplicate-URL filters shared by every batch
    of raw search results. Mutates *seen* in place so repeated calls (the main
    gather, then the coverage check) dedupe against each other too."""
    kept: list[Article] = []
    dropped = 0
    for article in raw_articles:
        if article.url in seen:
            continue
        if _ROUNDUP_RE.search(article.headline):
            logger.info("dropped roundup/listicle: {!r}", article.headline)
            dropped += 1
            continue
        if _is_hub_page(article.headline):
            logger.info("dropped hub/reference page: {!r}", article.headline)
            dropped += 1
            continue
        seen.add(article.url)
        kept.append(article)
    return kept, dropped


_coverage_check_prompt = (
    "I already found these {n} events/articles about {topic_name!r} ({description}) "
    "from the last {lookback}:\n\n{headlines}\n\n"
    "Search for any OTHER major, specific event, decision, or development about this "
    "exact topic from the same period that is NOT already covered by the list above. "
    "Only report something if it is a genuinely DIFFERENT event — do not just rephrase "
    "or re-cite something already listed, and do not report generic background or "
    "ongoing-situation commentary. If you find nothing missing, say so plainly and "
    "name nothing. If you do find something, name the specific event and cite the "
    "real source."
)


async def _check_coverage(
    topic_name: str,
    description: str,
    articles: list[Article],
    lookback: str,
    today: str,
    recency: str,
) -> list[Article]:
    """Cross-check the articles already gathered against one more fresh search
    that's explicitly asked what's missing, rather than searching blind again.
    Catches real gaps the concurrent per-source/general queries collectively
    missed. Returns [] (fails open, never blocks the digest) on any error —
    this is a quality improvement, not a required step."""
    if not articles:
        return []
    headlines = "\n".join(f"- {a.headline} ({a.source})" for a in articles[:40])
    query = f"Today is {today}. " + _coverage_check_prompt.format(
        topic_name=topic_name,
        description=description or topic_name,
        n=len(articles),
        lookback=lookback,
        headlines=headlines,
    )
    try:
        result = await _research(query, search_recency_filter=recency)
    except Exception:  # noqa: BLE001
        logger.warning("coverage check failed for {!r}", topic_name)
        return []
    return _parse(result, block_domains=_BLOCKED_GENERAL_DOMAINS, is_general_query=True)


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


def _own_domain(source: str) -> str | None:
    """Extract a domain from a tracked source string if it looks like one
    (e.g. "youtube.com/c/SomeChannel" -> "youtube.com"). Returns None for a
    plain outlet name like "BBC" that isn't itself a URL/domain — used so a
    user-tracked channel on an otherwise-always-blocked platform (YouTube,
    Instagram, ...) can still be queried, while an unrelated citation from
    that same platform tacked onto a different source's query stays blocked.
    """
    candidate = _source_label(source)
    without_scheme = candidate.removeprefix("https://").removeprefix("http://")
    if "." not in without_scheme.split("/")[0]:
        return None
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    return _source_from_url(candidate)


async def _query_source(
    topic_name: str,
    source: str,
    lookback: str,
    today: str,
    recency: str = "week",
    block_domains: frozenset[str] | None = None,
    since: datetime | None = None,
    is_user_tracked: bool = False,
) -> list[Article]:
    outlet = _source_label(source)
    # Only a source the user explicitly added (not an auto-suggested side
    # outlet) can bypass _ALWAYS_BLOCKED_DOMAINS, and only for its own domain —
    # e.g. tracking "youtube.com/c/SomeNewsChannel" should work, but that
    # never licenses an unrelated YouTube citation on a different source's query.
    allow_domain = _own_domain(source) if is_user_tracked else None

    # Try fetching directly from the outlet's own site first (real sitemap/RSS —
    # see fetch.py) — only falls through to the Perplexity recall-based query below
    # when no direct method is available at all for this outlet.
    if since is not None:
        direct = await fetch.fetch_recent(outlet, topic_name, topic_name, since)
        if direct is not None:
            if block_domains:
                direct = [a for a in direct if not any(a.source.endswith(d) for d in block_domains)]
            logger.debug(
                "source {!r} -> {} articles via direct fetch (skipped Perplexity)",
                source,
                len(direct),
            )
            return direct

    cutoff = _cutoff_date(lookback)
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
    articles = _parse(result, block_domains=block_domains, allow_domain=allow_domain)
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

# Same two-step pattern as identify_sides() above: ground in a real search first,
# then extract — instead of asking the model to invent outlet names from memory.
_guidance_research_prompt = (
    "What are the most authoritative, real, currently active outlets for tracking news "
    "on {topic_name!r} ({description})? The topic's name/description may be written in "
    "any language — that is NOT a signal about which country, region, or language "
    "community the topic is about; do not let it bias which outlets you name. Treat the "
    "topic as general/international in scope UNLESS it is itself, in substance, "
    "specifically about one country/region/language community (e.g. a topic about a "
    "specific nation's politics genuinely should prioritise that nation's own press). "
    "Cover: topic-specific specialist publications (e.g. IEEE Spectrum/Ars Technica for "
    "tech, The Lancet/NEJM for medicine, IAEA for nuclear, Variety/Deadline for film); "
    "regional/national outlets in the local language ONLY if the topic is itself "
    "geographically specific; if — and only if — the topic has CONCRETE, NAMED "
    "adversarial parties (specific states, a specific government vs. a specific named "
    "opposition, a specific regulator vs. a specific named industry coalition), each "
    "named party's own state/party-aligned press, named symmetrically for every party (a "
    "spectrum of editorial opinions about a broad topic is NOT the same as named "
    "adversarial parties — skip this category entirely if there isn't a real, specific "
    "dispute); general-interest wire services and international/national press as a "
    "neutral layer; and academic/institutional primary sources where relevant (arXiv, "
    "PubMed, IAEA, WHO). Name specific outlets."
)


class SourceGuidance(BaseModel):
    instructions: str
    # 3-5 general-purpose outlets (not party/side-specific — identify_sides()
    # covers those separately) gather() queries directly via the same
    # sitemap/RSS-first mechanism as user-tracked sources, so a topic with no
    # user-added sources still gets comprehensive coverage from a few real,
    # relevant outlets instead of relying only on the general AI search's
    # recall.
    outlets: list[str] = Field(default_factory=list)


_guidance_extractor: Agent[None, SourceGuidance] = Agent(
    build_model("fast"),
    output_type=SourceGuidance,
    system_prompt=(
        "Extract source guidance for a news research bot from the research text below.\n\n"
        "instructions: 2-4 sentences naming the most relevant outlets to prioritise, using "
        "ONLY outlets actually named in the text — never invent one that isn't mentioned. "
        "If the research named outlets for concrete, named adversarial parties (not just a "
        "spectrum of editorial opinions), include every party's outlets symmetrically — "
        "never omit a party's side. Start directly with 'Prioritise:' — no preamble.\n\n"
        "outlets: separately, list 3-5 of the single most authoritative GENERAL-PURPOSE "
        "outlets named in the text (specialist NEWS publications, major wire services, "
        "established journals/magazines) — outlets anyone tracking this topic should read "
        "regardless of which side of any dispute they favor. Do NOT include party/side-"
        "specific or state-aligned outlets here even if named in the text — those are "
        "handled separately. Do NOT include a company's own blog, newsroom, or press-"
        "release page (e.g. 'OpenAI Blog', 'Anthropic Blog', a company's official "
        "Twitter/X) even if it's a leading voice on the topic — those are promotional "
        "primary sources, not independent news coverage, and are excluded everywhere else "
        "in this system for the same reason. Use the outlet's real name only (e.g. "
        "'Archaeology Magazine', not a URL). Empty list if the text named no clear "
        "general-purpose independent outlets."
    ),
)


_MAX_OUTLETS_PER_SIDE = 3
_MAX_DEFAULT_OUTLETS = 4


class _SidesOutput(BaseModel):
    sides: list[Side]


# Two-step, grounded in a real web search instead of the model's background knowledge:
# 1. _research() (perplexity/sonar) finds real, currently active outlets per side.
# 2. A cheap extraction pass turns that prose into structured Side objects, naming
#    only outlets the research actually mentioned — never inventing beyond it.
# Tested against claude-sonnet-4.6 (pure knowledge, no search), grok/gemini/deepseek/gpt-5.1
# with OpenRouter's ":online" plugin, and other Perplexity Sonar tiers — plain sonar gave
# the most specific, reliably-cited outlet names for the lowest cost (see
# scripts/experiments/compare_research_models.py). Outlets it invents from background
# knowledge alone — with no search — were the real risk this avoids, not search-engine quality.
_sides_research_prompt = (
    "Is the topic {topic_name!r} ({description}), AS A WHOLE, CENTRALLY ABOUT a dispute "
    "between concrete, named adversarial parties — specific states at war or in dispute, a "
    "specific government vs. a specific named opposition movement/party, a specific "
    "regulator vs. a specific named industry coalition, or specific named factions within a "
    "country? The dispute must be the topic's main subject, not a tangential angle. The "
    "topic's name/description may be written in any language — that alone is NOT a signal "
    "that the topic is about that language's country/region; judge purely on substance. "
    "If yes, identify each named party and name 2-4 real, currently active media outlets "
    "per party that are specifically state-aligned, party-aligned, or otherwise formally "
    "affiliated with that party's position — not just outlets that happen to share a "
    "general editorial leaning on the subject.\n\n"
    "A topic with ONE simple, direct, central dispute (e.g. a war between two named "
    "states, a government vs. a named opposition with no other framing) qualifies "
    "directly from the question above — no further test needed, just identify the "
    "parties and answer YES.\n\n"
    "MULTI-ASPECT TEST (only relevant when the topic lists SEVERAL aspects, like "
    "elections + protests + sanctions + foreign relations, and it's unclear whether "
    "that makes it 'too broad'): ask whether there is ONE country/organization whose "
    "internal power struggle — current leadership/government vs. a named rival "
    "faction or opposition movement — is the THREAD running through every aspect "
    "listed. If yes, every other aspect is a CONSEQUENCE of that same power struggle, "
    "not an unrelated topic, even though it also involves other countries (e.g. "
    "Western sanctions imposed BECAUSE OF a domestic crackdown are still part of the "
    "same dispute). Worked example: 'Belarus: political and economic developments, "
    "including elections, protests, sanctions, and relations with Russia and Western "
    "countries' passes this test — every aspect listed is a consequence of the SAME "
    "Lukashenko-government-vs-opposition power struggle. This SHOULD get sides "
    "(Government of Belarus vs. the named opposition movement). This test is for "
    "deciding whether a multi-aspect DOMESTIC topic still has one dispute at its "
    "core — it does NOT apply to, and must never be used to reject, a direct "
    "state-vs-state war or other single, already-named dispute; those qualify from "
    "the main question above regardless of how many aspects (military operations, "
    "diplomacy, sanctions, humanitarian impact) the topic also mentions.\n\n"
    "Answer NO and name NO sides only if: the topic is broad, general, or worldwide in scope "
    "and only some narrow regional or topical slice of it touches a conflict (e.g. 'archaeology "
    "news worldwide' should NOT get sides just because some archaeological sites sit in "
    "conflict zones — that's a tangential intersection, not what the topic is about); OR "
    "the topic's several aspects are genuinely independent of each other with no single "
    "dispute or power struggle connecting them (e.g. 'AI trends' covers model releases, "
    "regulation, and applications — these are separate subjects with no shared adversarial "
    "dispute, each with its own spectrum of editorial opinions) — a spectrum of editorial "
    "opinions or ideological leanings, by itself, is NOT the same thing as a conflict's "
    "sides, even if real outlets exist at every point on that spectrum."
)

_sides_extractor: Agent[None, _SidesOutput] = Agent(
    build_model("fast"),
    output_type=_SidesOutput,
    system_prompt=(
        "Extract a structured list of sides/parties and their media outlets from the "
        "research text below. Use ONLY sides and outlets actually named in the text — "
        "never invent one that isn't mentioned.\n\n"
        "Rules:\n"
        "1. If the research concludes the topic has no concrete named adversarial parties "
        "as its CENTRAL subject, return an EMPTY list. This includes: (a) cases where the "
        "research instead describes a spectrum of editorial leanings or viewpoints (e.g. "
        "'techno-optimist', 'climate-skeptic', 'pro-regulation') rather than actual named "
        "opposing parties — those are NOT sides; and (b) cases where the research found a "
        "conflict that's only a narrow, tangential sub-case within a broader/worldwide topic "
        "(e.g. it found 'Israel vs. Hamas' while researching general worldwide archaeology "
        "news, because some dig sites happen to sit in a conflict zone) — if the dispute "
        "isn't what the topic itself is fundamentally about, return EMPTY, even though the "
        "named parties and outlets in that sub-case are real.\n"
        "2. If real adversarial parties exist, return one entry per party, with the "
        "specific outlet names mentioned for it (up to 4). Never include only one party if "
        "the text discusses multiple. Cap at 4 parties total — if the research describes "
        "more than that, it has likely drifted into listing viewpoints, not real parties; "
        "keep only the clearest, most concrete ones.\n"
        "3. Keep side names short and neutral, matching how the text refers to them, e.g. "
        "'Russia', 'Ukraine', 'Government', 'Opposition'.\n"
        "4. Outlet names only — no URLs, no descriptions.\n"
        "5. NEVER attribute a general-interest international or national newspaper, "
        "wire service, or broadcaster to any one side — this includes named ones like "
        "Reuters/AP/AFP/BBC/Guardian/DW/CNN/Al Jazeera/NYT/WSJ, AND any other outlet whose "
        "role here is neutral outside reporting rather than formal alignment with that "
        "specific party. Only include an outlet if it is state-owned by, formally "
        "affiliated with, or unambiguously partisan toward that one named party — general "
        "newspapers covering the topic from outside it belong to the separate general "
        "search, never a side's list, even if the research text mentions them in that "
        "side's section."
    ),
)


# Doublecheck for outlet/party misattribution — confirmed real in production
# twice: "ITAR" (a Russian state agency) attributed to Ukraine's side, and
# "Press TV" (Iranian) attributed to Belarus's government side. Tried a free
# chat model first (no search, just background knowledge) — unreliable: it
# consistently flagged the CORRECT, less-internationally-famous outlets
# (BelTA, Suspilne) while missing the actual planted errors, in both a 70B
# free model and the existing "fast" tier. Same lesson as identify_sides()
# itself: a plausible-sounding judgment from background knowledge alone isn't
# trustworthy for this kind of fact, regardless of model size — it needs to
# be grounded in a real search. One research() call covers every pair in the
# topic at once, then a cheap extraction pass reads the (now factual) prose —
# extracting from stated facts is the easy part; finding the facts wasn't.
async def verify_side_outlets(sides: list[Side]) -> list[Side]:
    """Remove any outlet a search-grounded doublecheck finds is not actually
    affiliated with its stated party. Fails open (returns *sides* unchanged)
    on any error — this is a quality improvement, not a required step."""
    pairs = [(outlet, side.name) for side in sides for outlet in side.outlets]
    if not pairs:
        return sides
    listing = "\n".join(f"{i + 1}. outlet={o!r}, party={p!r}" for i, (o, p) in enumerate(pairs))
    query = (
        "For each numbered (outlet, party) pair below, search to verify whether the "
        "outlet is genuinely affiliated with, state-owned by, or based in that "
        "party/country. List which pair numbers are a MISMATCH (the outlet is NOT "
        "actually affiliated with that party), with a one-sentence reason for each. "
        "If none are mismatches, say so plainly.\n\n" + listing
    )
    try:
        grounded = await _research(query)
        result = await _outlet_check_extractor.run(
            f"Pairs checked:\n{listing}\n\nResearch:\n{grounded.text}"
        )
    except Exception:  # noqa: BLE001
        logger.warning("outlet affiliation check failed — keeping all outlets unchanged")
        return sides
    bad_indices = {i - 1 for i in result.output.mismatch_indices}
    if not bad_indices:
        return sides
    bad_pairs = {pairs[i] for i in bad_indices if 0 <= i < len(pairs)}
    cleaned: list[Side] = []
    for side in sides:
        kept = [o for o in side.outlets if (o, side.name) not in bad_pairs]
        removed = [o for o in side.outlets if o not in kept]
        if removed:
            logger.info(
                "outlet check: removed {} from {!r} (affiliation mismatch)", removed, side.name
            )
        cleaned.append(Side(name=side.name, outlets=kept))
    return cleaned


class _OutletCheckOutput(BaseModel):
    mismatch_indices: list[int] = Field(default_factory=list)


_outlet_check_extractor: Agent[None, _OutletCheckOutput] = Agent(
    build_model("fast"),
    output_type=_OutletCheckOutput,
    system_prompt=(
        "Extract which numbered pairs the research below identified as a MISMATCH "
        "(outlet not actually affiliated with the stated party). Return ONLY the "
        "numbers explicitly identified as mismatches in the research text — never "
        "infer one yourself. Empty list if the research found no mismatches."
    ),
)


_SIDES_RESEARCH_ATTEMPTS = 3


async def identify_sides(description: str, topic_name: str) -> list[Side]:
    """Identify the distinct parties/perspectives in a topic, each with example outlets,
    grounded in a real web search rather than the model's unverified background knowledge.

    Returns an empty list for topics with no inherent sides, or if every attempt fails.
    This only runs once per topic (creation, or a description edit), so retrying is cheap
    insurance against a flaky live call.

    Retries the WHOLE research+extraction pipeline, not just extraction — the actual
    yes/no judgment lives in the research() call itself (a fresh live web search each
    time), and it's genuinely non-deterministic for borderline topics. Confirmed live:
    "Belarus: elections, protests, sanctions, and relations with Russia/the West" — a
    topic that very much DOES have two real sides (government vs. opposition) — landed
    on "no sides" 2 of 3 times even after tightening the prompt, because the research
    step itself sometimes judges the topic "too broad/multidimensional" and sometimes
    correctly recognizes those aspects as facets of one conflict. Retrying only the
    extraction step (the old behavior) can't fix this, since extraction is just reading
    whatever the research call already concluded.
    """
    for attempt in range(_SIDES_RESEARCH_ATTEMPTS):
        query = _sides_research_prompt.format(
            topic_name=topic_name, description=description or topic_name
        )
        try:
            grounded = await _research(query)
        except Exception:  # noqa: BLE001
            logger.warning("side research attempt {} failed for {!r}", attempt + 1, topic_name)
            continue
        prompt = f"Topic: {topic_name}\n\nResearch:\n{grounded.text}"
        try:
            result = await _sides_extractor.run(prompt)
            if result.output.sides:
                return await verify_side_outlets(result.output.sides)
        except Exception:  # noqa: BLE001
            logger.warning("side extraction attempt {} failed for {!r}", attempt + 1, topic_name)
    return []


async def generate_source_guidance(description: str, topic_name: str) -> SourceGuidance:
    """Generate topic-specific source guidance, grounded in a real web search rather
    than the model's unverified background knowledge.

    Falls back to the generic instructions (and no default outlets) if either call fails.
    """
    fallback = SourceGuidance(instructions=_GENERAL_QUERY_INSTRUCTIONS, outlets=[])
    query = _guidance_research_prompt.format(
        topic_name=topic_name, description=description or topic_name
    )
    try:
        grounded = await _research(query)
    except Exception:  # noqa: BLE001
        logger.warning("source guidance research failed for {!r} — using defaults", topic_name)
        return fallback
    try:
        result = await _guidance_extractor.run(f"Topic: {topic_name}\n\nResearch:\n{grounded.text}")
        guidance = result.output
        # Always append the quality exclusions so guidance stays consistent.
        instructions = f"{guidance.instructions.strip()} {_SOURCE_EXCLUSIONS}"
        outlets = guidance.outlets[:_MAX_DEFAULT_OUTLETS]
        return SourceGuidance(instructions=instructions, outlets=outlets)
    except Exception:  # noqa: BLE001
        logger.warning("source guidance extraction failed for {!r} — using defaults", topic_name)
        return fallback


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
    allow_domain: str | None = None,
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
    block_domains: if set, URLs from these domains are silently skipped, in
    addition to _ALWAYS_BLOCKED_DOMAINS (social/video platforms). The latter
    apply even to a query for a source the user explicitly tracks — UNLESS
    allow_domain matches, which is set to that source's own domain when the
    user specifically tracks a channel/account on one of those platforms
    (e.g. "youtube.com/c/SomeNewsChannel") — a citation from a *different*
    always-blocked domain Perplexity tacked on (e.g. an unrelated YouTube
    video while researching "BBC") is still blocked either way.
    """
    articles = []
    for src in result.sources:
        if not src.url:
            continue
        domain = _source_from_url(src.url)
        is_always_blocked = domain != allow_domain and any(
            domain.endswith(d) for d in _ALWAYS_BLOCKED_DOMAINS
        )
        if is_always_blocked or (block_domains and any(domain.endswith(d) for d in block_domains)):
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
