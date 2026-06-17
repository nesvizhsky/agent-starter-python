"""Telegram bot: all handlers and Application wiring for Disputatio.

Run locally with long polling (no public URL needed):

    uv run disputatio-bot

In production the same handlers run via webhook — see app.py.
"""

from __future__ import annotations

from loguru import logger
from pydantic_ai import Agent
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from agent.config import get_settings
from agent.logging_setup import setup_logging
from agent.services.llm import build_model
from disputatio import jobs, store
from disputatio.personas import all_keys

# ---------------------------------------------------------------------------
# ConversationHandler states for /add_topic
# ---------------------------------------------------------------------------

_ASK_DESC, _ASK_NAME_CONFIRM, _ASK_FREQ, _ASK_SOURCES = range(4)

# ---------------------------------------------------------------------------
# Name-generation agent (fast + cheap — just makes a 2-4 word label)
# ---------------------------------------------------------------------------

_name_agent: Agent[None, str] = Agent(
    build_model("fast"),
    output_type=str,
    system_prompt=(
        "Generate a short topic label (2–4 words, title case) from the user's description. "
        "Return ONLY the label — no quotes, no punctuation, no explanation. "
        "Examples: 'Russia-Ukraine War', 'AI Regulation', 'Megalithic Archaeology', 'Climate Policy'."  # noqa: E501
    ),
)


async def _generate_name(description: str) -> str:
    result = await _name_agent.run(description)
    return result.output.strip()


_VALID_SIGNALS = {"good", "too_shallow", "too_long", "already_knew", "wrong_persona"}

# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _short(name: str, limit: int = 40) -> str:
    """Truncate long topic names for display in status messages."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


async def _allowed(update: Update) -> bool:
    allow = get_settings().allowed_ids
    user = update.effective_user
    if allow and (user is None or user.id not in allow):
        if update.message:
            await update.message.reply_text("This bot isn't open to the public yet.")
        return False
    return True


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

_WELCOME = (
    "Welcome to *Disputatio* — your multi-perspective news intelligence bot.\n\n"
    "I track topics you care about and deliver regular digests showing how different "
    "sources frame the same events — including rhetoric and propaganda signals.\n\n"
    "To get started: /add\\_topic\n\n"
    "Commands:\n"
    "/add\\_topic — track a new topic\n"
    "/topics — list your topics\n"
    "/check — get a digest right now\n"
    "/more — read the full analysis from the last digest\n"
    "/synthesis — weekly synthesis for a topic\n"
    "/pause — pause a topic\n"
    "/resume — resume a topic\n"
    "/add\\_source — add a source to a topic\n"
    "/del\\_source — remove a source"
)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    await store.get_or_create_user(tg.id, tg.first_name, tg.username)
    await update.message.reply_text(_WELCOME, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /add_topic conversation
# ---------------------------------------------------------------------------


async def cmd_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not await _allowed(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "What do you want to track?\n\nDescribe it in a sentence — I'll suggest a name."
    )
    return _ASK_DESC


async def _got_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text or context.user_data is None:
        return _ASK_DESC
    desc = update.message.text.strip()
    if not desc:
        await update.message.reply_text("Please describe what you want to track.")
        return _ASK_DESC
    context.user_data["new_topic_desc"] = desc

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        name = await _generate_name(desc)
    except Exception:  # noqa: BLE001
        name = " ".join(desc.split()[:4]).rstrip(".,!?")

    context.user_data["new_topic_name"] = name
    await update.message.reply_text(
        f"📌 *{name}*\n\nLooks good as the topic name?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Continue →", callback_data="name:ok"),
                    InlineKeyboardButton("Rename it", callback_data="name:rename"),
                ]
            ]
        ),
        parse_mode="Markdown",
    )
    return _ASK_NAME_CONFIRM


async def _name_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        name = context.user_data.get("new_topic_name", "") if context.user_data else ""
        await query.edit_message_text(f"✓ *{name}*", parse_mode="Markdown")
    return await _ask_freq(update, context)


async def _name_rename_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Type a short label for this topic:")
    return _ASK_NAME_CONFIRM


async def _got_custom_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text or context.user_data is None:
        return _ASK_NAME_CONFIRM
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please type a short label.")
        return _ASK_NAME_CONFIRM
    context.user_data["new_topic_name"] = name
    return await _ask_freq(update, context)


async def _ask_freq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Daily", callback_data="freq:daily"),
                InlineKeyboardButton("Twice daily", callback_data="freq:twice_daily"),
                InlineKeyboardButton("Weekly", callback_data="freq:weekly"),
            ]
        ]
    )
    msg = "How often do you want updates?"
    if update.message:
        await update.message.reply_text(msg, reply_markup=keyboard)
    elif update.callback_query and update.callback_query.message:
        from telegram import Message as TGMessage

        cq_msg = update.callback_query.message
        if isinstance(cq_msg, TGMessage):
            await cq_msg.reply_text(msg, reply_markup=keyboard)
    return _ASK_FREQ


async def _got_freq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or context.user_data is None:
        return _ASK_FREQ
    await query.answer()
    freq = query.data.split(":")[1] if query.data else "daily"
    context.user_data["new_topic_freq"] = freq
    await query.edit_message_text(
        f"Frequency: *{freq.replace('_', ' ')}*.\n\n"
        "Any specific sources to track? Send a comma-separated list "
        "(e.g. *BBC, TASS, Al Jazeera*) or tap Skip.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Skip", callback_data="sources:skip")]]
        ),
        parse_mode="Markdown",
    )
    return _ASK_SOURCES


async def _got_sources_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return _ASK_SOURCES
    raw = update.message.text.strip()
    sources = [s.strip() for s in raw.split(",") if s.strip()]
    return await _create_topic(update, context, sources)


async def _got_sources_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("No specific sources — I'll cast a wide net.")
    return await _create_topic(update, context, [])


async def _create_topic(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sources: list[str]
) -> int:
    tg = update.effective_user
    if tg is None or context.user_data is None:
        return ConversationHandler.END
    name = context.user_data.pop("new_topic_name", "")
    desc = context.user_data.pop("new_topic_desc", None)
    freq = context.user_data.pop("new_topic_freq", "daily")
    topic = await store.create_topic(
        tg.id, name=name, description=desc, sources=sources, frequency=freq
    )
    msg = (
        f"✓ Topic *{topic.name}* created.\n"
        f"Frequency: {freq.replace('_', ' ')}\n"
        f"Sources: {', '.join(sources) if sources else 'general'}\n\n"
        "Use /check to get a digest right now."
    )
    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown")  # type: ignore[union-attr]
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data is not None:
        context.user_data.pop("new_topic_name", None)
        context.user_data.pop("new_topic_desc", None)
        context.user_data.pop("new_topic_freq", None)
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Topic picker — shared across /check, /pause, /resume, /synthesis
# ---------------------------------------------------------------------------


async def _topic_picker(
    update: Update,
    telegram_id: int,
    action: str,
    prompt: str,
) -> None:
    """Send an inline keyboard listing all topics for the given action."""
    topics = await store.get_topics(telegram_id)
    if not topics:
        if update.message:
            await update.message.reply_text(
                "You have no topics yet. Use /add\\_topic to create one.",
                parse_mode="Markdown",
            )
        return
    buttons = [[InlineKeyboardButton(t.name, callback_data=f"ta:{action}:{t.id}")] for t in topics]
    if update.message:
        await update.message.reply_text(prompt, reply_markup=InlineKeyboardMarkup(buttons))


async def on_topic_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback handler for topic picker buttons (ta:{action}:{topic_id})."""
    query = update.callback_query
    if query is None or query.from_user is None or query.data is None:
        return
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    _, action, topic_id_str = parts

    from uuid import UUID

    topic = await store.get_topic(query.from_user.id, UUID(topic_id_str))
    if topic is None:
        await query.edit_message_text("Topic not found.")
        return

    if action == "check":
        await query.edit_message_text(
            f"Running digest for *{_short(topic.name)}*…", parse_mode="Markdown"
        )
        try:
            await jobs._run_digest(topic, context.bot)
        except Exception:  # noqa: BLE001
            logger.exception("picker /check failed for topic {}", topic.id)
            await query.message.reply_text("Something went wrong — try again in a moment.")  # type: ignore[union-attr]

    elif action == "synthesis":
        await query.edit_message_text("Synthesising the week…")
        try:
            await jobs._run_synthesis(topic, context.bot)
        except Exception:  # noqa: BLE001
            logger.exception("picker /synthesis failed for topic {}", topic.id)
            await query.message.reply_text("Synthesis failed — try again.")  # type: ignore[union-attr]

    elif action == "pause":
        await store.update_topic(topic.id, paused=True)
        await query.edit_message_text(f"⏸ *{topic.name}* paused.", parse_mode="Markdown")

    elif action == "resume":
        await store.update_topic(topic.id, paused=False)
        await query.edit_message_text(f"▶ *{topic.name}* resumed.", parse_mode="Markdown")

    elif action == "reset":
        n = await store.clear_seen(topic.id)
        await query.edit_message_text(
            f"✓ Cleared {n} seen articles for *{_short(topic.name)}*. "
            "Next /check will fetch fresh content.",
            parse_mode="Markdown",
        )


# ---------------------------------------------------------------------------
# /topics
# ---------------------------------------------------------------------------


async def cmd_topics(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    topics = await store.get_topics(tg.id)
    if not topics:
        await update.message.reply_text(
            "You have no topics yet. Use /add\\_topic to create one.", parse_mode="Markdown"
        )
        return
    lines = ["*Your topics:*\n"]
    for t in topics:
        status = "⏸ paused" if t.paused else f"▶ {t.frequency.replace('_', ' ')}"
        last = t.last_sent_at.strftime("%d %b %H:%M") if t.last_sent_at else "never"
        lines.append(f"• *{t.name}* — {status} — last sent: {last}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /check <name>
# ---------------------------------------------------------------------------


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    name = " ".join(context.args or []).strip()
    if not name:
        await _topic_picker(update, tg.id, "check", "Which topic?")
        return
    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text(
        f"Running digest for *{_short(topic.name)}*…", parse_mode="Markdown"
    )
    try:
        await jobs._run_digest(topic, context.bot)
    except Exception:  # noqa: BLE001
        logger.exception("/check failed for topic {}", topic.id)
        await update.message.reply_text("Something went wrong — try again in a moment.")


# ---------------------------------------------------------------------------
# /more
# ---------------------------------------------------------------------------


async def cmd_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    overflow = jobs.get_overflow(tg.id)
    if not overflow:
        await update.message.reply_text(
            "No extended analysis stored — request a fresh digest with /check."
        )
        return
    await update.message.reply_text(overflow)


# ---------------------------------------------------------------------------
# /reset  — clear seen articles so the next /check fetches fresh content
# ---------------------------------------------------------------------------


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    name = " ".join(context.args or []).strip()
    if not name:
        await _topic_picker(update, tg.id, "reset", "Reset which topic?")
        return
    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return
    n = await store.clear_seen(topic.id)
    await update.message.reply_text(
        f"✓ Cleared {n} seen articles for *{_short(topic.name)}*. "
        "Next /check will fetch fresh content.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /rename — give a topic a shorter display name
# ---------------------------------------------------------------------------


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /rename <current name> | <new short label>\n"
            "Example: /rename Russia-Ukraine | War in Ukraine",
            parse_mode="Markdown",
        )
        return
    raw = " ".join(args)
    if "|" not in raw:
        await update.message.reply_text(
            "Separate the current name and new label with `|`.\n"
            "Example: /rename Russia-Ukraine | War",
            parse_mode="Markdown",
        )
        return
    old_name, new_name = (p.strip() for p in raw.split("|", 1))
    topic = await store.get_topic_by_name(tg.id, old_name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{old_name}*.", parse_mode="Markdown")
        return
    updates: dict[str, object] = {"name": new_name}
    if not topic.description:
        updates["description"] = topic.name
    await store.update_topic(topic.id, **updates)
    await update.message.reply_text(f"✓ Renamed to *{new_name}*.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /synthesis <name>
# ---------------------------------------------------------------------------


async def cmd_synthesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    name = " ".join(context.args or []).strip()
    if not name:
        await _topic_picker(update, tg.id, "synthesis", "Which topic?")
        return
    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("Synthesising the week…")
    try:
        await jobs._run_synthesis(topic, context.bot)
    except Exception:  # noqa: BLE001
        logger.exception("/synthesis failed for topic {}", topic.id)
        await update.message.reply_text("Synthesis failed — try again in a moment.")


# ---------------------------------------------------------------------------
# /pause and /resume
# ---------------------------------------------------------------------------


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    name = " ".join(context.args or []).strip()
    if not name:
        await _topic_picker(update, tg.id, "pause", "Which topic to pause?")
        return
    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return
    await store.update_topic(topic.id, paused=True)
    await update.message.reply_text(f"⏸ *{topic.name}* paused.", parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    name = " ".join(context.args or []).strip()
    if not name:
        await _topic_picker(update, tg.id, "resume", "Which topic to resume?")
        return
    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return
    await store.update_topic(topic.id, paused=False)
    await update.message.reply_text(f"▶ *{topic.name}* resumed.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /add_source and /del_source
# ---------------------------------------------------------------------------


async def cmd_add_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None or not context.args or len(context.args) < 2:
        if update.message:
            await update.message.reply_text("Usage: /add_source <topic name> <source>")
        return
    # Last word is the source, everything before is the topic name
    *name_parts, source = context.args
    name = " ".join(name_parts)
    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return
    if source not in topic.sources:
        await store.update_topic(topic.id, sources=[*topic.sources, source])
    await update.message.reply_text(f"Added *{source}* to *{topic.name}*.", parse_mode="Markdown")


async def cmd_del_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None or not context.args or len(context.args) < 2:
        if update.message:
            await update.message.reply_text("Usage: /del_source <topic name> <source>")
        return
    *name_parts, source = context.args
    name = " ".join(name_parts)
    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return
    updated = [s for s in topic.sources if s != source]
    await store.update_topic(topic.id, sources=updated)
    await update.message.reply_text(
        f"Removed *{source}* from *{topic.name}*.", parse_mode="Markdown"
    )


# ---------------------------------------------------------------------------
# /persona — pin a specific persona to a topic
# ---------------------------------------------------------------------------


async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None or not context.args or len(context.args) < 2:
        keys = ", ".join(all_keys())
        if update.message:
            await update.message.reply_text(
                f"Usage: /persona <topic name> <persona key>\n\nAvailable: {keys}"
            )
        return
    *name_parts, key = context.args
    name = " ".join(name_parts)
    if key not in all_keys():
        await update.message.reply_text(
            f"Unknown persona *{key}*. Available: {', '.join(all_keys())}",
            parse_mode="Markdown",
        )
        return
    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return
    await store.update_topic(topic.id, pinned_persona=key)
    await update.message.reply_text(f"Pinned *{key}* to *{topic.name}*.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Feedback callback (digest reaction buttons)
# ---------------------------------------------------------------------------

_NEGATIVE_SIGNALS = {"too_shallow", "too_long", "already_knew", "wrong_persona"}


async def on_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None or query.data is None:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3:
        return
    _, signal, digest_id_str = parts
    if signal not in _VALID_SIGNALS:
        return

    telegram_id = query.from_user.id
    ids = jobs.get_last_digest_ids(telegram_id)
    topic_id = ids[0] if ids else None

    try:
        from uuid import UUID

        await store.save_feedback(
            telegram_id=telegram_id,
            topic_id=topic_id,
            digest_id=UUID(digest_id_str),
            signal=signal,
            note=None,
        )
    except Exception:  # noqa: BLE001
        logger.warning("failed to save feedback signal={} digest={}", signal, digest_id_str)

    if signal == "good":
        await query.edit_message_reply_markup(reply_markup=None)
    else:
        # Offer extended correction
        if context.user_data is not None:
            context.user_data["awaiting_correction_topic"] = topic_id
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(  # type: ignore[union-attr]
            "Got it. Want to tell me more? Type a correction or /skip."
        )


# ---------------------------------------------------------------------------
# Free-text handler (corrections and /skip)
# ---------------------------------------------------------------------------


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text or not await _allowed(update):
        return
    text = update.message.text.strip()
    ud = context.user_data
    topic_id = ud.get("awaiting_correction_topic") if ud is not None else None

    if topic_id is not None and ud is not None:
        ud.pop("awaiting_correction_topic")
        if text.lower() != "/skip":
            try:
                await store.append_feedback_note(topic_id, text)
                await update.message.reply_text("✓ Noted — I'll adjust future digests.")
            except Exception:  # noqa: BLE001
                logger.warning("failed to save correction for topic {}", topic_id)
                await update.message.reply_text("Couldn't save that — try again later.")
        else:
            await update.message.reply_text("No problem, skipped.")
        return

    await update.message.reply_text(
        "Use commands to interact:\n"
        "/add\\_topic · /topics · /check · /more · /synthesis · /pause · /resume",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------


async def _post_init(app: Application) -> None:  # type: ignore[type-arg]
    await store.apply_migrations()
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Welcome / help"),
            BotCommand("add_topic", "Track a new topic"),
            BotCommand("topics", "List your topics"),
            BotCommand("check", "Get a digest now: /check <topic>"),
            BotCommand("more", "Full analysis from last digest"),
            BotCommand("synthesis", "Weekly synthesis: /synthesis <topic>"),
            BotCommand("pause", "Pause updates: /pause <topic>"),
            BotCommand("resume", "Resume updates: /resume <topic>"),
            BotCommand("add_source", "Add a source: /add_source <topic> <source>"),
            BotCommand("del_source", "Remove a source: /del_source <topic> <source>"),
            BotCommand("persona", "Pin a persona: /persona <topic> <key>"),
            BotCommand("reset", "Clear seen articles: /reset <topic>"),
            BotCommand("rename", "Rename a topic: /rename <old> | <new>"),
        ]
    )


def build_application() -> Application:  # type: ignore[type-arg]
    """Build the PTB Application with all handlers. Used by both polling and webhook."""
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    app = ApplicationBuilder().token(token).post_init(_post_init).build()

    add_topic_conv = ConversationHandler(
        entry_points=[CommandHandler("add_topic", cmd_add_topic)],
        states={
            _ASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, _got_desc)],
            _ASK_NAME_CONFIRM: [
                CallbackQueryHandler(_name_confirmed, pattern=r"^name:ok"),
                CallbackQueryHandler(_name_rename_prompt, pattern=r"^name:rename"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_custom_name),
            ],
            _ASK_FREQ: [CallbackQueryHandler(_got_freq, pattern=r"^freq:")],
            _ASK_SOURCES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_sources_text),
                CallbackQueryHandler(_got_sources_skip, pattern=r"^sources:skip"),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        per_message=False,
    )

    app.add_handler(add_topic_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("more", cmd_more))
    app.add_handler(CommandHandler("synthesis", cmd_synthesis))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("add_source", cmd_add_source))
    app.add_handler(CommandHandler("del_source", cmd_del_source))
    app.add_handler(CommandHandler("persona", cmd_persona))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CallbackQueryHandler(on_topic_action, pattern=r"^ta:"))
    app.add_handler(CallbackQueryHandler(on_feedback, pattern=r"^fb:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app


def main() -> None:
    setup_logging()
    logger.info("starting disputatio bot (polling)")
    build_application().run_polling()


if __name__ == "__main__":
    main()
