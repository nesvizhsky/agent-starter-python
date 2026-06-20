"""Integration tests for elephant/store.py.

These hit the real Neon database — run with:
    uv run pytest -m integration scripts/tests/test_elephant_store.py

They clean up after themselves: all test data is deleted in the fixture teardown.
"""

from collections.abc import AsyncGenerator

import pytest

from agent.services import db
from elephant import store
from elephant.models import Article

TEST_USER_ID = 999_000_001  # unlikely to collide with a real Telegram id


@pytest.fixture(scope="session", autouse=True)
async def _db_session() -> AsyncGenerator[None, None]:  # noqa: PT004
    """Apply migrations once per session; tear down test data and close the pool after all tests."""
    await store.apply_migrations()
    yield
    await db.execute("DELETE FROM elephant_users WHERE telegram_id = $1", TEST_USER_ID)
    await db.close_pool()


@pytest.fixture(autouse=True)
async def _clean_between_tests() -> AsyncGenerator[None, None]:  # noqa: PT004
    """Delete test data before each test so they don't bleed into each other."""
    await db.execute("DELETE FROM elephant_users WHERE telegram_id = $1", TEST_USER_ID)
    yield


@pytest.mark.integration
async def test_get_or_create_user() -> None:
    user = await store.get_or_create_user(TEST_USER_ID, "Test", "testuser")
    assert user.telegram_id == TEST_USER_ID
    assert user.first_name == "Test"

    # idempotent: upserts name change
    user2 = await store.get_or_create_user(TEST_USER_ID, "Updated", "testuser")
    assert user2.first_name == "Updated"


@pytest.mark.integration
async def test_topic_lifecycle() -> None:
    await store.get_or_create_user(TEST_USER_ID, "Test", None)

    topic = await store.create_topic(
        TEST_USER_ID,
        name="War in Ukraine",
        sources=["BBC", "TASS", "Ukrainska Pravda", "Meduza"],
        frequency="daily",
        send_hour=8,
        timezone="Europe/London",
    )
    assert topic.name == "War in Ukraine"
    assert "BBC" in topic.sources
    assert topic.paused is False

    topics = await store.get_topics(TEST_USER_ID)
    assert any(t.id == topic.id for t in topics)

    by_name = await store.get_topic_by_name(TEST_USER_ID, "war in ukraine")
    assert by_name is not None and by_name.id == topic.id

    await store.update_topic(topic.id, paused=True, feedback_notes="Prefer shorter")
    updated = await store.get_topic(TEST_USER_ID, topic.id)
    assert updated is not None
    assert updated.paused is True
    assert updated.feedback_notes == "Prefer shorter"

    await store.stamp_sent(topic.id)
    stamped = await store.get_topic(TEST_USER_ID, topic.id)
    assert stamped is not None and stamped.last_sent_at is not None


@pytest.mark.integration
async def test_seen_and_dedup() -> None:
    await store.get_or_create_user(TEST_USER_ID, "Test", None)
    topic = await store.create_topic(
        TEST_USER_ID,
        name="Archaeology",
        sources=["LiveScience"],
        frequency="weekly",
    )

    articles = [
        Article(
            url="https://example.com/a1",
            headline="Roman road found",
            source="LiveScience",
            published_at=None,
            summary="A road was found.",
        ),
        Article(
            url="https://example.com/a2",
            headline="Egyptian bakery",
            source="ArchMag",
            published_at=None,
            summary="A bakery was found.",
        ),
    ]
    fake_embeddings = [[0.1] * 1024, [0.9] * 1024]

    await store.record_seen(topic.id, articles, fake_embeddings)

    seen_urls = await store.get_seen_urls(topic.id)
    assert "https://example.com/a1" in seen_urls
    assert "https://example.com/a2" in seen_urls

    # similarity against the [0.1]*1024 embedding — should be very high (near 1.0)
    sim = await store.max_similarity(topic.id, [0.1] * 1024, since_days=7)
    assert sim > 0.99

    # similarity against a very different embedding — should be low
    sim2 = await store.max_similarity(topic.id, [0.0] * 512 + [1.0] * 512, since_days=7)
    assert sim2 < 0.9
