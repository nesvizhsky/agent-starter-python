"""All database access for Disputatio.

Every function is scoped by telegram_id (directly, or via topic_id FK).
No raw SQL lives outside this file.

Vectors (pgvector) are passed as formatted strings and cast in SQL:
  write: "[0.1, 0.2, ...]"::vector
  read:  content_embedding::float4[]  → asyncpg returns list[float] natively
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from agent.services import db
from disputatio.models import Article, Topic, User

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_UPDATABLE_TOPIC_FIELDS = frozenset(
    {
        "name",
        "description",
        "display_name",
        "display_description",
        "frequency",
        "send_hour",
        "send_minute",
        "send_dow",
        "schedule_days",
        "timezone",
        "paused",
        "sources",
        "excluded_sources",
        "trusted_sources",
        "feedback_notes",
        "source_guidance",
    }
)


def _vec(embedding: list[float]) -> str:
    """Format a float list as a pgvector literal: '[0.1,0.2,...]'"""
    return "[" + ",".join(str(x) for x in embedding) + "]"


def _topic(row: object) -> Topic:
    return Topic.model_validate(dict(row))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def apply_migrations() -> list[str]:
    """Apply any pending migrations. Call once at startup."""
    return await db.apply_migrations(MIGRATIONS_DIR)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def get_or_create_user(
    telegram_id: int,
    first_name: str,
    username: str | None,
) -> User:
    row = await db.fetchrow(
        """
        INSERT INTO disputatio_users (telegram_id, first_name, username)
        VALUES ($1, $2, $3)
        ON CONFLICT (telegram_id) DO UPDATE
            SET first_name = EXCLUDED.first_name,
                username   = EXCLUDED.username
        RETURNING *
        """,
        telegram_id,
        first_name,
        username,
    )
    return User.model_validate(dict(row))  # type: ignore[arg-type]


async def get_user_language(telegram_id: int) -> str:
    row = await db.fetchrow(
        "SELECT language FROM disputatio_users WHERE telegram_id = $1",
        telegram_id,
    )
    return str(row["language"]) if row else "English"


async def set_user_language(telegram_id: int, language: str) -> None:
    await db.execute(
        "UPDATE disputatio_users SET language = $1 WHERE telegram_id = $2",
        language,
        telegram_id,
    )


async def set_topic_display_fields(
    topic_id: UUID,
    display_name: str | None,
    display_description: str | None,
) -> None:
    await db.execute(
        """UPDATE disputatio_topics
           SET display_name = $1, display_description = $2
           WHERE id = $3""",
        display_name,
        display_description,
        topic_id,
    )


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------


async def get_topics(telegram_id: int) -> list[Topic]:
    rows = await db.fetch(
        "SELECT * FROM disputatio_topics WHERE telegram_id = $1 ORDER BY created_at",
        telegram_id,
    )
    return [_topic(r) for r in rows]


async def get_topic(telegram_id: int, topic_id: UUID) -> Topic | None:
    row = await db.fetchrow(
        "SELECT * FROM disputatio_topics WHERE id = $1 AND telegram_id = $2",
        topic_id,
        telegram_id,
    )
    return _topic(row) if row else None


async def get_topic_by_name(telegram_id: int, name: str) -> Topic | None:
    row = await db.fetchrow(
        "SELECT * FROM disputatio_topics WHERE telegram_id = $1 AND lower(name) = lower($2)",
        telegram_id,
        name,
    )
    return _topic(row) if row else None


async def create_topic(
    telegram_id: int,
    *,
    name: str,
    frequency: str = "daily",
    send_hour: int = 8,
    send_minute: int = 0,
    send_dow: int = 0,
    schedule_days: str = "",
    timezone: str = "UTC",
    sources: list[str],
    description: str | None = None,
    source_guidance: str | None = None,
) -> Topic:
    row = await db.fetchrow(
        """
        INSERT INTO disputatio_topics
            (telegram_id, name, description, frequency,
             send_hour, send_minute, send_dow, schedule_days, timezone, sources,
             source_guidance)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING *
        """,
        telegram_id,
        name,
        description,
        frequency,
        send_hour,
        send_minute,
        send_dow,
        schedule_days,
        timezone,
        sources,
        source_guidance,
    )
    return _topic(row)  # type: ignore[arg-type]


async def update_topic(topic_id: UUID, **fields: object) -> None:
    """Update arbitrary topic fields. Only whitelisted column names are accepted."""
    unknown = set(fields) - _UPDATABLE_TOPIC_FIELDS
    if unknown:
        raise ValueError(f"Unknown topic fields: {unknown}")
    if not fields:
        return
    keys = list(fields)
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(keys))
    await db.execute(
        f"UPDATE disputatio_topics SET {sets} WHERE id = $1",  # noqa: S608
        topic_id,
        *[fields[k] for k in keys],
    )


async def stamp_sent(topic_id: UUID) -> None:
    await db.execute(
        "UPDATE disputatio_topics SET last_sent_at = now() WHERE id = $1",
        topic_id,
    )


async def get_all_active_topics() -> list[Topic]:
    """Return all unpaused topics across all users. Used by the cron job."""
    rows = await db.fetch(
        "SELECT * FROM disputatio_topics WHERE paused = false ORDER BY telegram_id, created_at",
    )
    return [_topic(r) for r in rows]


# ---------------------------------------------------------------------------
# Seen content (freshness filter)
# ---------------------------------------------------------------------------


async def get_seen_urls(topic_id: UUID) -> set[str]:
    rows = await db.fetch(
        "SELECT article_url FROM disputatio_seen WHERE topic_id = $1",
        topic_id,
    )
    return {r["article_url"] for r in rows}


async def max_similarity(
    topic_id: UUID,
    embedding: list[float],
    since_days: int,
) -> float:
    """Return the highest cosine similarity between `embedding` and any stored
    embedding for this topic in the last `since_days` days.
    Returns 0.0 if there is nothing to compare against.
    """
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    row = await db.fetchrow(
        """
        SELECT MAX(1 - (content_embedding <=> $1::vector)) AS similarity
        FROM disputatio_seen
        WHERE topic_id = $2
          AND seen_at > $3
          AND content_embedding IS NOT NULL
        """,
        _vec(embedding),
        topic_id,
        cutoff,
    )
    if row is None or row["similarity"] is None:
        return 0.0
    return float(row["similarity"])


async def record_seen(
    topic_id: UUID,
    articles: list[Article],
    embeddings: list[list[float]],
) -> None:
    """Persist articles + their embeddings so future runs know what's been seen."""
    for article, embedding in zip(articles, embeddings, strict=True):
        await db.execute(
            """
            INSERT INTO disputatio_seen
                (topic_id, article_url, headline, content_embedding, published_at)
            VALUES ($1, $2, $3, $4::vector, $5)
            ON CONFLICT DO NOTHING
            """,
            topic_id,
            article.url,
            article.headline,
            _vec(embedding),
            article.published_at,
        )


async def clear_seen(topic_id: UUID) -> int:
    """Delete all seen-article records for *topic_id*. Returns row count deleted."""
    row = await db.fetchrow(
        "SELECT COUNT(*) AS n FROM disputatio_seen WHERE topic_id = $1", topic_id
    )
    n = int(row["n"]) if row else 0
    await db.execute("DELETE FROM disputatio_seen WHERE topic_id = $1", topic_id)
    return n


async def delete_topic(topic_id: UUID) -> None:
    """Delete a topic and all its associated data."""
    await db.execute("DELETE FROM disputatio_feedback WHERE topic_id = $1", topic_id)
    await db.execute("DELETE FROM disputatio_digests WHERE topic_id = $1", topic_id)
    await db.execute("DELETE FROM disputatio_seen WHERE topic_id = $1", topic_id)
    await db.execute("DELETE FROM disputatio_topics WHERE id = $1", topic_id)
