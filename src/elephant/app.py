"""Production entrypoint: FastAPI hosting the Telegram webhook and cron tick.

    uv run fastapi run src/elephant/app.py   # production (reads $PORT)

Locally, use polling instead (uv run elephant-bot); this file is what Railway runs.

Two endpoints, both protected by a shared secret:
  POST /telegram/webhook  — Telegram delivers updates (verified by secret header)
  POST /cron/tick         — hourly scheduler; runs the digest + synthesis pipelines
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from loguru import logger
from telegram import Update
from telegram.ext import Application

from agent.config import get_settings
from agent.logging_setup import setup_logging
from elephant.admin import router as admin_router
from elephant.bot import _post_init, build_application
from elephant.bot_state import set_bot
from elephant.jobs import run_due_digests

_ptb: Application | None = None  # type: ignore[type-arg]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _ptb
    setup_logging()
    settings = get_settings()

    ptb = build_application()
    _ptb = ptb
    await ptb.initialize()
    # PTB only auto-calls post_init from run_polling/run_webhook; we drive manually.
    await _post_init(ptb)
    await ptb.start()
    set_bot(ptb.bot)

    if settings.public_url:
        url = f"{settings.public_url.rstrip('/')}/telegram/webhook"
        await ptb.bot.set_webhook(
            url=url,
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )
        logger.info("webhook registered at {}", url)
    else:
        logger.warning("PUBLIC_URL not set — webhook not registered")

    yield

    await ptb.stop()
    await ptb.shutdown()


app = FastAPI(title="Eat the Elephant", lifespan=lifespan)
app.include_router(admin_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    settings = get_settings()
    secret = settings.telegram_webhook_secret
    if not secret or not secrets.compare_digest(x_telegram_bot_api_secret_token or "", secret):
        raise HTTPException(status_code=403)
    ptb = _ptb
    if ptb is None:
        raise HTTPException(status_code=503)
    update = Update.de_json(await request.json(), ptb.bot)
    if update is not None:
        await ptb.process_update(update)
    return {"ok": True}


async def _run_due_digests_logged(ptb: Application) -> None:  # type: ignore[type-arg]
    """Background wrapper: run_due_digests() already isolates per-topic failures,
    but if something at the top level still raises, log it instead of losing it
    silently — there's no HTTP response left to carry the error by this point."""
    try:
        digests = await run_due_digests(ptb.bot)
        logger.info("cron tick done — digests={}", digests)
    except Exception:  # noqa: BLE001
        logger.exception("cron tick failed")


@app.post("/cron/tick")
async def cron_tick(
    x_cron_secret: str | None = Header(default=None),
) -> dict[str, bool]:
    settings = get_settings()
    if not settings.cron_secret or not secrets.compare_digest(
        x_cron_secret or "", settings.cron_secret
    ):
        raise HTTPException(status_code=401)
    ptb = _ptb
    if ptb is None:
        raise HTTPException(status_code=503)
    # Fire-and-forget: a digest run can take minutes (LLM + research calls per due
    # topic, sequentially), far past any HTTP/proxy timeout. Awaiting it inline made
    # the cron caller's request time out — Railway's curl-based cron then reports a
    # "crashed" deployment, and the request being killed could cut a digest off
    # mid-send. Acknowledge immediately; the actual work continues in the background.
    asyncio.create_task(_run_due_digests_logged(ptb))
    return {"started": True}


def main() -> None:
    import uvicorn

    setup_logging()
    uvicorn.run("elephant.app:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
