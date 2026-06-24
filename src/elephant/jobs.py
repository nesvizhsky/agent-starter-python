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
from elephant.models import Article, Slot, Topic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TG_MAX = 4096  # Telegram hard limit for text messages
_DEFAULT_LOOKBACK_HOURS = 48  # used for manual /check, which isn't tied to a slot


# ---------------------------------------------------------------------------
# Due-check logic (pure — no I/O)
# ---------------------------------------------------------------------------


def due_slots(topic: Topic, now_local: datetime) -> list[Slot]:
    """Which of this topic's slots (if any) should fire at `now_local`?

    Each slot tracks its own last_sent_at, so slots never interfere with each
    other — a topic can have two "every day" slots at different times (an
    arbitrary-gap twice-daily), or several single-day slots, all independent.
    """
    if topic.paused:
        return []

    due: list[Slot] = []
    for slot in topic.slots:
        if now_local.hour != slot.hour:
            continue
        if slot.days and now_local.weekday() not in slot.days:
            continue
        if slot.last_sent_at is None:
            due.append(slot)
            continue
        last = slot.last_sent_at.astimezone(ZoneInfo(topic.timezone))
        if slot.every_n_weeks >= 1:
            if (now_local.date() - last.date()).days >= slot.every_n_weeks * 7:
                due.append(slot)
        elif last.date() < now_local.date():
            due.append(slot)
    return due


def _lookback_hours(topic: Topic, slot: Slot | None, now_local: datetime) -> int:
    """How far back should this run search for content?

    Derived from the triggering slot's own cadence rather than a fixed table,
    so it stays correct for any day/time combination a user picks.
    """
    if slot is None:
        return _DEFAULT_LOOKBACK_HOURS
    if not slot.days:
        # Every-day slot. If the topic has more than one (e.g. twice daily at
        # arbitrary times), look back less so each send only covers its own gap.
        every_day_slots = sum(1 for s in topic.slots if not s.days)
        return 24 if every_day_slots > 1 else _DEFAULT_LOOKBACK_HOURS
    if slot.every_n_weeks >= 1:
        return slot.every_n_weeks * 7 * 24
    # Specific weekday(s), firing every matching day: look back to the most
    # recent prior matching weekday rather than assuming a fixed gap.
    for back in range(1, 8):
        if (now_local.weekday() - back) % 7 in slot.days:
            return back * 24
    return 7 * 24  # unreachable in practice — slot.days is non-empty here


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


async def _run_digest(
    topic: Topic, bot: Bot, *, slot: Slot | None = None, now_local: datetime | None = None
) -> None:
    """Full pipeline for one topic. Raises on failure (caller isolates).

    slot is the checkup that triggered this run (None for a manual /check) —
    it determines how far back to look for fresh content.
    """
    logger.info("digest pipeline: topic={} name={!r}", topic.id, topic.name)

    now_local = now_local or datetime.now(ZoneInfo(topic.timezone))
    lookback_hours = _lookback_hours(topic, slot, now_local)

    articles = await research.gather(topic, lookback_hours)
    fresh: list[Article] = []
    embeddings: list[list[float]] = []
    if articles:
        lookback_days = max(1, lookback_hours // 24)
        fresh, embeddings = await dedup.filter_seen(topic.id, articles, lookback_days)

    if not fresh:
        logger.info("nothing new for {!r}", topic.name)
        # A manual /check or "Check now" tap — the user is actively waiting for
        # SOME response; silence here is indistinguishable from broken (reported
        # in production: "it was showing the searching status but then it
        # disappeared, and that's it"). Scheduled (cron) ticks stay quiet on
        # purpose — notifying every time nothing new happened would be spam.
        if slot is None:
            language = await store.get_user_language(topic.telegram_id)
            output = await digest.generate([], topic.feedback_notes, language)
            await store.stamp_sent(topic.id)
            await _send_digest(bot, topic, output)
        return

    stories = await perspectives.cluster(
        fresh, topic_name=topic.name, lookback=research.lookback_label(lookback_hours)
    )
    stories = await propaganda.analyze(stories, sides=topic.sides)
    language = await store.get_user_language(topic.telegram_id)
    output = await digest.generate(stories, topic.feedback_notes, language)

    await store.record_seen(topic.id, fresh, embeddings)
    await store.stamp_sent(topic.id)
    if slot is not None and slot.id is not None:
        await store.stamp_slot_sent(slot.id)
    await _send_digest(bot, topic, output)

    logger.info("digest sent: topic={}", topic.id)


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------


async def run_due_digests(bot: Bot, *, force: bool = False) -> int:
    """Run the digest pipeline for every due checkup slot, across all topics.

    force=True ignores the clock and sends one digest per topic regardless of
    schedule — useful for `elephant-cron` in dev. Returns the number of
    digests successfully sent.
    """
    sent = 0
    for topic in await store.get_all_active_topics():
        now_local = datetime.now(ZoneInfo(topic.timezone))
        slots: list[Slot | None] = [None] if force else list(due_slots(topic, now_local))
        for slot in slots:
            try:
                await _run_digest(topic, bot, slot=slot, now_local=now_local)
                sent += 1
            except Exception:  # noqa: BLE001
                logger.exception("digest failed for topic {} ({!r})", topic.id, topic.name)
    return sent
