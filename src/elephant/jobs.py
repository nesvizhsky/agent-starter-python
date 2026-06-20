"""Cron job runners for Eat the Elephant.

`is_due` is a pure function (unit-testable offline).
`run_due_digests` loops all active topics, checks due-ness,
runs the pipeline, and isolates per-topic failures.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from elephant import dedup, digest, perspectives, propaganda, research, store
from elephant.models import Topic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TG_MAX = 4096  # Telegram hard limit for text messages


# ---------------------------------------------------------------------------
# Due-check logic (pure — no I/O)
# ---------------------------------------------------------------------------


def is_due(topic: Topic, now_local: datetime) -> bool:
    """Should this topic receive a digest at `now_local` (the topic's local time)?

    True only if: not paused, it's the right hour, frequency allows it, and
    we haven't already sent in this slot. Designed to be idempotent across
    repeated hourly ticks.
    """
    if topic.paused:
        return False

    due_hours: set[int] = {topic.send_hour}
    if topic.frequency == "twice_daily":
        due_hours.add((topic.send_hour + 12) % 24)

    if now_local.hour not in due_hours:
        return False

    # Day-of-week checks run before the last_sent_at guard so they also apply
    # to topics that have never been sent (last_sent_at is None).
    match topic.frequency:
        case "weekdays":
            if now_local.weekday() > 4:  # Sat=5, Sun=6
                return False
        case "mwf":
            if now_local.weekday() not in {0, 2, 4}:
                return False
        case "tuth":
            if now_local.weekday() not in {1, 3}:
                return False
        case "custom_days":
            if not topic.schedule_days:
                return False
            days = {int(d) for d in topic.schedule_days.split(",") if d.strip()}
            if now_local.weekday() not in days:
                return False
        case "weekly" | "biweekly":
            if now_local.weekday() != topic.send_dow:
                return False

    if topic.last_sent_at is None:
        return True

    last = topic.last_sent_at.astimezone(ZoneInfo(topic.timezone))

    match topic.frequency:
        case "daily" | "weekdays" | "mwf" | "tuth" | "custom_days":
            return last.date() < now_local.date()
        case "twice_daily":
            # 10h buffer avoids double-sending on clock-edge ticks
            return (now_local - last).total_seconds() >= 10 * 3600
        case "weekly":
            return (now_local.date() - last.date()).days >= 7
        case "biweekly":
            return (now_local.date() - last.date()).days >= 14
        case _:
            return False


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------


def _chunk_text(text: str, limit: int = _TG_MAX) -> list[str]:
    """Split text on paragraph boundaries into chunks no longer than *limit* chars."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = (current + "\n\n" + para).lstrip("\n") if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Single paragraph too long: fall back to line-level splitting
            if len(para) > limit:
                for line in para.split("\n"):
                    joined = (current + "\n" + line).lstrip("\n") if current else line
                    if len(joined) <= limit:
                        current = joined
                    else:
                        if current:
                            chunks.append(current)
                        current = line
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks


def _topic_keyboard(topic: Topic) -> InlineKeyboardMarkup:
    tid = str(topic.id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶ Check now", callback_data=f"tp:check:{tid}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"tp:edit:{tid}"),
            ]
        ]
    )


async def _send_digest(bot: Bot, topic: Topic, output: digest.DigestOutput) -> None:
    byline = f"<b>{topic.shown_name}</b>\n\n"
    chunks = _chunk_text(output.main)
    for i, chunk in enumerate(chunks):
        text = (byline + chunk) if i == 0 else chunk
        try:
            await bot.send_message(chat_id=topic.telegram_id, text=text, parse_mode="HTML")
        except BadRequest:
            logger.warning("HTML parse failed for chunk {}/{} — retrying plain", i + 1, len(chunks))
            await bot.send_message(chat_id=topic.telegram_id, text=text)

    # Topic card with action buttons.
    query_text = topic.shown_description or topic.shown_name
    last = topic.last_sent_at.strftime("%d %b") if topic.last_sent_at else "now"
    card_text = f"📌 *{topic.shown_name}*\n_{query_text}  ·  {last}_"
    await bot.send_message(
        chat_id=topic.telegram_id,
        text=card_text,
        reply_markup=_topic_keyboard(topic),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def _run_digest(topic: Topic, bot: Bot) -> None:
    """Full pipeline for one topic. Raises on failure (caller isolates)."""
    logger.info("digest pipeline: topic={} name={!r}", topic.id, topic.name)

    articles = await research.gather(topic)
    if not articles:
        logger.info("no articles found for {!r}", topic.name)
        return

    fresh, embeddings = await dedup.filter_seen(topic.id, articles, topic.frequency)
    if not fresh:
        logger.info("nothing new for {!r} — skipping", topic.name)
        return

    lookback = research._LOOKBACK.get(topic.frequency, "48 hours")
    stories = await perspectives.cluster(fresh, topic_name=topic.name, lookback=lookback)
    stories = await propaganda.analyze(stories, sides=topic.sides)
    language = await store.get_user_language(topic.telegram_id)
    output = await digest.generate(stories, topic.feedback_notes, language)

    await store.record_seen(topic.id, fresh, embeddings)
    await store.stamp_sent(topic.id)
    await _send_digest(bot, topic, output)

    logger.info("digest sent: topic={}", topic.id)


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------


async def run_due_digests(bot: Bot, *, force: bool = False) -> int:
    """Run the digest pipeline for every topic that is due now.

    force=True ignores the clock — useful for `elephant-cron` in dev.
    Returns the number of digests successfully sent.
    """
    sent = 0
    for topic in await store.get_all_active_topics():
        now_local = datetime.now(ZoneInfo(topic.timezone))
        if not force and not is_due(topic, now_local):
            continue
        try:
            await _run_digest(topic, bot)
            sent += 1
        except Exception:  # noqa: BLE001
            logger.exception("digest failed for topic {} ({!r})", topic.id, topic.name)
    return sent
