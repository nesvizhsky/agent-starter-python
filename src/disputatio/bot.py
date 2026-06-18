"""Telegram bot: all handlers and Application wiring for Disputatio.

Run locally with long polling (no public URL needed):

    uv run disputatio-bot

In production the same handlers run via webhook — see app.py.
"""

from __future__ import annotations

import asyncio
import contextlib

from loguru import logger
from pydantic_ai import Agent
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
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
from disputatio import jobs, research, store
from disputatio.models import Topic

# ---------------------------------------------------------------------------
# ConversationHandler states (shared by /add_topic and /schedule)
# ---------------------------------------------------------------------------

_AT_DESC = 0  # /add_topic: enter description
_AT_NAME_CONFIRM = 1  # /add_topic: confirm LLM-generated name
_ASK_SCHED_TYPE = 2  # both: pick frequency type
_ASK_SCHED_DAYS = 3  # both: pick days (custom/weekly/biweekly)
_ASK_TIME = 4  # both: type a time
_ASK_TZ = 5  # /add_topic only: pick timezone
_AT_SOURCES = 6  # /add_topic only: enter sources
_SC_PICK = 7  # /schedule: topic picker

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


_VALID_SIGNALS = {"good", "too_shallow", "too_long", "already_knew"}

# ---------------------------------------------------------------------------
# Schedule constants
# ---------------------------------------------------------------------------

_DOW_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DOW_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_SCHED_TYPE_LABELS: dict[str, str] = {
    "daily": "Every day",
    "twice_daily": "Twice daily",
    "weekdays": "Mon–Fri",
    "mwf": "Mon/Wed/Fri",
    "tuth": "Tue/Thu",
    "custom_days": "Pick days…",
    "weekly": "Once a week",
    "biweekly": "Every 2 weeks",
}

# Frequency types that require a day-selection step
_NEEDS_DAYS = frozenset({"custom_days", "weekly", "biweekly"})

# (display label, IANA timezone name)
_TIMEZONES = [
    ("UTC-8  LA", "America/Los_Angeles"),
    ("UTC-6  Chicago", "America/Chicago"),
    ("UTC-5  New York", "America/New_York"),
    ("UTC+0  London", "Europe/London"),
    ("UTC+1  Paris", "Europe/Paris"),
    ("UTC+2  Helsinki", "Europe/Helsinki"),
    ("UTC+3  Moscow", "Europe/Moscow"),
    ("UTC+4  Dubai", "Asia/Dubai"),
    ("UTC+5:30  India", "Asia/Kolkata"),
    ("UTC+8  Singapore", "Asia/Singapore"),
    ("UTC+9  Tokyo", "Asia/Tokyo"),
    ("UTC+10  Sydney", "Australia/Sydney"),
]

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

_WELCOME = (
    "Welcome to *Disputatio* — news from multiple perspectives.\n\n"
    "Track topics you care about. Get digests comparing how different outlets cover the same "
    "story, with rhetoric and bias signals."
)

_START_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("➕ Add topic", callback_data="start:add_topic"),
            InlineKeyboardButton("📋 My topics", callback_data="start:topics"),
        ],
        [
            InlineKeyboardButton("▶ Check now", callback_data="start:check"),
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
    await update.message.reply_text(_WELCOME, parse_mode="Markdown", reply_markup=_START_KEYBOARD)


async def _start_add_topic_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the add-topic conversation via the /start button."""
    query = update.callback_query
    if query is None or query.from_user is None:
        return ConversationHandler.END
    await query.answer()
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
    await query.answer()
    action = query.data.split(":", 1)[1]
    tg_id = query.from_user.id

    if action == "topics":
        topics = await store.get_topics(tg_id)
        if not topics:
            await query.message.reply_text(  # type: ignore[union-attr]
                "You have no topics yet.", reply_markup=_START_KEYBOARD
            )
        else:
            await query.message.reply_text(f"Your {len(topics)} topic(s):")  # type: ignore[union-attr]
            for t in topics:
                text, keyboard = _topic_card(t)
                await query.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")  # type: ignore[union-attr]

    elif action == "check":
        await _topic_picker(update, tg_id, "check", "Which topic?")


# ---------------------------------------------------------------------------
# /add_topic conversation — entry
# ---------------------------------------------------------------------------


async def cmd_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not await _allowed(update):
        return ConversationHandler.END
    if context.user_data is not None:
        context.user_data["sched_mode"] = "create"
    await update.message.reply_text(
        "What do you want to track?\n\nDescribe it in a sentence — I'll suggest a name."
    )
    return _AT_DESC


async def _got_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text or context.user_data is None:
        return _AT_DESC
    desc = update.message.text.strip()
    if not desc:
        await update.message.reply_text("Please describe what you want to track.")
        return _AT_DESC
    await update.message.chat.send_action(ChatAction.TYPING)
    results = await asyncio.gather(
        _generate_name(desc),
        research.expand_query(desc, ""),
        research.generate_source_guidance(desc, ""),
        return_exceptions=True,
    )
    name = (
        results[0]
        if not isinstance(results[0], BaseException)
        else " ".join(desc.split()[:4]).rstrip(".,!?")
    )
    expanded = results[1] if not isinstance(results[1], BaseException) else desc
    guidance = results[2] if not isinstance(results[2], BaseException) else None

    context.user_data["new_topic_name"] = name
    context.user_data["new_topic_desc"] = expanded
    context.user_data["new_topic_source_guidance"] = guidance

    await update.message.reply_text(
        f"📌 *{name}*\n🔍 _{expanded}_\n🌐 _{guidance}_\n\nLooks good?",
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
    return _AT_NAME_CONFIRM


async def _name_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        name = context.user_data.get("new_topic_name", "") if context.user_data else ""
        await query.edit_message_text(f"✓ *{name}*", parse_mode="Markdown")
    return await _ask_sched_type(update, context)


async def _name_rename_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Type a short label for this topic:")
    return _AT_NAME_CONFIRM


async def _got_custom_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text or context.user_data is None:
        return _AT_NAME_CONFIRM
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please type a short label.")
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

    if context.user_data is not None:
        context.user_data["sched_mode"] = "update"

    name = " ".join(context.args or []).strip()  # type: ignore[union-attr]
    if not name:
        topics = await store.get_topics(tg.id)
        if not topics:
            await update.message.reply_text(
                "You have no topics yet. Use /add\\_topic to create one.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(t.name, callback_data=f"sc_pick:{t.id}")] for t in topics]
        await update.message.reply_text(
            "Which topic to reschedule?", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return _SC_PICK

    topic = await store.get_topic_by_name(tg.id, name)
    if topic is None:
        await update.message.reply_text(f"No topic called *{name}*.", parse_mode="Markdown")
        return ConversationHandler.END

    if context.user_data is not None:
        context.user_data["sched_topic_id"] = str(topic.id)
    return await _ask_sched_type(update, context)


async def _sc_got_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Topic selected from the /schedule picker."""
    query = update.callback_query
    if query is None or query.data is None or context.user_data is None:
        return _SC_PICK
    await query.answer()
    topic_id_str = query.data.split(":", 1)[1]
    context.user_data["sched_topic_id"] = topic_id_str
    await query.edit_message_text("Changing schedule…")
    return await _ask_sched_type(update, context)


# ---------------------------------------------------------------------------
# Shared scheduling flow: type → days → time
# ---------------------------------------------------------------------------


async def _ask_sched_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Every day", callback_data="sched:daily"),
                InlineKeyboardButton("Twice daily", callback_data="sched:twice_daily"),
            ],
            [
                InlineKeyboardButton("Mon–Fri", callback_data="sched:weekdays"),
                InlineKeyboardButton("Mon/Wed/Fri", callback_data="sched:mwf"),
            ],
            [
                InlineKeyboardButton("Tue/Thu", callback_data="sched:tuth"),
                InlineKeyboardButton("Pick days…", callback_data="sched:custom_days"),
            ],
            [
                InlineKeyboardButton("Once a week", callback_data="sched:weekly"),
                InlineKeyboardButton("Every 2 weeks", callback_data="sched:biweekly"),
            ],
        ]
    )
    await _reply(update, "How often would you like updates?", reply_markup=keyboard)
    return _ASK_SCHED_TYPE


async def _got_sched_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None or context.user_data is None:
        return _ASK_SCHED_TYPE
    await query.answer()
    freq = query.data.split(":", 1)[1]
    context.user_data["new_topic_freq"] = freq
    await query.edit_message_text(f"✓ {_SCHED_TYPE_LABELS.get(freq, freq)}")

    if freq in _NEEDS_DAYS:
        mode = "single" if freq in {"weekly", "biweekly"} else "multi"
        context.user_data["sched_days_mode"] = mode
        context.user_data["selected_days"] = set()
        return await _ask_sched_days(update, context)
    return await _ask_time(update, context)


def _day_toggle_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    def lbl(i: int) -> str:
        return f"✓ {_DOW_SHORT[i]}" if i in selected else _DOW_SHORT[i]

    row1 = [InlineKeyboardButton(lbl(i), callback_data=f"day_toggle:{i}") for i in range(4)]
    row2 = [InlineKeyboardButton(lbl(i), callback_data=f"day_toggle:{i}") for i in range(4, 7)]
    done_row = [InlineKeyboardButton("Done ✓", callback_data="day_done")]
    return InlineKeyboardMarkup([row1, row2, done_row])


def _day_single_keyboard() -> InlineKeyboardMarkup:
    def _btn(i: int) -> InlineKeyboardButton:
        return InlineKeyboardButton(_DOW_SHORT[i], callback_data=f"dow_single:{i}")

    return InlineKeyboardMarkup([[_btn(i) for i in range(4)], [_btn(i) for i in range(4, 7)]])


async def _ask_sched_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    mode = ud.get("sched_days_mode", "single") if ud else "single"
    if mode == "multi":
        selected: set[int] = ud.get("selected_days", set()) if ud else set()
        await _reply(
            update,
            "Which days? Tap to select, then tap *Done ✓*.",
            reply_markup=_day_toggle_keyboard(selected),
            parse_mode="Markdown",
        )
    else:
        freq = ud.get("new_topic_freq", "weekly") if ud else "weekly"
        label = "week" if freq == "weekly" else "2 weeks"
        await _reply(
            update,
            f"Which day? (once every {label})",
            reply_markup=_day_single_keyboard(),
        )
    return _ASK_SCHED_DAYS


async def _toggle_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tap a day to toggle it in the multi-select picker."""
    query = update.callback_query
    if query is None or query.data is None or context.user_data is None:
        return _ASK_SCHED_DAYS
    await query.answer()
    day_idx = int(query.data.split(":", 1)[1])
    selected: set[int] = context.user_data.get("selected_days", set())
    if day_idx in selected:
        selected.discard(day_idx)
    else:
        selected.add(day_idx)
    context.user_data["selected_days"] = selected
    await query.edit_message_reply_markup(reply_markup=_day_toggle_keyboard(selected))
    return _ASK_SCHED_DAYS


async def _done_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Done button in multi-select day picker."""
    query = update.callback_query
    if query is None or context.user_data is None:
        return _ASK_SCHED_DAYS
    selected: set[int] = context.user_data.get("selected_days", set())
    if not selected:
        await query.answer("Pick at least one day.", show_alert=True)
        return _ASK_SCHED_DAYS
    await query.answer()
    days_str = ",".join(str(d) for d in sorted(selected))
    context.user_data["new_topic_schedule_days"] = days_str
    day_names = " / ".join(_DOW_SHORT[d] for d in sorted(selected))
    await query.edit_message_text(f"✓ {day_names}")
    return await _ask_time(update, context)


async def _got_single_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Day selected in single-select (weekly/biweekly)."""
    query = update.callback_query
    if query is None or query.data is None or context.user_data is None:
        return _ASK_SCHED_DAYS
    await query.answer()
    dow = int(query.data.split(":", 1)[1])
    context.user_data["new_topic_dow"] = dow
    await query.edit_message_text(f"✓ {_DOW_LABELS[dow]}")
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
    await _reply(
        update,
        "What time? Type it, e.g. *9:00* or *21:30*\n(your local time, 24-hour or am/pm)",
        parse_mode="Markdown",
    )
    return _ASK_TIME


async def _got_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text or context.user_data is None:
        return _ASK_TIME
    h, m = _parse_time(update.message.text)
    context.user_data["new_topic_hour"] = h
    context.user_data["new_topic_minute"] = m
    await update.message.reply_text(f"✓ {h:02d}:{m:02d}")

    if context.user_data.get("sched_mode") == "update":
        return await _update_schedule(update, context)
    return await _ask_tz(update, context)


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
    topic = await store.get_topic(tg.id, topic_id)
    if topic is None:
        if update.message:
            await update.message.reply_text("Topic not found.")
        return ConversationHandler.END

    freq = ud.pop("new_topic_freq", "daily")
    hour = ud.pop("new_topic_hour", 8)
    minute = ud.pop("new_topic_minute", 0)
    dow = ud.pop("new_topic_dow", topic.send_dow)
    schedule_days = ud.pop("new_topic_schedule_days", "")
    ud.pop("sched_mode", None)
    ud.pop("sched_days_mode", None)
    ud.pop("selected_days", None)

    await store.update_topic(
        topic_id,
        frequency=freq,
        send_hour=hour,
        send_minute=minute,
        send_dow=dow,
        schedule_days=schedule_days,
    )
    label = _sched_label_data(freq, hour, minute, dow, schedule_days, topic.timezone)
    msg = f"✓ Schedule updated for *{_short(topic.name)}*:\n{label}"
    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Timezone step (add_topic only)
# ---------------------------------------------------------------------------


async def _ask_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    def _tz_btn(lbl: str, zone: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(lbl, callback_data=f"tz:{zone}")

    rows = [
        [_tz_btn(lbl, zone) for lbl, zone in _TIMEZONES[:3]],
        [_tz_btn(lbl, zone) for lbl, zone in _TIMEZONES[3:6]],
        [_tz_btn(lbl, zone) for lbl, zone in _TIMEZONES[6:9]],
        [_tz_btn(lbl, zone) for lbl, zone in _TIMEZONES[9:]],
    ]
    await _reply(
        update,
        "What's your timezone?\n\n"
        "_Note: Telegram can't read your device timezone. "
        "Use /timezone to update it when you travel._",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )
    return _ASK_TZ


async def _got_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or context.user_data is None:
        return _ASK_TZ
    await query.answer()
    tz = query.data.split(":", 1)[1] if query.data else "UTC"
    context.user_data["new_topic_tz"] = tz
    label = next((lbl for lbl, z in _TIMEZONES if z == tz), tz)
    await query.edit_message_text(f"✓ {label}")
    return await _ask_sources(update, context)


# ---------------------------------------------------------------------------
# Sources step (add_topic only)
# ---------------------------------------------------------------------------


async def _ask_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = (
        "Any specific sources to track? Send a comma-separated list "
        "(e.g. *BBC, TASS, Al Jazeera*) or tap Skip."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="sources:skip")]])
    await _reply(update, msg, reply_markup=keyboard, parse_mode="Markdown")
    return _AT_SOURCES


async def _got_sources_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return _AT_SOURCES
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
    ud = context.user_data
    name = ud.pop("new_topic_name", "")
    desc = ud.pop("new_topic_desc", None)
    source_guidance = ud.pop("new_topic_source_guidance", None)
    freq = ud.pop("new_topic_freq", "daily")
    hour = ud.pop("new_topic_hour", 8)
    minute = ud.pop("new_topic_minute", 0)
    dow = ud.pop("new_topic_dow", 0)
    schedule_days = ud.pop("new_topic_schedule_days", "")
    tz = ud.pop("new_topic_tz", "UTC")
    ud.pop("sched_mode", None)
    ud.pop("sched_days_mode", None)
    ud.pop("selected_days", None)

    topic = await store.create_topic(
        tg.id,
        name=name,
        description=desc,
        sources=sources,
        frequency=freq,
        send_hour=hour,
        send_minute=minute,
        send_dow=dow,
        schedule_days=schedule_days,
        timezone=tz,
        source_guidance=source_guidance,
    )
    label = _sched_label_data(freq, hour, minute, dow, schedule_days, tz)
    tz_label = next((lbl for lbl, z in _TIMEZONES if z == tz), tz)
    msg = (
        f"✓ *{topic.name}* created.\n"
        f"{label}  ·  {tz_label}\n"
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
        for key in (
            "new_topic_name",
            "new_topic_desc",
            "new_topic_freq",
            "new_topic_hour",
            "new_topic_minute",
            "new_topic_dow",
            "new_topic_tz",
            "new_topic_schedule_days",
            "sched_mode",
            "sched_topic_id",
            "sched_days_mode",
            "selected_days",
        ):
            context.user_data.pop(key, None)
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Schedule display helpers
# ---------------------------------------------------------------------------


def _sched_label(topic: Topic) -> str:
    """Short human-readable schedule for /topics list: 'Mon/Wed/Fri · 09:00'."""
    return _sched_label_data(
        topic.frequency,
        topic.send_hour,
        topic.send_minute,
        topic.send_dow,
        topic.schedule_days,
        topic.timezone,
    )


def _sched_label_data(
    freq: str, hour: int, minute: int, dow: int, schedule_days: str, timezone: str
) -> str:
    freq_name = _SCHED_TYPE_LABELS.get(freq, freq)
    if freq in {"weekly", "biweekly"}:
        freq_name = f"{freq_name} ({_DOW_LABELS[dow]})"
    elif freq == "custom_days" and schedule_days:
        freq_name = " / ".join(_DOW_SHORT[int(d)] for d in schedule_days.split(",") if d.strip())
    tz_short = next((lbl.split()[0] for lbl, z in _TIMEZONES if z == timezone), "")
    time_str = f"{hour:02d}:{minute:02d}"
    if tz_short:
        return f"{freq_name} · {time_str} ({tz_short})"
    return f"{freq_name} · {time_str}"


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
        [InlineKeyboardButton(t.shown_name, callback_data=f"ta:{action}:{t.id}")]
        for t in topics
    ]
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
        updated = await store.get_topic(query.from_user.id, topic.id)
        if updated:
            text, keyboard = _topic_card(updated)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

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

    elif action == "delete":
        await query.edit_message_text(
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

    elif action == "set_tz":

        def _tz_btn(lbl: str, zone: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(lbl, callback_data=f"tzset:{zone}:{topic.id}")

        rows = [
            [_tz_btn(lbl, zone) for lbl, zone in _TIMEZONES[:3]],
            [_tz_btn(lbl, zone) for lbl, zone in _TIMEZONES[3:6]],
            [_tz_btn(lbl, zone) for lbl, zone in _TIMEZONES[6:9]],
            [_tz_btn(lbl, zone) for lbl, zone in _TIMEZONES[9:]],
        ]
        await query.edit_message_text(
            f"New timezone for *{_short(topic.name)}*:",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )


async def on_topic_delete_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for the delete-confirmation buttons (td:{confirm|cancel}:{topic_id})."""
    query = update.callback_query
    if query is None or query.from_user is None or query.data is None:
        return
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    _prefix, action, topic_id_str = parts

    from uuid import UUID

    if action == "cancel":
        await query.edit_message_text("Cancelled.")
        return

    topic = await store.get_topic(query.from_user.id, UUID(topic_id_str))
    if topic is None:
        await query.edit_message_text("Topic not found.")
        return

    await store.delete_topic(topic.id)
    await query.edit_message_text(f"✓ *{_short(topic.name)}* deleted.", parse_mode="Markdown")


async def on_tz_set(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for /timezone timezone buttons (tzset:{zone}:{topic_id})."""
    query = update.callback_query
    if query is None or query.from_user is None or query.data is None:
        return
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    _pfx, zone, topic_id_str = parts

    from uuid import UUID

    topic = await store.get_topic(query.from_user.id, UUID(topic_id_str))
    if topic is None:
        await query.edit_message_text("Topic not found.")
        return

    await store.update_topic(topic.id, timezone=zone)
    label = next((lbl for lbl, z in _TIMEZONES if z == zone), zone)
    await query.edit_message_text(
        f"✓ Timezone for *{_short(topic.name)}* → {label}.", parse_mode="Markdown"
    )


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
    rows.append([
        InlineKeyboardButton("➕ Always check", callback_data=f"tp:add_src:{tid}"),
        InlineKeyboardButton("🚫 Always ignore", callback_data=f"tp:add_blk:{tid}"),
    ])
    rows.append([InlineKeyboardButton("← Back", callback_data=f"tp:back:{tid}")])
    await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")


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
        await query.answer()
        return
    _, action, topic_id_str = parts

    await query.answer("Running digest…" if action == "check" else "")

    from uuid import UUID

    topic = await store.get_topic(query.from_user.id, UUID(topic_id_str))
    if topic is None:
        await query.edit_message_text("Topic not found.")
        return

    if action == "check":
        # Show running state inside the card (under the title, buttons stay)
        _, card_keyboard = _topic_card(topic)
        query_text = topic.shown_description or topic.shown_name
        last = topic.last_sent_at.strftime("%d %b") if topic.last_sent_at else "never sent"
        running_text = (
            f"📌 *{topic.shown_name}*\n"
            f"⏳ _Fetching digest…_\n"
            f"_{query_text}  ·  {last}_"
        )
        await query.edit_message_text(
            running_text, reply_markup=card_keyboard, parse_mode="Markdown"
        )
        # Also send a separate status message below the card
        status = await context.bot.send_message(
            chat_id=topic.telegram_id,
            text=(
                f"⏳ *Fetching digest for {topic.shown_name}…*\n"
                "_This takes 1–2 minutes. Other commands won't respond until it's done._"
            ),
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
            await context.bot.send_message(
                chat_id=topic.telegram_id, text="Something went wrong — try again."
            )
        updated = await store.get_topic(query.from_user.id, topic.id)
        if updated:
            text, keyboard = _topic_card(updated)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif action in ("pause", "resume"):
        paused = action == "pause"
        await store.update_topic(topic.id, paused=paused)
        updated = await store.get_topic(query.from_user.id, topic.id)
        if updated:
            text, keyboard = _topic_card(updated)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif action == "reset":
        n = await store.clear_seen(topic.id)
        text, keyboard = _topic_card(topic)
        await query.edit_message_text(
            text + f"\n\n✓ Cleared {n} seen articles.",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    elif action == "schedule":
        await query.message.reply_text(  # type: ignore[union-attr]
            f"To change the schedule for *{_short(topic.name)}*, type:\n/schedule {topic.name}",
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
        text, keyboard = _topic_card_expanded(topic)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif action == "close":
        text, keyboard = _topic_card(topic)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif action == "back":
        text, keyboard = _topic_card_expanded(topic)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

    elif action == "delete":
        await query.edit_message_text(
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
        await query.edit_message_text(f"✓ *{_short(name)}* deleted.", parse_mode="Markdown")

    elif action == "del_cancel":
        text, keyboard = _topic_card_expanded(topic)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /topics
# ---------------------------------------------------------------------------


def _topic_card_text(t: Topic) -> str:
    query_text = t.shown_description or t.shown_name
    status = "⏸ paused" if t.paused else _sched_label(t)
    last = t.last_sent_at.strftime("%d %b") if t.last_sent_at else "never sent"
    return f"📌 *{t.shown_name}*\n_{query_text}  ·  {status}  ·  {last}_"


def _topic_card(t: Topic) -> tuple[str, InlineKeyboardMarkup]:
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
    return _topic_card_text(t), keyboard


def _topic_card_expanded(t: Topic) -> tuple[str, InlineKeyboardMarkup]:
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
    return _topic_card_text(t), keyboard


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
    await update.message.reply_text(f"Your {len(topics)} topic(s):")
    for t in topics:
        text, keyboard = _topic_card(t)
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /timezone — change timezone for a topic
# ---------------------------------------------------------------------------


async def cmd_timezone(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not await _allowed(update):
        return
    tg = update.effective_user
    if tg is None:
        return
    await _topic_picker(update, tg.id, "set_tz", "Update timezone for which topic?")


# ---------------------------------------------------------------------------
# /language
# ---------------------------------------------------------------------------

_LANGUAGE_OPTIONS = [
    ("🇬🇧 English", "English"),
    ("🇷🇺 Russian", "Russian"),
    ("🇪🇸 Spanish", "Spanish"),
    ("🇫🇷 French", "French"),
    ("🇩🇪 German", "German"),
    ("🇸🇦 Arabic", "Arabic"),
    ("🇨🇳 Chinese", "Chinese"),
    ("🇵🇹 Portuguese", "Portuguese"),
]

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
    prompt_tpl, other_label = _LANGUAGE_PROMPT.get(
        current, _LANGUAGE_PROMPT["English"]
    )
    buttons = [
        [InlineKeyboardButton(  # noqa: E501
            f"{'✓ ' if lang == current else ''}{label}", callback_data=f"lang:{lang}"
        )]
        for label, lang in _LANGUAGE_OPTIONS
    ]
    buttons.append([InlineKeyboardButton(other_label, callback_data="lang:__other__")])
    await update.message.reply_text(
        prompt_tpl.format(lang=current),
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
    await query.answer()
    tg = update.effective_user
    if tg is None:
        return
    lang = query.data[len("lang:"):]
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
        await query.edit_message_text(prompt, parse_mode="Markdown")
        return
    await store.set_user_language(tg.id, lang)
    confirm = _LANGUAGE_CONFIRMED.get(lang, f"✓ Digests will now be written in {lang}.")
    await query.edit_message_text(confirm)
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
# /reset
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
# /synthesis
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
# Feedback callback (digest reaction buttons)
# ---------------------------------------------------------------------------

_NEGATIVE_SIGNALS = {"too_shallow", "too_long", "already_knew"}


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
            "awaiting_language",
        ):
            ud.pop(key, None)
        await update.message.reply_text("Cancelled.")
        return

    # Free-text language input (triggered by "Other" in /language)
    if ud is not None and ud.get("awaiting_language"):
        tg = update.effective_user
        if tg is not None and text:
            ud["awaiting_language"] = False
            await store.set_user_language(tg.id, text)
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
            return_exceptions=True,
        )
        expanded = results[0] if not isinstance(results[0], BaseException) else text
        guidance = results[1] if not isinstance(results[1], BaseException) else None
        await store.update_topic(topic_id, description=expanded)
        if guidance:
            await store.update_topic(topic_id, source_guidance=guidance)
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
            await update.message.reply_text("Topic not found.")
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
            await update.message.reply_text("Topic not found.")
            return
        if domain not in topic.excluded_sources:
            await store.update_topic(topic_id, excluded_sources=[*topic.excluded_sources, domain])
        await update.message.reply_text(
            f"✓ *{domain}* will be ignored for *{topic_name}*.", parse_mode="Markdown"
        )
        return

    # Feedback correction (triggered by 👎 button on digest)
    topic_id_fb = ud.get("awaiting_correction_topic") if ud is not None else None
    if topic_id_fb is not None and ud is not None:
        ud.pop("awaiting_correction_topic")
        if text.lower() != "/skip":
            try:
                await store.append_feedback_note(topic_id_fb, text)
                await update.message.reply_text("✓ Noted — I'll adjust future digests.")
            except Exception:  # noqa: BLE001
                logger.warning("failed to save correction for topic {}", topic_id_fb)
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
    await app.bot.delete_my_commands()
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Main menu"),
            BotCommand("topics", "Show all your topics"),
            BotCommand("add_topic", "Track a new topic"),
            BotCommand("check", "Get a digest now"),
            BotCommand("more", "Full analysis from last digest"),
            BotCommand("synthesis", "Weekly synthesis for a topic"),
            BotCommand("timezone", "Update timezone for a topic"),
            BotCommand("language", "Set digest language"),
        ]
    )


def build_application() -> Application:  # type: ignore[type-arg]
    """Build the PTB Application with all handlers. Used by both polling and webhook."""
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    app = ApplicationBuilder().token(token).post_init(_post_init).build()

    # One ConversationHandler handles both /add_topic and /schedule
    topic_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add_topic", cmd_add_topic),
            CommandHandler("schedule", cmd_schedule),
            CallbackQueryHandler(_start_add_topic_cb, pattern=r"^start:add_topic$"),
        ],
        states={
            _AT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, _got_desc)],
            _AT_NAME_CONFIRM: [
                CallbackQueryHandler(_name_confirmed, pattern=r"^name:ok"),
                CallbackQueryHandler(_name_rename_prompt, pattern=r"^name:rename"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_custom_name),
            ],
            _SC_PICK: [CallbackQueryHandler(_sc_got_topic, pattern=r"^sc_pick:")],
            _ASK_SCHED_TYPE: [CallbackQueryHandler(_got_sched_type, pattern=r"^sched:")],
            _ASK_SCHED_DAYS: [
                CallbackQueryHandler(_toggle_day, pattern=r"^day_toggle:"),
                CallbackQueryHandler(_done_days, pattern=r"^day_done$"),
                CallbackQueryHandler(_got_single_day, pattern=r"^dow_single:"),
            ],
            _ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, _got_time_text)],
            _ASK_TZ: [CallbackQueryHandler(_got_tz, pattern=r"^tz:")],
            _AT_SOURCES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _got_sources_text),
                CallbackQueryHandler(_got_sources_skip, pattern=r"^sources:skip"),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        per_message=False,
    )

    app.add_handler(topic_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("more", cmd_more))
    app.add_handler(CommandHandler("synthesis", cmd_synthesis))
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
    app.add_handler(CallbackQueryHandler(on_tz_set, pattern=r"^tzset:"))
    app.add_handler(CallbackQueryHandler(on_feedback, pattern=r"^fb:"))
    app.add_handler(CallbackQueryHandler(_cb_language, pattern=r"^lang:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app


def main() -> None:
    setup_logging()
    logger.info("starting disputatio bot (polling)")
    build_application().run_polling()


if __name__ == "__main__":
    main()
