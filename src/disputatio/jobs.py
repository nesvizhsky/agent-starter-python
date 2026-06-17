"""Cron job runners for Disputatio.

`is_due` and `_is_synthesis_due` are pure functions (unit-testable offline).
`run_due_digests` / `run_due_syntheses` loop all active topics, check due-ness,
run the pipeline, and isolate per-topic failures.

The overflow text from each digest is stored in a module-level dict keyed by
telegram_id so the /more handler in bot.py can serve it. It's ephemeral (reset
on restart), which is acceptable for MVP.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from disputatio import dedup, digest, perspectives, propaganda, research, store, synthesis
from disputatio.models import Topic
from disputatio.personas import Persona, avatar_url, pick
from disputatio.synthesis import SynthesisOutput

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TG_MAX = 4096  # Telegram hard limit for text messages

# ---------------------------------------------------------------------------
# Module-level caches (ephemeral — reset on restart)
# ---------------------------------------------------------------------------

# {telegram_id: overflow_text}
_overflow: dict[int, str] = {}

# {telegram_id: (topic_id, digest_id)} — the most recently sent digest per user
_last_digest: dict[int, tuple[UUID, UUID]] = {}


def get_overflow(telegram_id: int) -> str | None:
    return _overflow.get(telegram_id)


def get_last_digest_ids(telegram_id: int) -> tuple[UUID, UUID] | None:
    """Return (topic_id, digest_id) for the last digest sent to this user."""
    return _last_digest.get(telegram_id)


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

    if topic.last_sent_at is None:
        return True

    last = topic.last_sent_at.astimezone(ZoneInfo(topic.timezone))

    match topic.frequency:
        case "daily":
            return last.date() < now_local.date()
        case "twice_daily":
            # 10h buffer avoids double-sending on clock-edge ticks
            return (now_local - last).total_seconds() >= 10 * 3600
        case "weekly":
            return (
                now_local.weekday() == topic.send_dow and (now_local.date() - last.date()).days >= 7
            )
        case _:
            return False


def _is_synthesis_due(topic: Topic, now_local: datetime) -> bool:
    if topic.paused or now_local.hour != topic.send_hour:
        return False
    if topic.last_synthesis_at is None:
        return False  # never synthesised → need at least one week of digests first
    last = topic.last_synthesis_at.astimezone(ZoneInfo(topic.timezone))
    return (now_local.date() - last.date()).days >= 7


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------


def _feedback_keyboard(digest_id: UUID) -> InlineKeyboardMarkup:
    d = str(digest_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍 Good", callback_data=f"fb:good:{d}"),
                InlineKeyboardButton("👎 Not useful", callback_data=f"fb:bad:{d}"),
            ],
            [
                InlineKeyboardButton("📚 Too shallow", callback_data=f"fb:too_shallow:{d}"),
                InlineKeyboardButton("📖 Too long", callback_data=f"fb:too_long:{d}"),
                InlineKeyboardButton("🎭 Wrong voice", callback_data=f"fb:wrong_persona:{d}"),
            ],
        ]
    )


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


async def _send_digest(
    bot: Bot,
    topic: Topic,
    persona: Persona,
    output: digest.DigestOutput,
    digest_id: UUID,
) -> None:
    try:
        url = avatar_url(persona)
        await bot.send_photo(
            chat_id=topic.telegram_id,
            photo=url,
            caption=f"*{persona.name}* — {persona.intro}",
            parse_mode="Markdown",
        )
    except Exception:  # noqa: BLE001
        logger.debug("avatar photo failed for {} — sending text only", persona.key)

    byline = f"<b>{persona.name}</b> · <i>{persona.label}</i>\n\n"
    chunks = _chunk_text(output.main)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        text = (byline + chunk) if i == 0 else chunk
        has_feedback = is_last and not output.character_note
        kwargs = {
            "chat_id": topic.telegram_id,
            "text": text,
            "reply_markup": _feedback_keyboard(digest_id) if has_feedback else None,
        }
        try:
            await bot.send_message(**kwargs, parse_mode="HTML")
        except BadRequest:
            logger.warning("HTML parse failed for chunk {}/{} — retrying plain", i + 1, len(chunks))
            await bot.send_message(**kwargs)

    if output.character_note:
        note = f"<i>{output.character_note}</i>"
        kwargs_note = {
            "chat_id": topic.telegram_id,
            "text": note,
            "reply_markup": _feedback_keyboard(digest_id),
        }
        try:
            await bot.send_message(**kwargs_note, parse_mode="HTML")
        except BadRequest:
            await bot.send_message(**kwargs_note)


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
    stories = await propaganda.analyze(stories)
    persona = pick(topic.pinned_persona)
    output = await digest.generate(stories, persona, topic.feedback_notes)

    digest_id = await store.record_digest(topic.id, topic.telegram_id, output.main, persona.key)
    await store.record_seen(topic.id, fresh, embeddings)
    await store.stamp_sent(topic.id)

    await _send_digest(bot, topic, persona, output, digest_id)

    if output.overflow:
        _overflow[topic.telegram_id] = output.overflow
    _last_digest[topic.telegram_id] = (topic.id, digest_id)

    logger.info("digest sent: topic={} persona={}", topic.id, persona.key)


async def _run_synthesis(topic: Topic, bot: Bot) -> None:
    """Weekly synthesis for one topic. Raises on failure (caller isolates)."""
    logger.info("synthesis pipeline: topic={} name={!r}", topic.id, topic.name)
    digests = await store.get_recent_digests(topic.id, days=7)
    result = await synthesis.generate(topic.name, digests)
    if result is None:
        logger.info("not enough digests to synthesise {!r}", topic.name)
        return
    await bot.send_message(
        chat_id=topic.telegram_id,
        text=_format_synthesis(result),
        parse_mode="Markdown",
    )
    await store.stamp_synthesis(topic.id)
    logger.info("synthesis sent for topic {}", topic.id)


# ---------------------------------------------------------------------------
# Public runners
# ---------------------------------------------------------------------------


async def run_due_digests(bot: Bot, *, force: bool = False) -> int:
    """Run the digest pipeline for every topic that is due now.

    force=True ignores the clock — useful for `disputatio-cron` in dev.
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


async def run_due_syntheses(bot: Bot, *, force: bool = False) -> int:
    """Run weekly synthesis for every topic that is due."""
    sent = 0
    for topic in await store.get_all_active_topics():
        now_local = datetime.now(ZoneInfo(topic.timezone))
        if not force and not _is_synthesis_due(topic, now_local):
            continue
        try:
            await _run_synthesis(topic, bot)
            sent += 1
        except Exception:  # noqa: BLE001
            logger.exception("synthesis failed for topic {} ({!r})", topic.id, topic.name)
    return sent


# ---------------------------------------------------------------------------
# Synthesis formatting
# ---------------------------------------------------------------------------


def _format_synthesis(s: SynthesisOutput) -> str:
    parts: list[str] = ["📊 *Weekly Synthesis*\n"]
    if s.contested_facts:
        parts.append("*Contested this week:*")
        parts.extend(f"• {f}" for f in s.contested_facts)
    if s.confirmed_facts:
        parts.append("\n*What solidified:*")
        parts.extend(f"• {f}" for f in s.confirmed_facts)
    parts.append(f"\n*How the story shifted:*\n{s.narrative_drift}")
    if s.source_patterns:
        parts.append("\n*Source patterns:*")
        parts.extend(f"• {p}" for p in s.source_patterns)
    parts.append(f"\n*In sum:* {s.summary}")
    return "\n".join(parts)
