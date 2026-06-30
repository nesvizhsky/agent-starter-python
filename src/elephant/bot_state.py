"""Holds a reference to the live Telegram Bot instance.

Set once during app lifespan (app.py); read by admin routes that need to send
messages without creating a circular import between admin.py and app.py.
"""

from __future__ import annotations

from telegram import Bot

_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


def get_bot() -> Bot | None:
    return _bot
