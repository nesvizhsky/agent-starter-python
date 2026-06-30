"""Freshness filter: drop articles the user has already seen for this topic.

Two passes:
  1. URL match  — instant, one DB query.
  2. Semantic   — embed each remaining article and compare against stored
                  embeddings; drops articles that are the same story under a
                  different headline or URL.

Returns the surviving articles paired with their embeddings so the caller can
persist them via store.record_seen() without re-computing.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID

from loguru import logger

from agent.services.llm import embed
from elephant import store
from elephant.models import Article

SIMILARITY_THRESHOLD = 0.85  # cosine similarity above which we treat two articles as the same story

# Semantic dedup looks back this many days regardless of the gather window.
# A daily topic gathers 2 days of news but must compare against 14 days of
# stored embeddings, otherwise the same ongoing story (same event, different
# URL each day) slips through once it's older than the gather window.
_SEMANTIC_LOOKBACK_DAYS = 14


async def filter_seen(
    topic_id: UUID,
    articles: list[Article],
    lookback_days: int,
) -> tuple[list[Article], list[list[float]]]:
    """Remove articles the user has already seen for *topic_id*.

    lookback_days: freshness window for the date filter — articles published
    before this cutoff are dropped. The semantic similarity check always uses
    a longer fixed window (_SEMANTIC_LOOKBACK_DAYS) so the same story can't
    reappear just because the first time it was seen falls outside the gather
    window.
    Returns (fresh_articles, their_embeddings).  The caller should pass both
    to store.record_seen() after sending the digest so future runs dedup correctly.
    """
    if not articles:
        return [], []

    # Per-source counts at each stage — added after a real incident where a
    # whole digest had only one source for a side with multiple tracked
    # outlets, and the aggregate "N -> M" log line alone couldn't tell us
    # whether a specific outlet's articles never arrived (gather()'s problem)
    # or arrived and got deduped away (this module's problem).
    in_counts = Counter(a.source for a in articles)

    # ------------------------------------------------------------------
    # Pass 1 — URL match (free, one query)
    # ------------------------------------------------------------------
    seen_urls = await store.get_seen_urls(topic_id)
    after_url = [a for a in articles if a.url not in seen_urls]

    # ------------------------------------------------------------------
    # Pass 1b — publication date (when available)
    # ------------------------------------------------------------------
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    after_date = [a for a in after_url if a.published_at is None or a.published_at >= cutoff]

    if not after_date:
        logger.info("dedup: all {} articles already seen (URL pass)", len(articles))
        _log_per_source(in_counts, Counter(), Counter())
        return [], []

    # ------------------------------------------------------------------
    # Pass 2 — semantic similarity via embeddings
    # ------------------------------------------------------------------
    texts = [f"{a.headline} {a.summary}" for a in after_date]
    embeddings = await embed(texts)

    # Check each embedding against the stored corpus concurrently.
    # Fail-open: if the similarity check errors, include the article
    # (better to show a near-duplicate than miss real news).
    # Use a longer lookback than the gather window so an ongoing story (same
    # event, new URL each day) can't keep slipping through once it's older
    # than lookback_days.
    semantic_days = max(lookback_days, _SEMANTIC_LOOKBACK_DAYS)
    similarities = await asyncio.gather(
        *[store.max_similarity(topic_id, emb, semantic_days) for emb in embeddings],
        return_exceptions=True,
    )

    fresh: list[Article] = []
    fresh_embeddings: list[list[float]] = []

    for article, emb, sim in zip(after_date, embeddings, similarities, strict=True):
        if isinstance(sim, BaseException):
            logger.warning(
                "similarity check failed for {!r}, including anyway: {}", article.url, sim
            )  # noqa: E501
            fresh.append(article)
            fresh_embeddings.append(emb)
        elif sim >= SIMILARITY_THRESHOLD:
            logger.debug("dedup: skipping {!r} (similarity {:.2f})", article.headline, sim)
        else:
            fresh.append(article)
            fresh_embeddings.append(emb)

    logger.info(
        "dedup: {} → {} after URL filter, {} after semantic filter",
        len(articles),
        len(after_date),
        len(fresh),
    )
    _log_per_source(
        in_counts, Counter(a.source for a in after_date), Counter(a.source for a in fresh)
    )
    return fresh, fresh_embeddings


def _log_per_source(
    in_counts: Counter[str], after_date_counts: Counter[str], fresh_counts: Counter[str]
) -> None:
    """One line per source showing in -> after URL/date pass -> after semantic
    pass, so a source that arrived with plenty of articles but ended up with
    zero in the final digest can be told apart from a source gather() never
    found anything for in the first place."""
    for source in sorted(in_counts):
        logger.info(
            "dedup per-source {!r}: {} in -> {} after URL/date -> {} after semantic",
            source,
            in_counts[source],
            after_date_counts.get(source, 0),
            fresh_counts.get(source, 0),
        )
