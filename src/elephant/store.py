"""All database access for Eat the Elephant.

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
from elephant.models import Article, Slot, Topic, User

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_UPDATABLE_TOPIC_FIELDS = frozenset(
    {
        "name",
        "description",
        "display_name",
        "display_description",
        "timezone",
        "paused",
        "sources",
        "excluded_sources",
        "trusted_sources",
        "feedback_notes",
        "source_guidance",
        "sides_json",
    }
)


def _vec(embedding: list[float]) -> str:
    """Format a float list as a pgvector literal: '[0.1,0.2,...]'"""
    return "[" + ",".join(str(x) for x in embedding) + "]"


def _slot(row: object) -> Slot:
    d: dict[str, object] = dict(row)  # type: ignore[arg-type]
    days_str = str(d.pop("days", "") or "")
    d["days"] = [int(x) for x in days_str.split(",") if x.strip()]
    return Slot.model_validate(d)


async def _slots_by_topic(topic_ids: list[UUID]) -> dict[UUID, list[Slot]]:
    if not topic_ids:
        return {}
    rows = await db.fetch(
        "SELECT * FROM elephant_topic_slots WHERE topic_id = ANY($1) ORDER BY hour, minute",
        topic_ids,
    )
    grouped: dict[UUID, list[Slot]] = {tid: [] for tid in topic_ids}
    for row in rows:
        grouped.setdefault(row["topic_id"], []).append(_slot(row))
    return grouped


def _topic(row: object, slots: list[Slot] | None = None) -> Topic:
    topic = Topic.model_validate(dict(row))  # type: ignore[arg-type]
    topic.slots = slots or []
    return topic


async def _attach_slots(topics: list[Topic]) -> list[Topic]:
    grouped = await _slots_by_topic([t.id for t in topics])
    for t in topics:
        t.slots = grouped.get(t.id, [])
    return topics


async def _insert_slots(topic_id: UUID, slots: list[Slot]) -> None:
    for slot in slots:
        await db.execute(
            """
            INSERT INTO elephant_topic_slots (topic_id, days, hour, minute, every_n_weeks)
            VALUES ($1, $2, $3, $4, $5)
            """,
            topic_id,
            ",".join(str(d) for d in slot.days),
            slot.hour,
            slot.minute,
            slot.every_n_weeks,
        )


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
        INSERT INTO elephant_users (telegram_id, first_name, username)
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
        "SELECT language FROM elephant_users WHERE telegram_id = $1",
        telegram_id,
    )
    return str(row["language"]) if row else "English"


async def set_user_language(telegram_id: int, language: str) -> None:
    await db.execute(
        "UPDATE elephant_users SET language = $1 WHERE telegram_id = $2",
        language,
        telegram_id,
    )


async def get_distinct_languages() -> list[str]:
    """All languages any existing user has set — used to pre-warm the UI
    translation cache at startup instead of paying for it lazily on whichever
    user's first action happens to land after a deploy/restart."""
    rows = await db.fetch("SELECT DISTINCT language FROM elephant_users")
    return [str(r["language"]) for r in rows]


async def get_user_timezone(telegram_id: int) -> str:
    row = await db.fetchrow(
        "SELECT timezone FROM elephant_users WHERE telegram_id = $1",
        telegram_id,
    )
    return str(row["timezone"]) if row else "UTC"


async def set_user_timezone(telegram_id: int, timezone: str) -> None:
    """Set the profile-level timezone and fan it out to every existing topic.

    jobs.py's scheduling reads topic.timezone directly, so this keeps that
    working unmodified while the profile setting is the one a user actually
    edits — changing it updates every topic at once, not just one.
    """
    await db.execute(
        "UPDATE elephant_users SET timezone = $1 WHERE telegram_id = $2",
        timezone,
        telegram_id,
    )
    await db.execute(
        "UPDATE elephant_topics SET timezone = $1 WHERE telegram_id = $2",
        timezone,
        telegram_id,
    )


async def set_topic_display_fields(
    topic_id: UUID,
    display_name: str | None,
    display_description: str | None,
) -> None:
    await db.execute(
        """UPDATE elephant_topics
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
        "SELECT * FROM elephant_topics WHERE telegram_id = $1 ORDER BY created_at",
        telegram_id,
    )
    return await _attach_slots([_topic(r) for r in rows])


async def get_topic(telegram_id: int, topic_id: UUID) -> Topic | None:
    row = await db.fetchrow(
        "SELECT * FROM elephant_topics WHERE id = $1 AND telegram_id = $2",
        topic_id,
        telegram_id,
    )
    if row is None:
        return None
    topic = _topic(row)
    topic.slots = (await _slots_by_topic([topic.id])).get(topic.id, [])
    return topic


async def get_topic_by_name(telegram_id: int, name: str) -> Topic | None:
    row = await db.fetchrow(
        "SELECT * FROM elephant_topics WHERE telegram_id = $1 AND lower(name) = lower($2)",
        telegram_id,
        name,
    )
    if row is None:
        return None
    topic = _topic(row)
    topic.slots = (await _slots_by_topic([topic.id])).get(topic.id, [])
    return topic


async def create_topic(
    telegram_id: int,
    *,
    name: str,
    slots: list[Slot],
    timezone: str = "UTC",
    sources: list[str],
    description: str | None = None,
    source_guidance: str | None = None,
    sides_json: str | None = None,
) -> Topic:
    row = await db.fetchrow(
        """
        INSERT INTO elephant_topics
            (telegram_id, name, description, timezone, sources,
             source_guidance, sides_json)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING *
        """,
        telegram_id,
        name,
        description,
        timezone,
        sources,
        source_guidance,
        sides_json,
    )
    topic = _topic(row)  # type: ignore[arg-type]
    await _insert_slots(topic.id, slots)
    topic.slots = slots
    return topic


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
        f"UPDATE elephant_topics SET {sets} WHERE id = $1",  # noqa: S608
        topic_id,
        *[fields[k] for k in keys],
    )


async def replace_slots(topic_id: UUID, slots: list[Slot]) -> None:
    """Replace a topic's entire set of checkup slots with *slots*."""
    await db.execute("DELETE FROM elephant_topic_slots WHERE topic_id = $1", topic_id)
    await _insert_slots(topic_id, slots)


async def stamp_sent(topic_id: UUID) -> None:
    await db.execute(
        "UPDATE elephant_topics SET last_sent_at = now() WHERE id = $1",
        topic_id,
    )


async def stamp_slot_sent(slot_id: UUID) -> None:
    await db.execute(
        "UPDATE elephant_topic_slots SET last_sent_at = now() WHERE id = $1",
        slot_id,
    )


async def get_all_active_topics() -> list[Topic]:
    """Return all unpaused topics across all users. Used by the cron job."""
    rows = await db.fetch(
        "SELECT * FROM elephant_topics WHERE paused = false ORDER BY telegram_id, created_at",
    )
    return await _attach_slots([_topic(r) for r in rows])


# ---------------------------------------------------------------------------
# Seen content (freshness filter)
# ---------------------------------------------------------------------------


async def get_seen_urls(topic_id: UUID) -> set[str]:
    rows = await db.fetch(
        "SELECT article_url FROM elephant_seen WHERE topic_id = $1",
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
        FROM elephant_seen
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
            INSERT INTO elephant_seen
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
    row = await db.fetchrow("SELECT COUNT(*) AS n FROM elephant_seen WHERE topic_id = $1", topic_id)
    n = int(row["n"]) if row else 0
    await db.execute("DELETE FROM elephant_seen WHERE topic_id = $1", topic_id)
    return n


async def delete_topic(topic_id: UUID) -> None:
    """Delete a topic and all its associated data."""
    await db.execute("DELETE FROM elephant_feedback WHERE topic_id = $1", topic_id)
    await db.execute("DELETE FROM elephant_digests WHERE topic_id = $1", topic_id)
    await db.execute("DELETE FROM elephant_seen WHERE topic_id = $1", topic_id)
    await db.execute("DELETE FROM elephant_topics WHERE id = $1", topic_id)


async def get_cached_domain(outlet: str) -> str | None:
    """Look up a previously-verified outlet name -> domain mapping. Global cache,
    shared across all topics/users — an outlet's domain doesn't depend on who's
    tracking it."""
    row = await db.fetchrow(
        "SELECT domain FROM elephant_outlet_domains WHERE outlet_name = $1", outlet.lower()
    )
    return str(row["domain"]) if row else None


async def cache_domain(outlet: str, domain: str) -> None:
    """Store a verified outlet name -> domain mapping, overwriting any stale entry."""
    await db.execute(
        "INSERT INTO elephant_outlet_domains (outlet_name, domain) VALUES ($1, $2) "
        "ON CONFLICT (outlet_name) DO UPDATE SET domain = $2, resolved_at = now()",
        outlet.lower(),
        domain,
    )
