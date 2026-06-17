"""Tests for disputatio/jobs.py.

Offline tests cover the pure is_due() logic. No DB, no LLM, no Telegram.

    uv run pytest scripts/tests/test_disputatio_jobs.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from disputatio.jobs import _is_synthesis_due, is_due
from disputatio.models import Topic


def _topic(
    *,
    frequency: str = "daily",
    send_hour: int = 8,
    send_dow: int = 0,
    schedule_days: str = "",
    paused: bool = False,
    last_sent_at: datetime | None = None,
    last_synthesis_at: datetime | None = None,
    timezone: str = "UTC",
) -> Topic:
    return Topic(
        id=uuid4(),
        telegram_id=1,
        name="Test",
        description=None,
        frequency=frequency,
        send_hour=send_hour,
        send_minute=0,
        send_dow=send_dow,
        schedule_days=schedule_days,
        timezone=timezone,
        paused=paused,
        sources=[],
        excluded_sources=[],
        trusted_sources=[],
        pinned_persona=None,
        feedback_notes=None,
        created_at=datetime.now(UTC),
        last_sent_at=last_sent_at,
        last_synthesis_at=last_synthesis_at,
    )


def _now(hour: int, day: int = 1) -> datetime:
    return datetime(2026, 6, day, hour, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# is_due — daily
# ---------------------------------------------------------------------------


def test_daily_due_at_send_hour_never_sent() -> None:
    topic = _topic(frequency="daily", send_hour=8, last_sent_at=None)
    assert is_due(topic, _now(8)) is True


def test_daily_not_due_wrong_hour() -> None:
    topic = _topic(frequency="daily", send_hour=8)
    assert is_due(topic, _now(9)) is False


def test_daily_not_due_already_sent_today() -> None:
    topic = _topic(frequency="daily", send_hour=8, last_sent_at=_now(8, day=1))
    assert is_due(topic, _now(8, day=1)) is False


def test_daily_due_sent_yesterday() -> None:
    topic = _topic(frequency="daily", send_hour=8, last_sent_at=_now(8, day=1))
    assert is_due(topic, _now(8, day=2)) is True


def test_paused_never_due() -> None:
    topic = _topic(paused=True, send_hour=8, last_sent_at=None)
    assert is_due(topic, _now(8)) is False


# ---------------------------------------------------------------------------
# is_due — twice_daily
# ---------------------------------------------------------------------------


def test_twice_daily_due_at_send_hour() -> None:
    topic = _topic(frequency="twice_daily", send_hour=8, last_sent_at=None)
    assert is_due(topic, _now(8)) is True


def test_twice_daily_due_at_plus_12() -> None:
    topic = _topic(frequency="twice_daily", send_hour=8, last_sent_at=None)
    assert is_due(topic, _now(20)) is True


def test_twice_daily_not_due_after_recent_send() -> None:
    # Sent at 08:00, checking at 08:00 same day = already sent within 10h
    sent = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)
    topic = _topic(frequency="twice_daily", send_hour=8, last_sent_at=sent)
    assert is_due(topic, _now(8, day=1)) is False


def test_twice_daily_due_after_12_hours() -> None:
    sent = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)
    topic = _topic(frequency="twice_daily", send_hour=8, last_sent_at=sent)
    now = datetime(2026, 6, 1, 20, 0, 0, tzinfo=UTC)
    assert is_due(topic, now) is True


# ---------------------------------------------------------------------------
# is_due — weekly
# ---------------------------------------------------------------------------


def test_weekly_due_after_7_days() -> None:
    sent = _now(8, day=1)
    topic = _topic(frequency="weekly", send_hour=8, last_sent_at=sent)
    assert is_due(topic, _now(8, day=8)) is True


def test_weekly_not_due_after_6_days() -> None:
    sent = _now(8, day=1)
    topic = _topic(frequency="weekly", send_hour=8, last_sent_at=sent)
    assert is_due(topic, _now(8, day=7)) is False


# ---------------------------------------------------------------------------
# is_due — new frequencies
# ---------------------------------------------------------------------------


def test_weekdays_due_on_monday() -> None:
    # day=1 in _now is 2026-06-01 which is a Monday (weekday=0)
    topic = _topic(frequency="weekdays", send_hour=8, last_sent_at=None)
    assert is_due(topic, _now(8, day=1)) is True


def test_weekdays_not_due_on_saturday() -> None:
    # 2026-06-06 is Saturday (weekday=5)
    topic = _topic(frequency="weekdays", send_hour=8, last_sent_at=None)
    saturday = datetime(2026, 6, 6, 8, 0, 0, tzinfo=UTC)
    assert is_due(topic, saturday) is False


def test_mwf_due_on_wednesday() -> None:
    # 2026-06-03 is Wednesday (weekday=2)
    topic = _topic(frequency="mwf", send_hour=9, last_sent_at=None)
    wednesday = datetime(2026, 6, 3, 9, 0, 0, tzinfo=UTC)
    assert is_due(topic, wednesday) is True


def test_mwf_not_due_on_tuesday() -> None:
    # 2026-06-02 is Tuesday (weekday=1)
    topic = _topic(frequency="mwf", send_hour=9, last_sent_at=None)
    tuesday = datetime(2026, 6, 2, 9, 0, 0, tzinfo=UTC)
    assert is_due(topic, tuesday) is False


def test_custom_days_due_on_selected_day() -> None:
    # 0,2 = Mon and Wed; test on Monday
    topic = _topic(frequency="custom_days", send_hour=8, schedule_days="0,2", last_sent_at=None)
    assert is_due(topic, _now(8, day=1)) is True  # day=1 is Monday


def test_custom_days_not_due_on_unselected_day() -> None:
    topic = _topic(frequency="custom_days", send_hour=8, schedule_days="2,4", last_sent_at=None)
    assert is_due(topic, _now(8, day=1)) is False  # day=1 is Monday (0), not in {2,4}


def test_biweekly_due_after_14_days() -> None:
    # send_dow=0 = Monday; day=1 is Monday
    sent = _now(8, day=1)
    topic = _topic(frequency="biweekly", send_hour=8, send_dow=0, last_sent_at=sent)
    two_weeks_later = datetime(2026, 6, 15, 8, 0, 0, tzinfo=UTC)  # 2026-06-15 is Monday
    assert is_due(topic, two_weeks_later) is True


def test_biweekly_not_due_after_7_days() -> None:
    sent = _now(8, day=1)
    topic = _topic(frequency="biweekly", send_hour=8, send_dow=0, last_sent_at=sent)
    assert is_due(topic, _now(8, day=8)) is False  # only 7 days, needs 14


# ---------------------------------------------------------------------------
# _is_synthesis_due
# ---------------------------------------------------------------------------


def test_synthesis_not_due_if_never_synthesised() -> None:
    topic = _topic(send_hour=8, last_synthesis_at=None)
    assert _is_synthesis_due(topic, _now(8)) is False


def test_synthesis_due_after_7_days() -> None:
    sent = _now(8, day=1)
    topic = _topic(send_hour=8, last_synthesis_at=sent)
    assert _is_synthesis_due(topic, _now(8, day=8)) is True


def test_synthesis_not_due_after_6_days() -> None:
    sent = _now(8, day=1)
    topic = _topic(send_hour=8, last_synthesis_at=sent)
    assert _is_synthesis_due(topic, _now(8, day=7)) is False


def test_synthesis_not_due_wrong_hour() -> None:
    sent = _now(8, day=1)
    topic = _topic(send_hour=8, last_synthesis_at=sent)
    assert _is_synthesis_due(topic, _now(9, day=8)) is False
