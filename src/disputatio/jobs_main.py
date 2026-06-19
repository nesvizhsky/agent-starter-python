"""Local entrypoint for `disputatio-cron`: force-run all due digests.

    uv run disputatio-cron

Used in dev to trigger the cron pipeline immediately without waiting for the clock.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from telegram import Bot

from agent.config import get_settings
from agent.logging_setup import setup_logging
from disputatio import store
from disputatio.jobs import run_due_digests


async def _run() -> None:
    await store.apply_migrations()
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    async with Bot(token) as bot:
        digests = await run_due_digests(bot, force=True)
    logger.info("cron done — digests={}", digests)


def main() -> None:
    setup_logging()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
