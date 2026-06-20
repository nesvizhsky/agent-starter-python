"""Tests for elephant/jobs.py.

Offline tests cover the pure due_slots() logic. No DB, no LLM, no Telegram.

    uv run pytest scripts/tests/test_elephant_jobs.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from elephant.jobs import due_slots
from elephant.models import Slot, Topic


def _slot(
    *,
    days: list[int] | None = None,
    hour: int = 8,
    minute: int = 0,
    every_n_weeks: int = 0,
    last_sent_at: datetime | None = None,
) -> Slot:
    return Slot(
        days=days or [],
        hour=hour,
        minute=minute,
        every_n_weeks=every_n_weeks,
        last_sent_at=last_sent_at,
    )


def _topic(
    *, slots: list[Slot] | None = None, paused: bool = False, timezone: str = "UTC"
) -> Topic:
    return Topic(
        id=uuid4(),
        telegram_id=1,
        name="Test",
        description=None,
        timezone=timezone,
        paused=paused,
        sources=[],
        excluded_sources=[],
        trusted_sources=[],
        feedback_notes=None,
        source_guidance=None,
        created_at=datetime.now(UTC),
        last_sent_at=None,
        slots=slots or [],
    )


def _now(hour: int, day: int = 1) -> datetime:
    return datetime(2026, 6, day, hour, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# due_slots — every-day slot
# ---------------------------------------------------------------------------


def test_daily_due_at_hour_never_sent() -> None:
    topic = _topic(slots=[_slot(hour=8)])
    assert due_slots(topic, _now(8)) == topic.slots


def test_daily_not_due_wrong_hour() -> None:
    topic = _topic(slots=[_slot(hour=8)])
    assert due_slots(topic, _now(9)) == []


def test_daily_not_due_already_sent_today() -> None:
    topic = _topic(slots=[_slot(hour=8, last_sent_at=_now(8, day=1))])
    assert due_slots(topic, _now(8, day=1)) == []


def test_daily_due_sent_yesterday() -> None:
    topic = _topic(slots=[_slot(hour=8, last_sent_at=_now(8, day=1))])
    assert due_slots(topic, _now(8, day=2)) == topic.slots


def test_paused_never_due() -> None:
    topic = _topic(slots=[_slot(hour=8)], paused=True)
    assert due_slots(topic, _now(8)) == []


# ---------------------------------------------------------------------------
# due_slots — two every-day slots at arbitrary times (twice daily, any gap)
# ---------------------------------------------------------------------------


def test_two_every_day_slots_each_independent() -> None:
    morning = _slot(hour=10)
    evening = _slot(hour=19)  # 9h gap, not 12 — must still work
    topic = _topic(slots=[morning, evening])
    assert due_slots(topic, _now(10)) == [morning]
    assert due_slots(topic, _now(19)) == [evening]
    assert due_slots(topic, _now(12)) == []


def test_second_slot_not_blocked_by_first_slots_send() -> None:
    """Sending the 10:00 slot must not stop the 19:00 slot firing the same day —
    each slot tracks its own last_sent_at, no shared cooldown between them."""
    morning = _slot(hour=10, last_sent_at=_now(10, day=1))
    evening = _slot(hour=19, last_sent_at=None)
    topic = _topic(slots=[morning, evening])
    assert due_slots(topic, _now(19, day=1)) == [evening]


def test_evening_slot_not_due_again_same_day() -> None:
    evening = _slot(hour=19, last_sent_at=_now(19, day=1))
    topic = _topic(slots=[evening])
    assert due_slots(topic, _now(19, day=1)) == []


# ---------------------------------------------------------------------------
# due_slots — weekday-restricted, fires every matching day (old weekdays/mwf/tuth/custom_days)
# ---------------------------------------------------------------------------


def test_weekdays_due_on_monday() -> None:
    # 2026-06-01 is a Monday
    topic = _topic(slots=[_slot(days=[0, 1, 2, 3, 4], hour=8)])
    assert due_slots(topic, _now(8, day=1)) == topic.slots


def test_weekdays_not_due_on_saturday() -> None:
    topic = _topic(slots=[_slot(days=[0, 1, 2, 3, 4], hour=8)])
    saturday = datetime(2026, 6, 6, 8, 0, 0, tzinfo=UTC)
    assert due_slots(topic, saturday) == []


def test_mwf_due_on_wednesday() -> None:
    # 2026-06-03 is a Wednesday
    topic = _topic(slots=[_slot(days=[0, 2, 4], hour=9)])
    wednesday = datetime(2026, 6, 3, 9, 0, 0, tzinfo=UTC)
    assert due_slots(topic, wednesday) == topic.slots


def test_mwf_not_due_on_tuesday() -> None:
    topic = _topic(slots=[_slot(days=[0, 2, 4], hour=9)])
    tuesday = datetime(2026, 6, 2, 9, 0, 0, tzinfo=UTC)
    assert due_slots(topic, tuesday) == []


def test_custom_days_due_on_selected_day() -> None:
    topic = _topic(slots=[_slot(days=[0, 2], hour=8)])
    assert due_slots(topic, _now(8, day=1)) == topic.slots  # Monday


def test_custom_days_not_due_on_unselected_day() -> None:
    topic = _topic(slots=[_slot(days=[2, 4], hour=8)])
    assert due_slots(topic, _now(8, day=1)) == []  # Monday not in {Wed, Fri}


def test_weekday_slot_due_again_next_matching_day() -> None:
    # Mon-Fri slot sent Monday should be due again Tuesday (no week-gating)
    topic = _topic(slots=[_slot(days=[0, 1, 2, 3, 4], hour=8, last_sent_at=_now(8, day=1))])
    tuesday = datetime(2026, 6, 2, 8, 0, 0, tzinfo=UTC)
    assert due_slots(topic, tuesday) == topic.slots


# ---------------------------------------------------------------------------
# due_slots — single-day, week-gated (old weekly/biweekly)
# ---------------------------------------------------------------------------


def test_weekly_due_after_7_days() -> None:
    sent = _now(8, day=1)
    topic = _topic(slots=[_slot(days=[0], hour=8, every_n_weeks=1, last_sent_at=sent)])
    assert due_slots(topic, _now(8, day=8)) == topic.slots  # day 8 is also Monday


def test_weekly_not_due_after_6_days() -> None:
    sent = _now(8, day=1)
    topic = _topic(slots=[_slot(days=[0], hour=8, every_n_weeks=1, last_sent_at=sent)])
    assert due_slots(topic, _now(8, day=7)) == []


def test_biweekly_due_after_14_days() -> None:
    sent = _now(8, day=1)  # Monday
    topic = _topic(slots=[_slot(days=[0], hour=8, every_n_weeks=2, last_sent_at=sent)])
    two_weeks_later = datetime(2026, 6, 15, 8, 0, 0, tzinfo=UTC)  # also Monday
    assert due_slots(topic, two_weeks_later) == topic.slots


def test_biweekly_not_due_after_7_days() -> None:
    sent = _now(8, day=1)
    topic = _topic(slots=[_slot(days=[0], hour=8, every_n_weeks=2, last_sent_at=sent)])
    assert due_slots(topic, _now(8, day=8)) == []  # only 7 days, needs 14


# ---------------------------------------------------------------------------
# due_slots — mixed slots on one topic ("Mon 12:00" + "Thu 10:00")
# ---------------------------------------------------------------------------


def test_mixed_weekday_slots_fire_independently() -> None:
    # 2026-06-01 Mon, 2026-06-04 Thu
    monday_slot = _slot(days=[0], hour=12)
    thursday_slot = _slot(days=[3], hour=10)
    topic = _topic(slots=[monday_slot, thursday_slot])
    assert due_slots(topic, datetime(2026, 6, 1, 12, 0, tzinfo=UTC)) == [monday_slot]
    assert due_slots(topic, datetime(2026, 6, 4, 10, 0, tzinfo=UTC)) == [thursday_slot]
    assert due_slots(topic, datetime(2026, 6, 1, 10, 0, tzinfo=UTC)) == []
