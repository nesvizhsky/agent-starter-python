"""Telegram bot: all handlers and Application wiring for Eat the Elephant.

Run locally with long polling (no public URL needed):

    uv run elephant-bot

In production the same handlers run via webhook — see app.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from loguru import logger
from pydantic_ai import Agent
from telegram import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
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
from elephant import jobs, research, store
from elephant.models import Slot, Topic

# ---------------------------------------------------------------------------
# ConversationHandler states (shared by /add_topic and /schedule)
# ---------------------------------------------------------------------------

_AT_DESC = 0  # /add_topic: enter description
_AT_NAME_CONFIRM = 1  # /add_topic: confirm LLM-generated name
_ASK_SCHED_TYPE = 2  # both: pick frequency type
_ASK_SCHED_DAYS = 3  # both: pick days (custom/weekly/biweekly)
_ASK_TIME = 4  # both: type a time
_AT_SOURCES = 6  # /add_topic only: enter sources
_SC_PICK = 7  # /schedule: topic picker
_ASK_ADD_ANOTHER = 8  # both: "add another checkup time?" after each slot


async def _safe_answer(query: CallbackQuery, text: str = "", show_alert: bool = False) -> None:
    """Acknowledge a callback query, tolerating an expired/invalid query id.

    Telegram invalidates callback queries a few seconds after they're sent. If the
    bot was busy (e.g. running a digest pipeline) when a click arrived, answering it
    can fail — but the button's actual action should still run, so this failure must
    never abort the handler.
    """
    with contextlib.suppress(BadRequest):
        await query.answer(text, show_alert=show_alert)


async def _safe_edit_text(query: CallbackQuery, text: str, **kwargs: object) -> None:
    """Edit a callback query's message, tolerating Telegram's "message is not
    modified" error — happens on a double-tap or retry where the new content is
    byte-for-byte identical to what's already shown, which is harmless, not a
    real failure, and was previously surfacing as an unhandled exception.

    Only that specific error is swallowed — anything else (e.g. a Markdown
    parse error from unescaped user input) still raises, since those are real
    bugs we want to see, not silently lose.
    """
    try:
        await query.edit_message_text(text, **kwargs)  # type: ignore[arg-type]
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


# ---------------------------------------------------------------------------
# Name-generation agent (fast + cheap — just makes a 2-4 word label)
# ---------------------------------------------------------------------------

_name_agent: Agent[None, str] = Agent(
    build_model("fast"),
    output_type=str,
    system_prompt=(
        "Generate a short topic label (2–4 words, title case) from the user's description. "
        "Respond in the SAME language the user wrote in — never translate to English. "
        "Return ONLY the label — no quotes, no punctuation, no explanation. "
        "Examples (English input only, for style — match the user's own language otherwise): "
        "'Russia-Ukraine War', 'AI Regulation', 'Megalithic Archaeology', 'Climate Policy'."
    ),
)


async def _generate_name(description: str) -> str:
    result = await _name_agent.run(description)
    return result.output.strip()


# ---------------------------------------------------------------------------
# Schedule constants
# ---------------------------------------------------------------------------

# freq -> _UI key, so the "✓ ..." confirmation after picking a schedule type
# is localized too, not just the button that triggered it.
_SCHED_TYPE_UI_KEYS: dict[str, str] = {
    "daily": "sched_daily",
    "twice_daily": "sched_twice_daily",
    "custom_days": "sched_custom_days",
    "biweekly": "sched_biweekly",
    "manual": "sched_manual_btn",
}

# ---------------------------------------------------------------------------
# UI localisation — English source strings only.
# All other languages are translated on demand by _ensure_ui() via LLM.
# ---------------------------------------------------------------------------

# Keys starting with "sched_" are schedule-type labels.
# Keys "dow_0"…"dow_6" are full day names; "dows_0"…"dows_6" are short.
# "topics_header" uses {n} as the count placeholder.
_UI: dict[str, str] = {
    # Messages
    "fetch_status": "⏳ *Fetching digest for {name}…*\n_This takes 1–2 minutes — feel free to keep using me meanwhile._",  # noqa: E501
    "fetch_card": "⏳ _Fetching digest…_",
    "err_generic": "Something went wrong — try again.",
    "err_moment": "Something went wrong — try again in a moment.",
    "no_topics": "You have no topics yet. Use /add\\_topic to create one.",
    "topic_nf": "Topic not found.",
    "cancelled": "Cancelled.",
    "paused_lbl": "⏸ paused",
    "never_sent": "never sent",
    "cleared_n": "✓ Cleared {n} seen articles for *{name}*. Use /check to fetch fresh results.",
    "topics_header": "Your {n} topic(s):",
    "welcome": "Welcome to *Eat the Elephant* 🐘\n\nTrack any topic. Get daily briefings from sources that disagree. One bite at a time.",  # noqa: E501
    "btn_add_topic": "➕ Add topic",
    "btn_my_topics": "📋 My topics",
    "btn_check_now": "▶ Check now",
    "no_topics_short": "You have no topics yet.",
    "no_topic_named": "No topic called *{name}*.",
    # Schedule type labels
    "sched_daily": "Every day",
    "sched_twice_daily": "Twice daily",
    "sched_weekdays": "Mon–Fri",
    "sched_mwf": "Mon/Wed/Fri",
    "sched_tuth": "Tue/Thu",
    "sched_custom_days": "Pick days…",
    "sched_weekly": "Once a week",
    "sched_biweekly": "Every 2 weeks",
    # Day-of-week full names (Mon=0 … Sun=6)
    "dow_0": "Monday",
    "dow_1": "Tuesday",
    "dow_2": "Wednesday",
    "dow_3": "Thursday",
    "dow_4": "Friday",
    "dow_5": "Saturday",
    "dow_6": "Sunday",
    # Day-of-week short names
    "dows_0": "Mon",
    "dows_1": "Tue",
    "dows_2": "Wed",
    "dows_3": "Thu",
    "dows_4": "Fri",
    "dows_5": "Sat",
    "dows_6": "Sun",
    # /add_topic + /schedule conversation flow
    "add_topic_prompt": "What do you want to track?\n\nDescribe it in a sentence — I'll suggest a name.",  # noqa: E501
    "desc_empty": "Please describe what you want to track.",
    "topic_confirm": "📌 *{name}*\n🔍 _{desc}_\n\nLooks good?",
    "btn_continue": "Continue →",
    "btn_rename": "Rename it",
    "rename_prompt": "Type a short label for this topic:",
    "rename_empty": "Please type a short label.",
    "reschedule_picker": "Which topic to reschedule?",
    "changing_schedule": "Changing schedule…",
    "sched_another_prompt": "Add another checkup — how often?",
    "sched_first_prompt": "How often would you like updates?",
    "sched_manual_btn": "🔕 No schedule — check manually",
    "nav_back": "← Back",
    "add_another_prompt": "Add another checkup time?",
    "sched_days_multi_prompt": "Which days? Tap to select, then tap *Done ✓*.",
    "sched_days_single_prompt": "Which day? (once every 2 weeks)",
    "btn_done_check": "Done ✓",
    "pick_one_day": "Pick at least one day.",
    "time_second_prompt": "What's the second time?",
    "time_first_prompt": "What's the first time?",
    "time_prompt": "What time?",
    "time_instructions": "{prompt} Type it, e.g. *9:00* or *21:30*\n(your local time, 24-hour or am/pm)",  # noqa: E501
    "btn_add_another": "+ Add another",
    "adding_another": "Adding another checkup time…",
    "done_short": "Done",
    "schedule_updated": "✓ Schedule updated for *{name}*:\n{label}",
    "tz_current": "Your current timezone: *{label}*\n\nPick a new one — this updates every topic, not just one:",  # noqa: E501
    "tz_set_confirm": "✓ Timezone set to {label} for all your topics.",
    "sources_prompt": "Any specific sources to track? Send a comma-separated list (e.g. *BBC, TASS, Al Jazeera*) or tap Skip.",  # noqa: E501
    "btn_skip": "Skip",
    "sources_skipped": "No specific sources — I'll cast a wide net.",
    "sources_general": "general",
    "topic_created": "🎉 *Topic created: {name}*\n━━━━━━━━━━━━━━━\n🗓 {label}  ·  {tz}\n📰 Sources: {sources}",  # noqa: E501
    "timeout_msg": "Timed out waiting for a reply. Send /add\\_topic to start again.",
}

# Two-level cache: {lang: {key: translated_string}}
_ui_cache: dict[str, dict[str, str]] = {"English": _UI}

# Frequency types that require a day-selection step
_NEEDS_DAYS = frozenset({"custom_days", "biweekly"})

# (display label, IANA timezone name) — covers every populated whole-hour UTC
# offset plus the common half/quarter-hour ones, not just a sparse sample.
# Single space (not double) and short city names so labels stay fully
# readable on narrow mobile screens at 2-per-row (see _timezone_keyboard) —
# the old 3-per-row, double-spaced layout got visually truncated by Telegram
# on some clients (e.g. "UTC+8  Singapore" rendering as "utc...gapore").
_TIMEZONES = [
    ("UTC-11 Samoa", "Pacific/Pago_Pago"),
    ("UTC-10 Honolulu", "Pacific/Honolulu"),
    ("UTC-9 Anchorage", "America/Anchorage"),
    ("UTC-8 LA", "America/Los_Angeles"),
    ("UTC-7 Denver", "America/Denver"),
    ("UTC-6 Chicago", "America/Chicago"),
    ("UTC-5 New York", "America/New_York"),
    ("UTC-4 Santiago", "America/Santiago"),
    ("UTC-3 B.Aires", "America/Argentina/Buenos_Aires"),
    ("UTC-1 Azores", "Atlantic/Azores"),
    ("UTC+0 London", "Europe/London"),
    ("UTC+1 Paris", "Europe/Paris"),
    ("UTC+2 Helsinki", "Europe/Helsinki"),
    ("UTC+3 Moscow", "Europe/Moscow"),
    ("UTC+3:30 Tehran", "Asia/Tehran"),
    ("UTC+4 Dubai", "Asia/Dubai"),
    ("UTC+5 Karachi", "Asia/Karachi"),
    ("UTC+5:30 India", "Asia/Kolkata"),
    ("UTC+6 Dhaka", "Asia/Dhaka"),
    ("UTC+7 Bangkok", "Asia/Bangkok"),
    ("UTC+8 Singapore", "Asia/Singapore"),
    ("UTC+9 Tokyo", "Asia/Tokyo"),
    ("UTC+9:30 Adelaide", "Australia/Adelaide"),
    ("UTC+10 Sydney", "Australia/Sydney"),
    ("UTC+11 Noumea", "Pacific/Noumea"),
    ("UTC+12 Auckland", "Pacific/Auckland"),
]

# ---------------------------------------------------------------------------
# UI translations
# ---------------------------------------------------------------------------

_lang_cache: dict[int, str] = {}


async def _ensure_ui(lang: str) -> None:
    """Translate all UI strings to *lang* on first use — one LLM batch call per language.

    Placeholders like {name} or {n} must survive translation intact. We instruct
    the LLM to preserve them, and fall back to English if the result is unusable.
    """
    if lang in _ui_cache:
        return
    import json

    from pydantic_ai import Agent as _A

    from agent.services.llm import build_model as _bm

    agent: _A[None, str] = _A(
        _bm("fast"),
        output_type=str,
        system_prompt=(
            f"Translate all JSON string values into {lang}. "
            "Rules you must follow:\n"
            "- Preserve every {placeholder} in curly braces exactly — do not translate the word inside.\n"  # noqa: E501
            "- Preserve Telegram Markdown symbols (* _ ` \\) exactly.\n"
            "- Preserve emoji characters exactly.\n"
            "- Return ONLY valid JSON with the same keys, no extra text or code fences."
        ),
    )
    try:
        result = await agent.run(json.dumps(_UI, ensure_ascii=False))
        raw = result.output.strip()
        # LLMs sometimes wrap JSON in code fences despite instructions
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        translated = json.loads(raw)
        _ui_cache[lang] = {k: str(v) for k, v in translated.items() if isinstance(v, str)}
        # Fill any missing keys with English fallback
        for k, v in _UI.items():
            _ui_cache[lang].setdefault(k, v)
        logger.info("UI translated to {} ({} keys)", lang, len(_ui_cache[lang]))
    except Exception as exc:
        logger.warning("UI translation failed for {} ({}), falling back to English", lang, exc)
        _ui_cache[lang] = _UI


def _t(lang: str, key: str, **fmt: object) -> str:
    """Return a translated UI string, falling back to English."""  # noqa: E501 — cache warmed by _lang()
    s = _ui_cache.get(lang, _UI).get(key) or _UI[key]
    return s.format(**fmt) if fmt else s


async def _lang(telegram_id: int) -> str:
    """Return user language and ensure UI strings are translated for it.

    Always calls _ensure_ui — it's a no-op once the cache is warm, but this
    guarantees the cache is populated even after a mid-session language change.
    """
    if telegram_id not in _lang_cache:
        _lang_cache[telegram_id] = await store.get_user_language(telegram_id)
    lang = _lang_cache[telegram_id]
    await _ensure_ui(lang)
    return lang


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


async def _reply(update: Update, text: str, **kwargs: object) -> None:
    """Send a new reply message regardless of whether the trigger was a message or callback."""
    if update.message:
        await update.message.reply_text(text, **kwargs)  # type: ignore[arg-type]
    elif update.callback_query:
        cq_msg = update.callback_query.message
        if isinstance(cq_msg, Message):
            await cq_msg.reply_text(text, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


def _start_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(_t(lang, "btn_add_topic"), callback_data="start:add_topic"),
                InlineKeyboardButton(_t(lang, "btn_my_topics"), callback_data="start:topics"),
            ],
            [
                InlineKeyboardButton(_t(lang, "btn_check_now"), callback_data="start:check"),
            ],
        ]
    )


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    await store.get_or_create_user(tg.id, tg.first_name, tg.username)
    lang = await _lang(tg.id)
    await update.message.reply_text(
        _t(lang, "welcome"), parse_mode="Markdown", reply_markup=_start_keyboard(lang)
    )


async def _start_add_topic_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the add-topic conversation via the /start button."""
    query = update.callback_query
    if query is None or query.from_user is None:
        return ConversationHandler.END
    await _safe_answer(query)
    _reset_topic_flow_state(context)
    if context.user_data is not None:
        context.user_data["sched_mode"] = "create"
    await query.message.reply_text(  # type: ignore[union-attr]
        "What do you want to track?\n\nDescribe it in a sentence — I'll suggest a name."
    )
    return _AT_DESC


async def on_start_nav(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles My topics and Check now buttons from the /start message."""
    query = update.callback_query
    if query is None or query.from_user is None or query.data is None:
        return
    await _safe_answer(query)
    action = query.data.split(":", 1)[1]
    tg_id = query.from_user.id
    lang = await _lang(tg_id)

    if action == "topics":
        topics = await store.get_topics(tg_id)
        if not topics:
            await query.message.reply_text(  # type: ignore[union-attr]
                _t(lang, "no_topics_short"), reply_markup=_start_keyboard(lang)
            )
        else:
            await query.message.reply_text(_t(lang, "topics_header", n=len(topics)))  # type: ignore[union-attr]
            for t in topics:
                text, keyboard = _topic_card(t, lang)
                await query.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")  # type: ignore[union-attr]

    elif action == "check":
        await _topic_picker(update, tg_id, "check", "Which topic?")


# ---------------------------------------------------------------------------
# /add_topic conversation — entry
# ---------------------------------------------------------------------------


async def cmd_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not await _allowed(update):
        return ConversationHandler.END
    _reset_topic_flow_state(context)
    if context.user_data is not None:
        context.user_data["sched_mode"] = "create"
    tg = update.effective_user
    lang = await _lang(tg.id) if tg else "English"
    await update.message.reply_text(_t(lang, "add_topic_prompt"))
    return _AT_DESC


async def _got_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text or context.user_data is None:
        return _AT_DESC
    tg = update.effective_user
    lang = await _lang(tg.id) if tg else "English"
    desc = update.message.text.strip()
    if not desc:
        await update.message.reply_text(_t(lang, "desc_empty"))
        return _AT_DESC
    await update.message.chat.send_action(ChatAction.TYPING)
    results = await asyncio.gather(
        _generate_name(desc),
        research.expand_query(desc, ""),
        research.generate_source_guidance(desc, ""),
        research.identify_sides(desc, ""),
        return_exceptions=True,
    )
    name = (
        results[0]
        if not isinstance(results[0], BaseException)
        else " ".join(desc.split()[:4]).rstrip(".,!?")
    )
    expanded = results[1] if not isinstance(results[1], BaseException) else desc
    guidance = results[2] if not isinstance(results[2], BaseException) else None
    sides = results[3] if not isinstance(results[3], BaseException) else []

    context.user_data["new_topic_name"] = name
    context.user_data["new_topic_desc"] = expanded
    context.user_data["new_topic_source_guidance"] = guidance
    context.user_data["new_topic_sides_json"] = json.dumps([s.model_dump() for s in sides])

    await update.message.reply_text(
        _t(lang, "topic_confirm", name=name, desc=expanded),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(_t(lang, "btn_continue"), callback_data="name:ok"),
                    InlineKeyboardButton(_t(lang, "btn_rename"), callback_data="name:rename"),
                ]
            ]
        ),
        parse_mode="Markdown",
    )
    return _AT_NAME_CONFIRM


async def _name_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await _safe_answer(query)
        name = context.user_data.get("new_topic_name", "") if context.user_data else ""
        await _safe_edit_text(query, f"✓ *{name}*", parse_mode="Markdown")
    return await _ask_sched_type(update, context)


async def _name_rename_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query and query.from_user:
        await _safe_answer(query)
        lang = await _lang(query.from_user.id)
        await _safe_edit_text(query, _t(lang, "rename_prompt"))
    return _AT_NAME_CONFIRM


async def _got_custom_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text or context.user_data is None:
        return _AT_NAME_CONFIRM
    tg = update.effective_user
    lang = await _lang(tg.id) if tg else "English"
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text(_t(lang, "rename_empty"))
        return _AT_NAME_CONFIRM
    context.user_data["new_topic_name"] = name
    return await _ask_sched_type(update, context)


# ---------------------------------------------------------------------------
# /schedule conversation — entry
# ---------------------------------------------------------------------------


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not await _allowed(update):
        return ConversationHandler.END
    tg = update.effective_user
    if tg is None:
        return ConversationHandler.END

    _reset_topic_flow_state(context)
    if context.user_data is not None:
        context.user_data["sched_mode"] = "update"

    lang = await _lang(tg.id)
    name = " ".join(context.args or []).strip()  # type: ignore[union-attr]
    if not name:
        topics = await store.get_topics(tg.id)
        if not topics:
            await update.message.reply_text(_t(lang, "no_topics"), parse_mode="Markdown")
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(t.name, callback_data=f"sc_pick:{t.id}")] for t in topics]
        await update.message.reply_text(
            _t(lang, "reschedule_picker"), reply_markup=InlineKeyboardMarkup(buttons)
        )
        return _SC_PICK

    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(
            _t(lang, "no_topic_named", name=name), parse_mode="Markdown"
        )
        return ConversationHandler.END

    if context.user_data is not None:
        context.user_data["sched_topic_id"] = str(topic.id)
    return await _ask_sched_type(update, context)


async def _sc_got_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Topic selected from the /schedule picker."""
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None or context.user_data is None:
        return _SC_PICK
    await _safe_answer(query)
    topic_id_str = query.data.split(":", 1)[1]
    context.user_data["sched_topic_id"] = topic_id_str
    lang = await _lang(query.from_user.id)
    await _safe_edit_text(query, _t(lang, "changing_schedule"))
    return await _ask_sched_type(update, context)


async def _tp_schedule_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the 📅 button on a topic card — jumps straight into the
    schedule-editing flow for that topic instead of telling the user to type
    /schedule <name> themselves."""
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None:
        return ConversationHandler.END
    await _safe_answer(query)
    topic_id_str = query.data.split(":", 2)[2]
    _reset_topic_flow_state(context)
    if context.user_data is not None:
        context.user_data["sched_mode"] = "update"
        context.user_data["sched_topic_id"] = topic_id_str
    if isinstance(query.message, Message):
        lang = await _lang(query.from_user.id)
        await query.message.reply_text(_t(lang, "changing_schedule"))
    return await _ask_sched_type(update, context)


# ---------------------------------------------------------------------------
# Shared scheduling flow: type → days → time
# ---------------------------------------------------------------------------


def _back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("←", callback_data="nav:back")


async def _ask_sched_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    lang = await _lang(tg.id) if tg else "English"
    n_slots = len(context.user_data.get("new_topic_slots", [])) if context.user_data else 0
    prompt = _t(lang, "sched_another_prompt" if n_slots else "sched_first_prompt")
    rows = [
        [
            InlineKeyboardButton(_t(lang, "sched_daily"), callback_data="sched:daily"),
            InlineKeyboardButton(_t(lang, "sched_twice_daily"), callback_data="sched:twice_daily"),
        ],
        [InlineKeyboardButton(_t(lang, "sched_custom_days"), callback_data="sched:custom_days")],
        [InlineKeyboardButton(_t(lang, "sched_biweekly"), callback_data="sched:biweekly")],
    ]
    # Only offered for the first checkup slot — once a topic already has a scheduled
    # slot, "no schedule" doesn't make sense as an *additional* one.
    if not n_slots:
        rows.append(
            [InlineKeyboardButton(_t(lang, "sched_manual_btn"), callback_data="sched:manual")]
        )
    rows.append([_back_button()])
    await _reply(update, prompt, reply_markup=InlineKeyboardMarkup(rows))
    return _ASK_SCHED_TYPE


async def _back_from_sched_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """← from the "how often" screen: undo into whatever came before it —
    the add-another prompt if a slot already exists, the name confirmation if
    this is the very first slot of a new topic, or just cancel a /schedule edit."""
    query = update.callback_query
    if query is None or query.from_user is None or context.user_data is None:
        return _ASK_SCHED_TYPE
    await _safe_answer(query)
    ud = context.user_data
    lang = await _lang(query.from_user.id)

    slots: list[Slot] = ud.get("new_topic_slots", [])
    if slots:
        await _safe_edit_text(query, _t(lang, "nav_back"))
        await _reply(
            update, _t(lang, "add_another_prompt"), reply_markup=_add_another_keyboard(lang)
        )
        return _ASK_ADD_ANOTHER

    if ud.get("sched_mode") == "update":
        await _safe_edit_text(query, _t(lang, "cancelled"))
        _reset_topic_flow_state(context)
        return ConversationHandler.END

    name = ud.get("new_topic_name", "")
    desc = ud.get("new_topic_desc", "")
    await _safe_edit_text(
        query,
        _t(lang, "topic_confirm", name=name, desc=desc),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(_t(lang, "btn_continue"), callback_data="name:ok"),
                    InlineKeyboardButton(_t(lang, "btn_rename"), callback_data="name:rename"),
                ]
            ]
        ),
        parse_mode="Markdown",
    )
    return _AT_NAME_CONFIRM


async def _got_sched_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None or context.user_data is None:
        return _ASK_SCHED_TYPE
    await _safe_answer(query)
    freq = query.data.split(":", 1)[1]
    context.user_data["new_topic_freq"] = freq
    lang = await _lang(query.from_user.id)
    ui_key = _SCHED_TYPE_UI_KEYS.get(freq)
    await _safe_edit_text(query, f"✓ {_t(lang, ui_key) if ui_key else freq}")

    if freq == "manual":
        # No slot to add — jump straight to wherever the "Done" path of the
        # add-another step would go, same as finishing a topic with slots.
        if context.user_data.get("sched_mode") == "update":
            return await _update_schedule(update, context)
        return await _use_profile_tz_and_continue(update, context)

    if freq in _NEEDS_DAYS:
        mode = "multi" if freq == "custom_days" else "single"
        context.user_data["sched_days_mode"] = mode
        context.user_data["selected_days"] = set()
        return await _ask_sched_days(update, context)
    if freq == "twice_daily":
        context.user_data["_twice_daily_pending"] = True
    return await _ask_time(update, context)


def _day_toggle_keyboard(selected: set[int], lang: str = "English") -> InlineKeyboardMarkup:
    def lbl(i: int) -> str:
        short = _t(lang, f"dows_{i}")
        return f"✓ {short}" if i in selected else short

    row1 = [InlineKeyboardButton(lbl(i), callback_data=f"day_toggle:{i}") for i in range(4)]
    row2 = [InlineKeyboardButton(lbl(i), callback_data=f"day_toggle:{i}") for i in range(4, 7)]
    done_btn = InlineKeyboardButton(_t(lang, "btn_done_check"), callback_data="day_done")
    done_row = [done_btn, _back_button()]
    return InlineKeyboardMarkup([row1, row2, done_row])


def _day_single_keyboard(lang: str = "English") -> InlineKeyboardMarkup:
    def _btn(i: int) -> InlineKeyboardButton:
        return InlineKeyboardButton(_t(lang, f"dows_{i}"), callback_data=f"dow_single:{i}")

    row2 = [_btn(i) for i in range(4, 7)] + [_back_button()]
    return InlineKeyboardMarkup([[_btn(i) for i in range(4)], row2])


async def _ask_sched_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    lang = await _lang(tg.id) if tg else "English"
    ud = context.user_data
    mode = ud.get("sched_days_mode", "single") if ud else "single"
    if mode == "multi":
        selected: set[int] = ud.get("selected_days", set()) if ud else set()
        await _reply(
            update,
            _t(lang, "sched_days_multi_prompt"),
            reply_markup=_day_toggle_keyboard(selected, lang),
            parse_mode="Markdown",
        )
    else:
        await _reply(
            update,
            _t(lang, "sched_days_single_prompt"),
            reply_markup=_day_single_keyboard(lang),
        )
    return _ASK_SCHED_DAYS


async def _back_from_sched_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """← from the day-picker: discard the in-progress day selection and return
    to the "how often" screen."""
    query = update.callback_query
    if query is None or query.from_user is None or context.user_data is None:
        return _ASK_SCHED_DAYS
    await _safe_answer(query)
    context.user_data.pop("selected_days", None)
    context.user_data.pop("sched_days_mode", None)
    context.user_data.pop("new_topic_freq", None)
    lang = await _lang(query.from_user.id)
    await _safe_edit_text(query, _t(lang, "nav_back"))
    return await _ask_sched_type(update, context)


async def _toggle_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tap a day to toggle it in the multi-select picker."""
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None or context.user_data is None:
        return _ASK_SCHED_DAYS
    await _safe_answer(query)
    day_idx = int(query.data.split(":", 1)[1])
    selected: set[int] = context.user_data.get("selected_days", set())
    if day_idx in selected:
        selected.discard(day_idx)
    else:
        selected.add(day_idx)
    context.user_data["selected_days"] = selected
    lang = await _lang(query.from_user.id)
    await query.edit_message_reply_markup(reply_markup=_day_toggle_keyboard(selected, lang))
    return _ASK_SCHED_DAYS


async def _done_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Done button in multi-select day picker."""
    query = update.callback_query
    if query is None or query.from_user is None or context.user_data is None:
        return _ASK_SCHED_DAYS
    lang = await _lang(query.from_user.id)
    selected: set[int] = context.user_data.get("selected_days", set())
    if not selected:
        await _safe_answer(query, _t(lang, "pick_one_day"), show_alert=True)
        return _ASK_SCHED_DAYS
    await _safe_answer(query)
    days_str = ",".join(str(d) for d in sorted(selected))
    context.user_data["new_topic_schedule_days"] = days_str
    day_names = " / ".join(_t(lang, f"dows_{d}") for d in sorted(selected))
    await _safe_edit_text(query, f"✓ {day_names}")
    return await _ask_time(update, context)


async def _got_single_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Day selected in single-select (weekly/biweekly)."""
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None or context.user_data is None:
        return _ASK_SCHED_DAYS
    await _safe_answer(query)
    dow = int(query.data.split(":", 1)[1])
    context.user_data["new_topic_dow"] = dow
    lang = await _lang(query.from_user.id)
    await _safe_edit_text(query, f"✓ {_t(lang, f'dow_{dow}')}")
    return await _ask_time(update, context)


def _parse_time(text: str) -> tuple[int, int]:
    """Parse '9:30', '21', '10-00', '9am', '9:30pm' → (hour, minute). Defaults to 9:00."""
    t = text.strip().lower().replace(".", ":").replace("h", ":").replace("-", ":")
    pm = t.endswith("pm")
    am = t.endswith("am")
    t = t.removesuffix("pm").removesuffix("am").strip()
    try:
        if ":" in t:
            h_str, _, m_str = t.partition(":")
            h, m = int(h_str), int(m_str[:2] or "0")
        else:
            h, m = int(t), 0
        if pm and h != 12:
            h += 12
        elif am and h == 12:
            h = 0
        return h % 24, min(m, 59)
    except ValueError:
        return 9, 0


async def _ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    lang = await _lang(tg.id) if tg else "English"
    ud = context.user_data
    if ud and ud.get("_twice_daily_second"):
        prompt_key = "time_second_prompt"
    elif ud and ud.get("_twice_daily_pending"):
        prompt_key = "time_first_prompt"
    else:
        prompt_key = "time_prompt"
    await _reply(
        update,
        _t(lang, "time_instructions", prompt=_t(lang, prompt_key)),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[_back_button()]]),
    )
    return _ASK_TIME


async def _back_from_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """← from the time prompt: return to wherever the time question for this
    slot was reached from — the day-picker, the "how often" screen, or (for
    twice daily's second time) re-ask the first time."""
    query = update.callback_query
    if query is None or query.from_user is None or context.user_data is None:
        return _ASK_TIME
    await _safe_answer(query)
    ud = context.user_data
    lang = await _lang(query.from_user.id)
    await _safe_edit_text(query, _t(lang, "nav_back"))

    if ud.pop("_twice_daily_second", False):
        ud["new_topic_freq"] = "twice_daily"
        ud["_twice_daily_pending"] = True
        return await _ask_time(update, context)

    ud.pop("_twice_daily_pending", None)
    freq = ud.get("new_topic_freq", "daily")
    if freq in _NEEDS_DAYS:
        return await _ask_sched_days(update, context)
    return await _ask_sched_type(update, context)


def _finish_slot(ud: dict[str, object], hour: int, minute: int) -> Slot:
    """Build a Slot from the current "freq + days" selection plus the time just typed,
    then clear those temporary keys so the next loop iteration starts fresh."""
    freq = str(ud.pop("new_topic_freq", "daily"))
    dow = ud.pop("new_topic_dow", None)
    schedule_days = str(ud.pop("new_topic_schedule_days", ""))
    ud.pop("sched_days_mode", None)
    ud.pop("selected_days", None)

    if freq == "custom_days":
        days = [int(d) for d in schedule_days.split(",") if d.strip()]
        every_n_weeks = 0
    elif freq == "biweekly":
        days = [int(dow)] if isinstance(dow, int) else [0]
        every_n_weeks = 2
    else:  # "daily" or "twice_daily" — twice_daily's second slot is also "daily"
        days, every_n_weeks = [], 0

    return Slot(days=days, hour=hour, minute=minute, every_n_weeks=every_n_weeks)


def _add_another_keyboard(lang: str = "English") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(_t(lang, "btn_add_another"), callback_data="slot:add"),
                InlineKeyboardButton(_t(lang, "btn_done_check"), callback_data="slot:done"),
            ],
            [InlineKeyboardButton("←", callback_data="slot:back")],
        ]
    )


async def _got_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text or context.user_data is None:
        return _ASK_TIME
    tg = update.effective_user
    lang = await _lang(tg.id) if tg else "English"
    h, m = _parse_time(update.message.text)
    slot = _finish_slot(context.user_data, h, m)
    context.user_data.setdefault("new_topic_slots", []).append(slot)
    await update.message.reply_text(f"✓ {h:02d}:{m:02d}")

    if context.user_data.pop("_twice_daily_pending", False):
        context.user_data["new_topic_freq"] = "daily"
        context.user_data["_twice_daily_second"] = True
        return await _ask_time(update, context)
    context.user_data.pop("_twice_daily_second", None)

    await _reply(update, _t(lang, "add_another_prompt"), reply_markup=_add_another_keyboard(lang))
    return _ASK_ADD_ANOTHER


async def _got_add_another(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None or query.from_user is None or context.user_data is None:
        return _ASK_ADD_ANOTHER
    await _safe_answer(query)
    action = query.data.split(":", 1)[1]
    lang = await _lang(query.from_user.id)

    if action == "back":
        slots: list[Slot] = context.user_data.get("new_topic_slots", [])
        if slots:
            slots.pop()
        await _safe_edit_text(query, _t(lang, "nav_back"))
        return await _ask_sched_type(update, context)

    if action == "add":
        await _safe_edit_text(query, _t(lang, "adding_another"))
        return await _ask_sched_type(update, context)

    await _safe_edit_text(query, f"✓ {_t(lang, 'done_short')}")
    if context.user_data.get("sched_mode") == "update":
        return await _update_schedule(update, context)
    return await _use_profile_tz_and_continue(update, context)


async def _update_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finalize a schedule edit (from /schedule)."""
    ud = context.user_data
    if ud is None:
        return ConversationHandler.END
    from uuid import UUID

    topic_id = UUID(ud.pop("sched_topic_id", ""))
    tg = update.effective_user
    if tg is None:
        return ConversationHandler.END
    lang = await _lang(tg.id)
    topic = await store.get_topic(tg.id, topic_id)
    if topic is None:
        if update.message:
            await update.message.reply_text(_t(lang, "topic_nf"))
        return ConversationHandler.END

    slots: list[Slot] = ud.pop("new_topic_slots", [])
    ud.pop("sched_mode", None)

    await store.replace_slots(topic_id, slots)
    label = _sched_label_for_slots(slots, topic.timezone, lang)
    msg = _t(lang, "schedule_updated", name=_short(topic.name), label=label)
    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown")  # type: ignore[union-attr]
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Timezone — a profile-level (per-user) setting, not per-topic. New topics
# pick it up automatically from the profile; /timezone changes it for every
# topic at once. See cmd_timezone / on_profile_tz_set below.
# ---------------------------------------------------------------------------


def _timezone_keyboard(callback_prefix: str) -> InlineKeyboardMarkup:
    """2 buttons per row — 3 per row with these label lengths was getting
    visually truncated by Telegram on some clients (e.g. "UTC+8  Singapore"
    rendering as "utc...gapore")."""

    def _btn(lbl: str, zone: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(lbl, callback_data=f"{callback_prefix}{zone}")

    return InlineKeyboardMarkup(
        [
            [_btn(lbl, zone) for lbl, zone in _TIMEZONES[i : i + 2]]
            for i in range(0, len(_TIMEZONES), 2)
        ]
    )


async def _use_profile_tz_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Apply the user's profile timezone to the new topic with no question asked,
    then continue to the sources step — replaces the old per-topic ask."""
    tg = update.effective_user
    if tg is None or context.user_data is None:
        return await _ask_sources(update, context)
    context.user_data["new_topic_tz"] = await store.get_user_timezone(tg.id)
    return await _ask_sources(update, context)


# ---------------------------------------------------------------------------
# Sources step (add_topic only)
# ---------------------------------------------------------------------------


async def _ask_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg = update.effective_user
    lang = await _lang(tg.id) if tg else "English"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(_t(lang, "btn_skip"), callback_data="sources:skip")]]
    )
    await _reply(update, _t(lang, "sources_prompt"), reply_markup=keyboard, parse_mode="Markdown")
    return _AT_SOURCES


async def _got_sources_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return _AT_SOURCES
    raw = update.message.text.strip()
    sources = [s.strip() for s in raw.split(",") if s.strip()]
    return await _create_topic(update, context, sources)


async def _got_sources_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query and query.from_user:
        await _safe_answer(query)
        lang = await _lang(query.from_user.id)
        await _safe_edit_text(query, _t(lang, "sources_skipped"))
    return await _create_topic(update, context, [])


async def _create_topic(
    update: Update, context: ContextTypes.DEFAULT_TYPE, sources: list[str]
) -> int:
    tg = update.effective_user
    if tg is None or context.user_data is None:
        return ConversationHandler.END
    lang = await _lang(tg.id)
    ud = context.user_data
    name = ud.pop("new_topic_name", "")
    desc = ud.pop("new_topic_desc", None)
    source_guidance = ud.pop("new_topic_source_guidance", None)
    sides_json = ud.pop("new_topic_sides_json", None)
    slots: list[Slot] = ud.pop("new_topic_slots", [])
    tz = ud.pop("new_topic_tz", "UTC")
    ud.pop("sched_mode", None)

    topic = await store.create_topic(
        tg.id,
        name=name,
        description=desc,
        sources=sources,
        slots=slots,
        timezone=tz,
        source_guidance=source_guidance,
        sides_json=sides_json,
    )
    label = _sched_label_for_slots(slots, tz, lang)
    tz_label = next((lbl for lbl, z in _TIMEZONES if z == tz), tz)
    # Distinct from the many small "✓ ..." step confirmations earlier in this same
    # flow (✓ name, ✓ schedule, ✓ timezone, ...) — a user reported missing that
    # their topic was actually saved because the final message looked the same as
    # those intermediate ones. A heading + box makes "topic now exists" unambiguous.
    sources_label = ", ".join(sources) if sources else _t(lang, "sources_general")
    msg = _t(
        lang, "topic_created", name=topic.name, label=label, tz=tz_label, sources=sources_label
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(_t(lang, "btn_check_now"), callback_data=f"ta:check:{topic.id}")]]
    )
    if update.message:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(  # type: ignore[union-attr]
            msg, reply_markup=keyboard, parse_mode="Markdown"
        )
    return ConversationHandler.END


_TOPIC_FLOW_KEYS = (
    "new_topic_name",
    "new_topic_desc",
    "new_topic_source_guidance",
    "new_topic_sides_json",
    "new_topic_freq",
    "new_topic_dow",
    "new_topic_tz",
    "new_topic_schedule_days",
    "new_topic_slots",
    "_twice_daily_pending",
    "_twice_daily_second",
    "sched_mode",
    "sched_topic_id",
    "sched_days_mode",
    "selected_days",
)


def _reset_topic_flow_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear any half-finished add_topic/schedule state before (re)starting the flow."""
    if context.user_data is not None:
        for key in _TOPIC_FLOW_KEYS:
            context.user_data.pop(key, None)


async def _conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fires when a user abandons add_topic/schedule mid-flow without finishing or /cancel."""
    _reset_topic_flow_state(context)
    chat = update.effective_chat
    tg = update.effective_user
    if chat is not None:
        lang = await _lang(tg.id) if tg else "English"
        await context.bot.send_message(chat.id, _t(lang, "timeout_msg"))
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _reset_topic_flow_state(context)
    if update.message:
        tg_c = update.effective_user
        ul_c = await _lang(tg_c.id) if tg_c else "English"
        await update.message.reply_text(_t(ul_c, "cancelled"))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Schedule display helpers
# ---------------------------------------------------------------------------


def _slot_label(slot: Slot, lang: str = "English") -> str:
    """Human-readable label for one slot, e.g. 'Mon/Wed/Fri · 09:00' or 'Every day · 10:00'."""
    time_str = f"{slot.hour:02d}:{slot.minute:02d}"
    days_set = set(slot.days)
    if not days_set:
        day_part = _t(lang, "sched_daily")
    elif days_set == {0, 1, 2, 3, 4}:
        day_part = _t(lang, "sched_weekdays")
    elif days_set == {0, 2, 4}:
        day_part = _t(lang, "sched_mwf")
    elif days_set == {1, 3}:
        day_part = _t(lang, "sched_tuth")
    elif len(slot.days) == 1:
        day_part = _t(lang, f"dow_{slot.days[0]}")
    else:
        day_part = " / ".join(_t(lang, f"dows_{d}") for d in sorted(days_set))

    if slot.every_n_weeks >= 1:
        cadence = (
            _t(lang, "sched_weekly")
            if slot.every_n_weeks == 1
            else f"Every {slot.every_n_weeks} weeks"
        )
        return f"{cadence} ({day_part}) · {time_str}"
    return f"{day_part} · {time_str}"


def _sched_label_for_slots(slots: list[Slot], timezone: str, lang: str = "English") -> str:
    """Human-readable schedule summary across all of a topic's slots."""
    if not slots:
        return _t(lang, "never_sent")
    ordered = sorted(slots, key=lambda s: (s.hour, s.minute))
    body = " & ".join(_slot_label(s, lang) for s in ordered)
    tz_short = next((lbl.split()[0] for lbl, z in _TIMEZONES if z == timezone), "")
    return f"{body} ({tz_short})" if tz_short else body


def _sched_label(topic: Topic, lang: str = "English") -> str:
    """Short human-readable schedule for /topics list and topic cards."""
    return _sched_label_for_slots(topic.slots, topic.timezone, lang)


# ---------------------------------------------------------------------------
# Topic picker — shared across /check, /pause, /resume, /synthesis, /reset
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
    buttons = [
        [InlineKeyboardButton(t.shown_name, callback_data=f"ta:{action}:{t.id}")] for t in topics
    ]
    if update.message:
        await update.message.reply_text(prompt, reply_markup=InlineKeyboardMarkup(buttons))


async def on_topic_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback handler for topic picker buttons (ta:{action}:{topic_id})."""
    query = update.callback_query
    if query is None or query.from_user is None or query.data is None:
        return
    await _safe_answer(query)

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    _, action, topic_id_str = parts

    from uuid import UUID

    ul = await _lang(query.from_user.id)
    topic = await store.get_topic(query.from_user.id, UUID(topic_id_str))
    if topic is None:
        await _safe_edit_text(query, _t(ul, "topic_nf"))
        return

    if action == "check":
        await _safe_edit_text(
            query, _t(ul, "fetch_status", name=topic.shown_name), parse_mode="Markdown"
        )
        try:
            await jobs._run_digest(topic, context.bot)
        except Exception:  # noqa: BLE001
            logger.exception("picker /check failed for topic {}", topic.id)
            await query.message.reply_text(_t(ul, "err_moment"))  # type: ignore[union-attr]
        updated = await store.get_topic(query.from_user.id, topic.id)
        if updated:
            text, keyboard = _topic_card(updated, ul)
            await _safe_edit_text(query, text, reply_markup=keyboard, parse_mode="Markdown")

    elif action == "pause":
        await store.update_topic(topic.id, paused=True)
        await _safe_edit_text(query, f"⏸ *{topic.shown_name}* paused.", parse_mode="Markdown")

    elif action == "resume":
        await store.update_topic(topic.id, paused=False)
        await _safe_edit_text(query, f"▶ *{topic.shown_name}* resumed.", parse_mode="Markdown")

    elif action == "reset":
        n = await store.clear_seen(topic.id)
        await _safe_edit_text(
            query,
            _t(ul, "cleared_n", n=n, name=_short(topic.shown_name)),
            parse_mode="Markdown",
        )

    elif action == "delete":
        await _safe_edit_text(
            query,
            f"Delete *{_short(topic.name)}*?\n\nThis removes the topic and all its history.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Yes, delete", callback_data=f"td:confirm:{topic.id}"),
                        InlineKeyboardButton("Cancel", callback_data=f"td:cancel:{topic.id}"),
                    ]
                ]
            ),
            parse_mode="Markdown",
        )


async def on_topic_delete_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for the delete-confirmation buttons (td:{confirm|cancel}:{topic_id})."""
    query = update.callback_query
    if query is None or query.from_user is None or query.data is None:
        return
    await _safe_answer(query)

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    _prefix, action, topic_id_str = parts

    from uuid import UUID

    ul_del = await _lang(query.from_user.id)
    if action == "cancel":
        await _safe_edit_text(query, _t(ul_del, "cancelled"))
        return

    topic = await store.get_topic(query.from_user.id, UUID(topic_id_str))
    if topic is None:
        await _safe_edit_text(query, _t(ul_del, "topic_nf"))
        return

    await store.delete_topic(topic.id)
    await _safe_edit_text(query, f"✓ *{_short(topic.name)}* deleted.", parse_mode="Markdown")


async def on_profile_tz_set(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for /timezone buttons (tzprofile:{zone}) — sets the timezone for
    the whole profile (every topic), not just one."""
    query = update.callback_query
    if query is None or query.from_user is None or query.data is None:
        return
    await _safe_answer(query)
    zone = query.data.split(":", 1)[1]

    await store.set_user_timezone(query.from_user.id, zone)
    lang = await _lang(query.from_user.id)
    label = next((lbl for lbl, z in _TIMEZONES if z == zone), zone)
    await _safe_edit_text(query, _t(lang, "tz_set_confirm", label=label))


async def _show_sources_view(query: object, topic: Topic) -> None:
    """Edit the current message to show the sources management view for *topic*."""
    from telegram import CallbackQuery as CQ

    q: CQ = query  # type: ignore[assignment]
    tid = str(topic.id)
    tracked = topic.sources
    ignored = topic.excluded_sources

    lines: list[str] = []
    lines.append("*Always check:* " + (", ".join(tracked) if tracked else "_none_"))
    lines.append("*Always ignore:* " + (", ".join(ignored) if ignored else "_none_"))
    lines.append("")
    lines.append(
        "_Tracked sources are queried by name every run. "
        "Ignored sources are never used, even if found by the general search._"
    )
    msg = "\n".join(lines)

    rows: list[list[InlineKeyboardButton]] = []
    # Use index instead of source name to stay within Telegram's 64-byte callback_data limit.
    for i, s in enumerate(tracked):
        rows.append([InlineKeyboardButton(f"✖ {s}", callback_data=f"tp:rm_src:{tid}:{i}")])
    for i, s in enumerate(ignored):
        rows.append([InlineKeyboardButton(f"🚫 {s}", callback_data=f"tp:rm_blk:{tid}:{i}")])
    rows.append(
        [
            InlineKeyboardButton("➕ Always check", callback_data=f"tp:add_src:{tid}"),
            InlineKeyboardButton("🚫 Always ignore", callback_data=f"tp:add_blk:{tid}"),
        ]
    )
    rows.append([InlineKeyboardButton("← Back", callback_data=f"tp:back:{tid}")])
    await _safe_edit_text(q, msg, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Topic panel callbacks (tp:{action}:{topic_id})
# ---------------------------------------------------------------------------


async def on_topic_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles all action buttons on a topic card from /topics."""
    query = update.callback_query
    if query is None or query.from_user is None or query.data is None:
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await _safe_answer(query)
        return
    _, action, topic_id_str = parts

    await _safe_answer(query, "Running digest…" if action == "check" else "")

    from uuid import UUID

    lang = await _lang(query.from_user.id)
    topic = await store.get_topic(query.from_user.id, UUID(topic_id_str))
    if topic is None:
        await _safe_edit_text(query, _t(lang, "topic_nf"))
        return

    if action == "check":
        # Show running state inside the card (under the title, buttons stay)
        _, card_keyboard = _topic_card(topic, lang)
        query_text = topic.shown_description or topic.shown_name
        never = _t(lang, "never_sent")
        last = topic.last_sent_at.strftime("%d %b") if topic.last_sent_at else never
        running_text = (
            f"📌 *{topic.shown_name}*\n{_t(lang, 'fetch_card')}\n_{query_text}  ·  {last}_"
        )
        await _safe_edit_text(
            query, running_text, reply_markup=card_keyboard, parse_mode="Markdown"
        )
        # Also send a separate status message below the card
        status = await context.bot.send_message(
            chat_id=topic.telegram_id,
            text=_t(lang, "fetch_status", name=topic.shown_name),
            parse_mode="Markdown",
        )
        failed = False
        try:
            await jobs._run_digest(topic, context.bot)
        except Exception:  # noqa: BLE001
            logger.exception("panel /check failed for topic {}", topic.id)
            failed = True
        with contextlib.suppress(Exception):
            await status.delete()
        if failed:
            await context.bot.send_message(chat_id=topic.telegram_id, text=_t(lang, "err_generic"))
        updated = await store.get_topic(query.from_user.id, topic.id)
        if updated:
            text, keyboard = _topic_card(updated, lang)
            await _safe_edit_text(query, text, reply_markup=keyboard, parse_mode="Markdown")

    elif action in ("pause", "resume"):
        paused = action == "pause"
        await store.update_topic(topic.id, paused=paused)
        updated = await store.get_topic(query.from_user.id, topic.id)
        if updated:
            text, keyboard = _topic_card(updated, lang)
            await _safe_edit_text(query, text, reply_markup=keyboard, parse_mode="Markdown")

    elif action == "reset":
        n = await store.clear_seen(topic.id)
        text, keyboard = _topic_card(topic, lang)
        await _safe_edit_text(
            query,
            text + f"\n\n{_t(lang, 'cleared_n', n=n, name=_short(topic.shown_name))}",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    elif action == "rename":
        if context.user_data is not None:
            context.user_data["awaiting_rename_id"] = str(topic.id)
            context.user_data["awaiting_rename_name"] = topic.name
        await query.message.reply_text(  # type: ignore[union-attr]
            f"Type the new name for *{_short(topic.name)}*:\n_(/cancel to abort)_",
            parse_mode="Markdown",
        )

    elif action == "describe":
        if context.user_data is not None:
            context.user_data["awaiting_describe_id"] = str(topic.id)
            context.user_data["awaiting_describe_name"] = topic.name
        current = topic.description or topic.name
        await query.message.reply_text(  # type: ignore[union-attr]
            f"*Research query for {_short(topic.name)}:*\n_{current}_\n\nType a replacement, or /cancel.",  # noqa: E501
            parse_mode="Markdown",
        )

    elif action == "sources":
        await _show_sources_view(query, topic)

    elif action == "rm_src":
        idx_str = (query.data or "").split(":", 3)[3]
        try:
            idx = int(idx_str)
            updated = [s for i, s in enumerate(topic.sources) if i != idx]
            await store.update_topic(topic.id, sources=updated)
        except (ValueError, IndexError):
            pass
        refreshed = await store.get_topic(query.from_user.id, topic.id)
        if refreshed:
            await _show_sources_view(query, refreshed)

    elif action == "add_src":
        if context.user_data is not None:
            context.user_data["awaiting_source_id"] = str(topic.id)
            context.user_data["awaiting_source_name"] = topic.name
        await query.message.reply_text(  # type: ignore[union-attr]
            f"Which domain should *{_short(topic.name)}* always check? (e.g. `reuters.com`)\n_/cancel to abort._",  # noqa: E501
            parse_mode="Markdown",
        )

    elif action == "rm_blk":
        idx_str = (query.data or "").split(":", 3)[3]
        try:
            idx = int(idx_str)
            updated = [s for i, s in enumerate(topic.excluded_sources) if i != idx]
            await store.update_topic(topic.id, excluded_sources=updated)
        except (ValueError, IndexError):
            pass
        refreshed = await store.get_topic(query.from_user.id, topic.id)
        if refreshed:
            await _show_sources_view(query, refreshed)

    elif action == "add_blk":
        if context.user_data is not None:
            context.user_data["awaiting_block_id"] = str(topic.id)
            context.user_data["awaiting_block_name"] = topic.name
        await query.message.reply_text(  # type: ignore[union-attr]
            f"Which domain should *{_short(topic.name)}* always ignore? (e.g. `foxnews.com`)\n_/cancel to abort._",  # noqa: E501
            parse_mode="Markdown",
        )

    elif action == "edit":
        text, keyboard = _topic_card_expanded(topic, lang)
        await _safe_edit_text(query, text, reply_markup=keyboard, parse_mode="Markdown")

    elif action == "close":
        text, keyboard = _topic_card(topic, lang)
        await _safe_edit_text(query, text, reply_markup=keyboard, parse_mode="Markdown")

    elif action == "back":
        text, keyboard = _topic_card_expanded(topic, lang)
        await _safe_edit_text(query, text, reply_markup=keyboard, parse_mode="Markdown")

    elif action == "delete":
        await _safe_edit_text(
            query,
            f"Delete *{_short(topic.name)}*?\n\nThis removes the topic and all its history.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Yes, delete", callback_data=f"tp:del_confirm:{topic.id}"
                        ),
                        InlineKeyboardButton("Cancel", callback_data=f"tp:del_cancel:{topic.id}"),
                    ]
                ]
            ),
            parse_mode="Markdown",
        )

    elif action == "del_confirm":
        name = topic.name
        await store.delete_topic(topic.id)
        await _safe_edit_text(query, f"✓ *{_short(name)}* deleted.", parse_mode="Markdown")

    elif action == "del_cancel":
        text, keyboard = _topic_card_expanded(topic)
        await _safe_edit_text(query, text, reply_markup=keyboard, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /topics
# ---------------------------------------------------------------------------


def _topic_card_text(t: Topic, lang: str = "English") -> str:
    query_text = t.shown_description or t.shown_name
    status = _t(lang, "paused_lbl") if t.paused else _sched_label(t, lang)
    last = t.last_sent_at.strftime("%d %b") if t.last_sent_at else _t(lang, "never_sent")
    return f"📌 *{t.shown_name}*\n_{query_text}  ·  {status}  ·  {last}_"


def _topic_card(t: Topic, lang: str = "English") -> tuple[str, InlineKeyboardMarkup]:
    """Compact card — just the primary action and an Edit button."""
    tid = str(t.id)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶ Check now", callback_data=f"tp:check:{tid}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"tp:edit:{tid}"),
            ]
        ]
    )
    return _topic_card_text(t, lang), keyboard


def _topic_card_expanded(t: Topic, lang: str = "English") -> tuple[str, InlineKeyboardMarkup]:
    """Expanded card — all management buttons + a Close row."""
    tid = str(t.id)
    pause_lbl = "▶ Resume" if t.paused else "⏸ Pause"
    pause_act = "resume" if t.paused else "pause"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶ Check now", callback_data=f"tp:check:{tid}"),
                InlineKeyboardButton(pause_lbl, callback_data=f"tp:{pause_act}:{tid}"),
            ],
            [
                InlineKeyboardButton("✏️ Rename", callback_data=f"tp:rename:{tid}"),
                InlineKeyboardButton("🔍 Query", callback_data=f"tp:describe:{tid}"),
                InlineKeyboardButton("📚 Sources", callback_data=f"tp:sources:{tid}"),
            ],
            [
                InlineKeyboardButton("📅", callback_data=f"tp:schedule:{tid}"),
                InlineKeyboardButton("🔄 Reset", callback_data=f"tp:reset:{tid}"),
                InlineKeyboardButton("🗑", callback_data=f"tp:delete:{tid}"),
            ],
            [InlineKeyboardButton("✕ Close", callback_data=f"tp:close:{tid}")],
        ]
    )
    return _topic_card_text(t, lang), keyboard


async def cmd_topics(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    topics = await store.get_topics(tg.id)
    lang = await store.get_user_language(tg.id)
    if not topics:
        await update.message.reply_text(
            "You have no topics yet. Use /add\\_topic to create one.", parse_mode="Markdown"
        )
        return
    await update.message.reply_text(_t(lang, "topics_header", n=len(topics)))
    for t in topics:
        text, keyboard = _topic_card(t, lang)
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /timezone — change the profile-level timezone (every topic at once)
# ---------------------------------------------------------------------------


async def cmd_timezone(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Sets the timezone for the whole profile (every topic at once) — not
    per-topic. Users were confused that changing it only affected one topic."""
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    lang = await _lang(tg.id)
    current = await store.get_user_timezone(tg.id)
    current_label = next((lbl for lbl, z in _TIMEZONES if z == current), current)
    await update.message.reply_text(
        _t(lang, "tz_current", label=current_label),
        reply_markup=_timezone_keyboard("tzprofile:"),
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /language
# ---------------------------------------------------------------------------

_LANGUAGE_OPTIONS = [
    ("🇬🇧 English", "English"),
    ("🇷🇺 Русский", "Russian"),
    ("🇪🇸 Español", "Spanish"),
    ("🇫🇷 Français", "French"),
    ("🇩🇪 Deutsch", "German"),
    ("🇸🇦 العربية", "Arabic"),
    ("🇨🇳 中文", "Chinese"),
    ("🇵🇹 Português", "Portuguese"),
]

# Maps stored language key → native display name (e.g. "Russian" → "Русский")
_LANGUAGE_NATIVE = {key: label.split(" ", 1)[1] for label, key in _LANGUAGE_OPTIONS}

_LANGUAGE_CONFIRMED = {
    "English": "✓ Digests will now be written in English.",
    "Russian": "✓ Дайджесты теперь будут на русском языке.",
    "Spanish": "✓ Los resúmenes se escribirán en español.",
    "French": "✓ Les résumés seront désormais rédigés en français.",
    "German": "✓ Die Digests werden jetzt auf Deutsch geschrieben.",
    "Arabic": "✓ سيتم كتابة الملخصات باللغة العربية.",
    "Chinese": "✓ 摘要将以中文撰写。",
    "Portuguese": "✓ Os resumos serão escritos em português.",
}

_LANGUAGE_PROMPT: dict[str, tuple[str, str]] = {
    "English": (
        "Current language: *{lang}*\n\nChoose the language for your digests:",
        "✏️ Other — type it",
    ),
    "Russian": (
        "Текущий язык: *{lang}*\n\nВыберите язык дайджестов:",
        "✏️ Другой — напишите",
    ),
    "Spanish": (
        "Idioma actual: *{lang}*\n\nElige el idioma de tus resúmenes:",
        "✏️ Otro — escríbelo",
    ),
    "French": (
        "Langue actuelle : *{lang}*\n\nChoisissez la langue de vos résumés :",
        "✏️ Autre — tapez-le",
    ),
    "German": (
        "Aktuelle Sprache: *{lang}*\n\nWähle die Sprache deiner Digests:",
        "✏️ Andere — tippe sie",
    ),
    "Arabic": (
        "اللغة الحالية: *{lang}*\n\nاختر لغة الملخصات:",
        "✏️ أخرى — اكتبها",
    ),
    "Chinese": (
        "当前语言：*{lang}*\n\n选择摘要语言：",
        "✏️ 其他——请输入",
    ),
    "Portuguese": (
        "Idioma atual: *{lang}*\n\nEscolha o idioma dos resumos:",
        "✏️ Outro — escreva",
    ),
}


async def cmd_language(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    current = await store.get_user_language(tg.id)
    prompt_tpl, other_label = _LANGUAGE_PROMPT.get(current, _LANGUAGE_PROMPT["English"])
    buttons = [
        [
            InlineKeyboardButton(  # noqa: E501
                f"{'✓ ' if lang == current else ''}{label}", callback_data=f"lang:{lang}"
            )
        ]
        for label, lang in _LANGUAGE_OPTIONS
    ]
    buttons.append([InlineKeyboardButton(other_label, callback_data="lang:__other__")])
    await update.message.reply_text(
        prompt_tpl.format(lang=_LANGUAGE_NATIVE.get(current, current)),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def _translate_topics(telegram_id: int, language: str) -> None:
    """Translate display_name and display_description for all user topics.

    If language is English, clears display fields so originals show through.
    Runs best-effort — a failure here doesn't block the language save.
    """
    from pydantic import BaseModel as _BM
    from pydantic_ai import Agent as _Agent

    from agent.services.llm import build_model as _build

    topics = await store.get_topics(telegram_id)
    if not topics:
        return

    if language == "English":
        for t in topics:
            await store.set_topic_display_fields(t.id, None, None)
        return

    class _Row(_BM):
        id: str
        name: str
        description: str | None

    class _Out(_BM):
        translations: list[_Row]

    agent: _Agent[None, _Out] = _Agent(
        _build("fast"),
        output_type=_Out,
        system_prompt=(
            f"Translate the given topic names and descriptions into {language}. "
            f"Write place names and proper nouns as they are conventionally written in {language} "
            f"(e.g. 'Russia-Ukraine' → 'Россия-Украина' in Russian). "
            "Return every topic in the same order; include the original id unchanged."
        ),
    )
    rows = [_Row(id=str(t.id), name=t.name, description=t.description) for t in topics]
    prompt = f"Translate these {len(rows)} topic(s) into {language}:\n" + "\n".join(
        f"- id={r.id}  name={r.name!r}  description={r.description!r}" for r in rows
    )
    result = await agent.run(prompt)
    by_id = {r.id: r for r in result.output.translations}
    for t in topics:
        tr = by_id.get(str(t.id))
        if tr:
            await store.set_topic_display_fields(t.id, tr.name, tr.description)


async def _cb_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await _safe_answer(query)
    tg = update.effective_user
    if tg is None:
        return
    lang = query.data[len("lang:") :]
    if lang == "__other__":
        context.user_data["awaiting_language"] = True  # type: ignore[index]
        current = await store.get_user_language(tg.id)
        other_prompts = {
            "Russian": "Напишите язык (например, *итальянский*, *японский*, *украинский*):",
            "Spanish": "Escribe el idioma (ej. *italiano*, *japonés*, *ucraniano*):",
            "French": "Tapez la langue (ex. *italien*, *japonais*, *ukrainien*) :",
            "German": "Tippe die Sprache ein (z.B. *Italienisch*, *Japanisch*, *Ukrainisch*):",
            "Arabic": "اكتب اللغة (مثلاً *الإيطالية*، *اليابانية*، *الأوكرانية*):",
            "Chinese": "请输入语言（例如*意大利语*、*日语*、*乌克兰语*）：",
            "Portuguese": "Escreva o idioma (ex. *italiano*, *japonês*, *ucraniano*):",
        }
        prompt = other_prompts.get(
            current, "Type the language you want (e.g. *Italian*, *Japanese*, *Ukrainian*):"
        )
        await _safe_edit_text(query, prompt, parse_mode="Markdown")
        return
    await store.set_user_language(tg.id, lang)
    _lang_cache[tg.id] = lang
    confirm = _LANGUAGE_CONFIRMED.get(lang, f"✓ Digests will now be written in {lang}.")
    await _safe_edit_text(query, confirm)
    await _translate_topics(tg.id, lang)


# ---------------------------------------------------------------------------
# /check
# ---------------------------------------------------------------------------


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    ul = await _lang(tg.id)
    name = " ".join(context.args or []).strip()
    if not name:
        await _topic_picker(update, tg.id, "check", "Which topic?")
        return
    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    fetch_msg = _t(ul, "fetch_status", name=topic.shown_name)
    await update.message.reply_text(fetch_msg, parse_mode="Markdown")
    try:
        await jobs._run_digest(topic, context.bot)
    except Exception:  # noqa: BLE001
        logger.exception("/check failed for topic {}", topic.id)
        await update.message.reply_text(_t(ul, "err_moment"))


# ---------------------------------------------------------------------------
# /reset
# ---------------------------------------------------------------------------


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    ul = await _lang(tg.id)
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
        _t(ul, "cleared_n", n=n, name=_short(topic.shown_name)), parse_mode="Markdown"
    )


# ---------------------------------------------------------------------------
# /rename
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
# /describe — update what the LLM searches for
# ---------------------------------------------------------------------------


async def cmd_describe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    args = context.args or []
    raw = " ".join(args)
    if "|" not in raw:
        await update.message.reply_text(
            "Usage: /describe <topic name> | <new research focus>\n\n"
            "Example: /describe Ecology | international laws on biodiversity, "
            "endangered species, and plastic pollution since 2024",
            parse_mode="Markdown",
        )
        return
    topic_name, new_desc = (p.strip() for p in raw.split("|", 1))
    if not new_desc:
        await update.message.reply_text("Please provide a description after the `|`.")
        return
    topic = await store.get_topic_by_name(tg.id, topic_name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{topic_name}*.", parse_mode="Markdown")
        return
    await store.update_topic(topic.id, description=new_desc)
    await update.message.reply_text(
        f"✓ *{topic.name}* will now research:\n_{new_desc}_\n\n"
        "Use /reset then /check to fetch fresh results with the new focus.",
        parse_mode="Markdown",
    )


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
# /delete_topic
# ---------------------------------------------------------------------------


async def cmd_delete_topic(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    await _topic_picker(update, tg.id, "delete", "Which topic do you want to delete?")


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
# Free-text handler
# ---------------------------------------------------------------------------


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text or not await _allowed(update):
        return
    text = update.message.text.strip()
    ud = context.user_data
    tg_user = update.effective_user
    ul = await _lang(tg_user.id) if tg_user else "English"

    if text.lower() == "/cancel" and ud is not None:
        for key in (
            "awaiting_rename_id",
            "awaiting_rename_name",
            "awaiting_describe_id",
            "awaiting_describe_name",
            "awaiting_source_id",
            "awaiting_source_name",
            "awaiting_block_id",
            "awaiting_block_name",
            "new_topic_source_guidance",
            "new_topic_sides_json",
            "awaiting_language",
        ):
            ud.pop(key, None)
        await update.message.reply_text(_t(ul, "cancelled"))
        return

    # Free-text language input (triggered by "Other" in /language)
    if ud is not None and ud.get("awaiting_language"):
        tg = update.effective_user
        if tg is not None and text:
            ud["awaiting_language"] = False
            await store.set_user_language(tg.id, text)
            _lang_cache[tg.id] = text
            confirm = _LANGUAGE_CONFIRMED.get(text, f"✓ Digests will now be written in {text}.")
            await update.message.reply_text(confirm)
            await _translate_topics(tg.id, text)
        return

    # Inline rename (triggered by ✏️ Rename button on topic card)
    if ud is not None and ud.get("awaiting_rename_id"):
        from uuid import UUID

        topic_id = UUID(ud.pop("awaiting_rename_id"))
        old_name = ud.pop("awaiting_rename_name", "")
        await store.update_topic(topic_id, name=text)
        await update.message.reply_text(
            f"✓ *{old_name}* renamed to *{text}*.", parse_mode="Markdown"
        )
        return

    # Inline describe (triggered by 📝 Research focus button on topic card)
    if ud is not None and ud.get("awaiting_describe_id"):
        from uuid import UUID

        topic_id = UUID(ud.pop("awaiting_describe_id"))
        topic_name = ud.pop("awaiting_describe_name", "")
        await update.message.chat.send_action(ChatAction.TYPING)
        results = await asyncio.gather(
            research.expand_query(text, topic_name),
            research.generate_source_guidance(text, topic_name),
            research.identify_sides(text, topic_name),
            return_exceptions=True,
        )
        expanded = results[0] if not isinstance(results[0], BaseException) else text
        guidance = results[1] if not isinstance(results[1], BaseException) else None
        sides = results[2] if not isinstance(results[2], BaseException) else None
        await store.update_topic(topic_id, description=expanded)
        if guidance:
            await store.update_topic(topic_id, source_guidance=guidance)
        if sides is not None:
            await store.update_topic(
                topic_id, sides_json=json.dumps([s.model_dump() for s in sides])
            )
        await update.message.reply_text(
            f"✓ *{topic_name}* will now research:\n_{expanded}_\n\n"
            "Use /reset then /check to fetch fresh results.",
            parse_mode="Markdown",
        )
        return

    # Inline add source (triggered by ➕ Add source button in sources view)
    if ud is not None and ud.get("awaiting_source_id"):
        from uuid import UUID

        topic_id = UUID(ud.pop("awaiting_source_id"))
        topic_name = ud.pop("awaiting_source_name", "")
        domain = text.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
        topic = await store.get_topic(update.effective_user.id, topic_id)  # type: ignore[union-attr]
        if topic is None:
            await update.message.reply_text(_t(ul, "topic_nf"))
            return
        if domain not in topic.sources:
            await store.update_topic(topic_id, sources=[*topic.sources, domain])
        await update.message.reply_text(
            f"✓ Added *{domain}* to *{topic_name}*.", parse_mode="Markdown"
        )
        return

    # Inline add ignored source (triggered by 🚫 Always ignore button)
    if ud is not None and ud.get("awaiting_block_id"):
        from uuid import UUID

        topic_id = UUID(ud.pop("awaiting_block_id"))
        topic_name = ud.pop("awaiting_block_name", "")
        domain = text.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
        topic = await store.get_topic(update.effective_user.id, topic_id)  # type: ignore[union-attr]
        if topic is None:
            await update.message.reply_text(_t(ul, "topic_nf"))
            return
        if domain not in topic.excluded_sources:
            await store.update_topic(topic_id, excluded_sources=[*topic.excluded_sources, domain])
        await update.message.reply_text(
            f"✓ *{domain}* will be ignored for *{topic_name}*.", parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        "Use commands to interact:\n/add\\_topic · /topics · /check · /pause · /resume",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------


async def _post_init(app: Application) -> None:  # type: ignore[type-arg]
    await store.apply_migrations()
    await app.bot.delete_my_commands()
    _cmds_en = [
        BotCommand("start", "Main menu"),
        BotCommand("topics", "Show all your topics"),
        BotCommand("add_topic", "Track a new topic"),
        BotCommand("check", "Get a digest now"),
        BotCommand("timezone", "Update your timezone"),
        BotCommand("language", "Set digest language"),
    ]
    await app.bot.set_my_commands(_cmds_en)
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Главное меню"),
            BotCommand("topics", "Ваши темы"),
            BotCommand("add_topic", "Добавить тему"),
            BotCommand("check", "Получить дайджест сейчас"),
            BotCommand("timezone", "Изменить часовой пояс"),
            BotCommand("language", "Язык дайджестов"),
        ],
        language_code="ru",
    )
    # Without this, Telegram defaults the menu button to a generic attachment-style
    # icon until the user starts typing "/". Forcing MenuButtonCommands makes it
    # always show the commands list.
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def build_application() -> Application:  # type: ignore[type-arg]
    """Build the PTB Application with all handlers. Used by both polling and webhook."""
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    # concurrent_updates: without this, PTB processes updates one at a time off its
    # internal queue — a slow digest job (1-2 min of LLM/search calls) would block the
    # bot from handling anything else, for any user, until it finishes. With it, each
    # update runs as its own task so the bot stays responsive while one is in flight.
    app = ApplicationBuilder().token(token).post_init(_post_init).concurrent_updates(True).build()

    # One ConversationHandler handles both /add_topic and /schedule
    topic_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add_topic", cmd_add_topic),
            CommandHandler("schedule", cmd_schedule),
            CallbackQueryHandler(_start_add_topic_cb, pattern=r"^start:add_topic$"),
            CallbackQueryHandler(_tp_schedule_entry, pattern=r"^tp:schedule:"),
        ],
        states={
            _AT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, _got_desc)],
            _AT_NAME_CONFIRM: [
                CallbackQueryHandler(_name_confirmed, pattern=r"^name:ok"),
                CallbackQueryHandler(_name_rename_prompt, pattern=r"^name:rename"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_custom_name),
            ],
            _SC_PICK: [CallbackQueryHandler(_sc_got_topic, pattern=r"^sc_pick:")],
            _ASK_SCHED_TYPE: [
                CallbackQueryHandler(_got_sched_type, pattern=r"^sched:"),
                CallbackQueryHandler(_back_from_sched_type, pattern=r"^nav:back$"),
            ],
            _ASK_SCHED_DAYS: [
                CallbackQueryHandler(_toggle_day, pattern=r"^day_toggle:"),
                CallbackQueryHandler(_done_days, pattern=r"^day_done$"),
                CallbackQueryHandler(_got_single_day, pattern=r"^dow_single:"),
                CallbackQueryHandler(_back_from_sched_days, pattern=r"^nav:back$"),
            ],
            _ASK_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_time_text),
                CallbackQueryHandler(_back_from_time, pattern=r"^nav:back$"),
            ],
            _ASK_ADD_ANOTHER: [CallbackQueryHandler(_got_add_another, pattern=r"^slot:")],
            _AT_SOURCES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_sources_text),
                CallbackQueryHandler(_got_sources_skip, pattern=r"^sources:skip"),
            ],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, _conversation_timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", _cancel),
            # Re-entering while already mid-flow (e.g. user abandoned a previous attempt and
            # clicks "Add topic" again) restarts cleanly instead of being silently dropped —
            # these entry points only match new conversations otherwise.
            CommandHandler("add_topic", cmd_add_topic),
            CommandHandler("schedule", cmd_schedule),
            CallbackQueryHandler(_start_add_topic_cb, pattern=r"^start:add_topic$"),
            CallbackQueryHandler(_tp_schedule_entry, pattern=r"^tp:schedule:"),
        ],
        conversation_timeout=600,  # 10 min — abandoned flows clean up instead of staying stuck
        per_message=False,
    )

    app.add_handler(topic_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("timezone", cmd_timezone))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("add_source", cmd_add_source))
    app.add_handler(CommandHandler("del_source", cmd_del_source))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("describe", cmd_describe))
    app.add_handler(CommandHandler("delete_topic", cmd_delete_topic))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CallbackQueryHandler(on_start_nav, pattern=r"^start:(topics|check)$"))
    app.add_handler(CallbackQueryHandler(on_topic_panel, pattern=r"^tp:"))
    app.add_handler(CallbackQueryHandler(on_topic_action, pattern=r"^ta:"))
    app.add_handler(CallbackQueryHandler(on_topic_delete_confirm, pattern=r"^td:"))
    app.add_handler(CallbackQueryHandler(on_profile_tz_set, pattern=r"^tzprofile:"))
    app.add_handler(CallbackQueryHandler(_cb_language, pattern=r"^lang:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app


def main() -> None:
    setup_logging()
    logger.info("starting elephant bot (polling)")
    build_application().run_polling()


if __name__ == "__main__":
    main()
