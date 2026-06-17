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
from datetime import UTC, datetime, timedelta
from uuid import UUID

from loguru import logger

from agent.services.llm import embed
from disputatio import store
from disputatio.models import Article

SIMILARITY_THRESHOLD = 0.85  # cosine similarity above which we treat two articles as the same story

_LOOKBACK_DAYS: dict[str, int] = {
    "daily": 2,
    "twice_daily": 1,
    "weekly": 7,
}


async def filter_seen(
    topic_id: UUID,
    articles: list[Article],
    frequency: str,
) -> tuple[list[Article], list[list[float]]]:
    """Remove articles the user has already seen for *topic_id*.

    Returns (fresh_articles, their_embeddings).  The caller should pass both
    to store.record_seen() after sending the digest so future runs dedup correctly.
    """
    if not articles:
        return [], []

    lookback_days = _LOOKBACK_DAYS.get(frequency, 2)

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
        return [], []

    # ------------------------------------------------------------------
    # Pass 2 — semantic similarity via embeddings
    # ------------------------------------------------------------------
    texts = [f"{a.headline} {a.summary}" for a in after_date]
    embeddings = await embed(texts)

    # Check each embedding against the stored corpus concurrently.
    # Fail-open: if the similarity check errors, include the article
    # (better to show a near-duplicate than miss real news).
    similarities = await asyncio.gather(
        *[store.max_similarity(topic_id, emb, lookback_days) for emb in embeddings],
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
    return fresh, fresh_embeddings
