"""Admin web interface for Eat the Elephant.

Routes live under /admin. Protected by ADMIN_PASSWORD env var (cookie-based
session). If ADMIN_PASSWORD is not set, all /admin routes return 404.

Two roles:
  full  — the main admin (password from ADMIN_PASSWORD env var). Can read and
           write everything, including managing the read-only admin.
  ro    — read-only admin (password stored in DB via the Settings page). Can
           view everything; all mutating routes return 403.

Auth is stateless: the cookie stores '{role}:{HMAC(password, salt)}', so it
invalidates automatically when the password changes, with no server-side session
store needed. The RO password hash lives in elephant_admin_settings (DB).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from loguru import logger
from telegram import InputProfilePhotoStatic

from agent.config import get_settings
from agent.logging_setup import LOG_FILE
from agent.services import db
from elephant import jobs, store
from elephant.bot_state import get_bot
from elephant.models import Topic

_LOG_TAIL = 300  # number of recent log lines to show

router = APIRouter(prefix="/admin")
_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

_COOKIE = "elephant_admin"
_COOKIE_MAX_AGE = 60 * 60 * 8  # 8 hours
_RO_SETTINGS_KEY = "ro_password_hash"
_RO_USERNAME_KEY = "ro_username"
_RO_HTMX = '<span class="text-red-400 text-xs">Read-only: cannot make changes.</span>'
_RO_RUN_HTMX = (
    '<span id="run-status" class="text-red-400 text-xs">Read-only: cannot make changes.</span>'
)
_FULL_SALT = b"elephant-admin-v1"
_RO_SALT = b"elephant-admin-ro-v1"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _hash(password: str, salt: bytes) -> str:
    return hmac.new(password.encode(), salt, hashlib.sha256).hexdigest()


def _make_cookie(role: Literal["full", "ro"], password: str) -> str:
    salt = _FULL_SALT if role == "full" else _RO_SALT
    return f"{role}:{_hash(password, salt)}"


def _enabled() -> bool:
    return bool(get_settings().admin_password)


async def _get_role(cookie: str | None) -> Literal["full", "ro"] | None:
    """Return 'full', 'ro', or None (not authenticated)."""
    if not cookie:
        return None
    pw = get_settings().admin_password
    full_expected = f"full:{_hash(pw, _FULL_SALT)}" if pw else None
    if (
        full_expected
        and cookie.startswith("full:")
        and secrets.compare_digest(cookie, full_expected)
    ):
        return "full"
    if cookie.startswith("ro:"):
        ro_hash = await store.get_admin_setting(_RO_SETTINGS_KEY)
        if ro_hash and secrets.compare_digest(cookie, f"ro:{ro_hash}"):
            return "ro"
    return None


def _render(
    template: str,
    request: Request,
    role: Literal["full", "ro"] = "full",
    **ctx: Any,
) -> HTMLResponse:
    settings = get_settings()
    ctx["env"] = settings.environment
    ctx["is_prod"] = settings.is_production
    ctx["is_readonly"] = role == "ro"
    return _templates.TemplateResponse(request, template, ctx)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if not _enabled():
        return Response(status_code=404)
    return _render("admin/login.html", request, error=None)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    s = get_settings()
    pw = s.admin_password
    # Full admin check — username + password both must match
    if (
        pw
        and secrets.compare_digest(username, s.admin_username)
        and secrets.compare_digest(password, pw)
    ):
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(
            _COOKIE,
            _make_cookie("full", pw),
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return resp
    # Read-only admin check — username + password hash both stored in DB
    ro_username = await store.get_admin_setting(_RO_USERNAME_KEY)
    ro_hash = await store.get_admin_setting(_RO_SETTINGS_KEY)
    if (
        ro_username
        and ro_hash
        and secrets.compare_digest(username, ro_username)
        and secrets.compare_digest(_hash(password, _RO_SALT), ro_hash)
    ):
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(
            _COOKIE,
            _make_cookie("ro", password),
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return resp
    return _render("admin/login.html", request, error="Wrong username or password")


@router.get("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)

    stats_rows = await db.fetch(
        """
        SELECT
          (SELECT COUNT(*) FROM elephant_users)                   AS total_users,
          (SELECT COUNT(*) FROM elephant_topics)                  AS total_topics,
          (SELECT COUNT(*) FROM elephant_topics WHERE NOT paused) AS active_topics,
          (SELECT COUNT(*) FROM elephant_topics WHERE paused)     AS paused_topics,
          (SELECT COUNT(DISTINCT telegram_id) FROM elephant_topics
           WHERE last_sent_at >= now() - INTERVAL '7 days')      AS active_users_7d,
          (SELECT COUNT(DISTINCT telegram_id) FROM elephant_topics
           WHERE last_sent_at >= now() - INTERVAL '30 days')     AS active_users_30d,
          (SELECT COUNT(*) FROM elephant_seen)                    AS total_seen,
          (SELECT COUNT(*) FROM elephant_topics
           WHERE NOT paused
           AND (last_sent_at IS NULL
                OR last_sent_at < now() - INTERVAL '48 hours'))  AS stale_topics
        """
    )
    stats = dict(stats_rows[0]) if stats_rows else {}

    # Activity chart
    activity = await db.fetch(
        """
        SELECT date_trunc('day', seen_at)::date AS day,
               COUNT(DISTINCT topic_id) AS digests
        FROM elephant_seen
        WHERE seen_at >= now() - INTERVAL '30 days'
        GROUP BY 1 ORDER BY 1
        """
    )
    activity_rows = [{"day": str(r["day"]), "digests": int(r["digests"])} for r in activity]
    max_digests = max((r["digests"] for r in activity_rows), default=1)

    # User growth
    growth = await db.fetch(
        """
        SELECT date_trunc('day', created_at)::date AS day, COUNT(*) AS n
        FROM elephant_users
        WHERE created_at >= now() - INTERVAL '30 days'
        GROUP BY 1 ORDER BY 1
        """
    )
    growth_rows = [{"day": str(r["day"]), "n": int(r["n"])} for r in growth]

    # Language breakdown
    langs = await db.fetch(
        """
        SELECT COALESCE(NULLIF(language, ''), 'Unknown') AS lang, COUNT(*) AS n
        FROM elephant_users
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    lang_rows = [{"lang": str(r["lang"]), "n": int(r["n"])} for r in langs]
    lang_total = sum(r["n"] for r in lang_rows) or 1

    # Geography: top timezones
    geo = await db.fetch(
        """
        SELECT COALESCE(NULLIF(timezone, ''), 'UTC') AS tz, COUNT(*) AS n
        FROM elephant_users
        GROUP BY 1 ORDER BY 2 DESC
        LIMIT 15
        """
    )
    geo_rows = [{"tz": str(r["tz"]), "n": int(r["n"])} for r in geo]
    geo_total = sum(r["n"] for r in geo_rows) or 1

    # Topics per user distribution
    dist = await db.fetch(
        """
        SELECT topic_count, COUNT(*) AS users
        FROM (
            SELECT telegram_id, COUNT(*) AS topic_count
            FROM elephant_topics
            GROUP BY telegram_id
        ) t
        GROUP BY topic_count ORDER BY topic_count
        """
    )
    dist_rows = [{"count": int(r["topic_count"]), "users": int(r["users"])} for r in dist]

    # Stale topics detail
    stale = await db.fetch(
        """
        SELECT t.id, t.name, t.last_sent_at, u.first_name, u.username, t.telegram_id
        FROM elephant_topics t
        JOIN elephant_users u ON u.telegram_id = t.telegram_id
        WHERE NOT t.paused
          AND (t.last_sent_at IS NULL OR t.last_sent_at < now() - INTERVAL '48 hours')
        ORDER BY t.last_sent_at ASC NULLS FIRST
        LIMIT 10
        """
    )

    # Recent digest activity
    recent = await db.fetch(
        """
        SELECT u.first_name, u.username, t.name AS topic_name,
               t.id AS topic_id, t.telegram_id, t.last_sent_at, t.paused
        FROM elephant_topics t
        JOIN elephant_users u ON u.telegram_id = t.telegram_id
        WHERE t.last_sent_at IS NOT NULL
        ORDER BY t.last_sent_at DESC
        LIMIT 20
        """
    )

    return _render(
        "admin/dashboard.html",
        request,
        role=role,
        stats=stats,
        activity=activity_rows,
        max_digests=max_digests,
        growth=growth_rows,
        lang_rows=lang_rows,
        lang_total=lang_total,
        geo_rows=geo_rows,
        geo_total=geo_total,
        dist_rows=dist_rows,
        stale=[dict(r) for r in stale],
        recent=[dict(r) for r in recent],
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)

    rows = await db.fetch(
        """
        SELECT u.telegram_id, u.first_name, u.username, u.created_at,
               u.language, u.timezone,
               COUNT(t.id)                                    AS topic_count,
               COUNT(t.id) FILTER (WHERE NOT t.paused)       AS active_count,
               MAX(t.last_sent_at)                           AS last_digest_at
        FROM elephant_users u
        LEFT JOIN elephant_topics t ON t.telegram_id = u.telegram_id
        GROUP BY u.telegram_id, u.first_name, u.username,
                 u.created_at, u.language, u.timezone
        ORDER BY u.created_at DESC
        """
    )
    return _render("admin/users.html", request, role=role, users=[dict(r) for r in rows])


@router.get("/users/{telegram_id}", response_class=HTMLResponse)
async def user_detail(
    telegram_id: int,
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
    saved: str | None = None,
    edit: str | None = None,
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)

    user_row = await db.fetchrow("SELECT * FROM elephant_users WHERE telegram_id = $1", telegram_id)
    if not user_row:
        return Response("User not found", status_code=404)

    topics = await store.get_topics(telegram_id)

    if topics:
        seen_rows = await db.fetch(
            """
            SELECT topic_id, COUNT(*) AS n
            FROM elephant_seen
            WHERE topic_id = ANY($1)
            GROUP BY topic_id
            """,
            [t.id for t in topics],
        )
        seen_by_topic = {r["topic_id"]: int(r["n"]) for r in seen_rows}
    else:
        seen_by_topic = {}

    return _render(
        "admin/user_detail.html",
        request,
        role=role,
        user=dict(user_row),
        topics=topics,
        seen_by_topic=seen_by_topic,
        saved=saved,
        editing=bool(edit),
    )


@router.post("/users/{telegram_id}", response_class=HTMLResponse)
async def user_save(
    telegram_id: int,
    elephant_admin: str | None = Cookie(default=None),
    language: str = Form(default=""),
    timezone: str = Form(default=""),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)
    if role != "full":
        return Response("Read-only access", status_code=403)

    changes: list[str] = []
    if language.strip():
        changes.append(f"language={language.strip()!r}")
    if timezone.strip():
        changes.append(f"timezone={timezone.strip()!r}")
    await store.update_user(
        telegram_id,
        language=language.strip() or None,
        timezone=timezone.strip() or None,
    )
    logger.info("admin: updated user {}", telegram_id)
    await store.log_admin_action("user.edit", str(telegram_id), "; ".join(changes) or "no changes")
    return RedirectResponse(f"/admin/users/{telegram_id}?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Topic detail + edit
# ---------------------------------------------------------------------------


@router.get("/topics/{topic_id}", response_class=HTMLResponse)
async def topic_detail(
    topic_id: UUID,
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
    saved: str | None = None,
    edit: str | None = None,
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)

    topic_row = await db.fetchrow(
        """
        SELECT t.*, u.first_name, u.username
        FROM elephant_topics t
        JOIN elephant_users u ON u.telegram_id = t.telegram_id
        WHERE t.id = $1
        """,
        topic_id,
    )
    if not topic_row:
        return Response("Topic not found", status_code=404)

    topic = Topic.model_validate(dict(topic_row))

    seen_count_row = await db.fetchrow(
        "SELECT COUNT(*) AS n FROM elephant_seen WHERE topic_id = $1", topic_id
    )
    seen_count = int(seen_count_row["n"]) if seen_count_row else 0

    slots_rows = await db.fetch(
        "SELECT * FROM elephant_topic_slots WHERE topic_id = $1 ORDER BY hour, minute",
        topic_id,
    )
    feedback = await db.fetch(
        "SELECT * FROM elephant_feedback WHERE topic_id = $1 ORDER BY created_at DESC LIMIT 10",
        topic_id,
    )

    owner = {"first_name": topic_row["first_name"], "username": topic_row["username"]}

    _day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def _fmt_slot(r: dict[str, object]) -> str:
        days_str = str(r.get("days") or "")
        if days_str:
            names = [_day_names[int(d)] for d in days_str.split(",") if d.strip().isdigit()]
            day_part = ", ".join(names) if names else days_str
        else:
            day_part = "Daily"
        h = int(str(r.get("hour") or 0))
        m = int(str(r.get("minute") or 0))
        every = int(str(r.get("every_n_weeks") or 0))
        cadence = f" every {every}w" if every >= 1 else ""
        return f"{day_part} at {h:02d}:{m:02d}{cadence}"

    slots = [_fmt_slot(dict(r)) for r in slots_rows]

    return _render(
        "admin/topic_detail.html",
        request,
        role=role,
        topic=topic,
        owner=owner,
        seen_count=seen_count,
        slots=slots,
        feedback=[dict(r) for r in feedback],
        saved=saved,
        editing=bool(edit),
    )


@router.post("/topics/{topic_id}", response_class=HTMLResponse)
async def topic_save(
    topic_id: UUID,
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
    description: str = Form(default=""),
    source_guidance: str = Form(default=""),
    feedback_notes: str = Form(default=""),
    sources: str = Form(default=""),
    excluded_sources: str = Form(default=""),
    timezone: str = Form(default=""),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)
    if role != "full":
        return Response("Read-only access", status_code=403)

    def _split(s: str) -> list[str]:
        return [x.strip() for x in s.splitlines() if x.strip()]

    kwargs: dict[str, object] = dict(
        description=description.strip() or None,
        source_guidance=source_guidance.strip() or None,
        feedback_notes=feedback_notes.strip() or None,
        sources=_split(sources),
        excluded_sources=_split(excluded_sources),
    )
    if timezone.strip():
        kwargs["timezone"] = timezone.strip()

    await store.update_topic(topic_id, **kwargs)
    logger.info("admin: updated topic {}", topic_id)
    name_row = await db.fetchrow("SELECT name FROM elephant_topics WHERE id = $1", topic_id)
    entity = str(name_row["name"]) if name_row else str(topic_id)
    await store.log_admin_action("topic.edit", entity, "settings updated")
    return RedirectResponse(f"/admin/topics/{topic_id}?saved=1", status_code=303)


@router.post("/topics/{topic_id}/pause", response_class=HTMLResponse)
async def topic_toggle_pause(
    topic_id: UUID,
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role != "full":
        return HTMLResponse(_RO_HTMX)

    row = await db.fetchrow("SELECT paused, name FROM elephant_topics WHERE id = $1", topic_id)
    if not row:
        return Response(status_code=404)
    new_paused = not row["paused"]
    await store.update_topic(topic_id, paused=new_paused)
    logger.info("admin: topic {} paused={}", topic_id, new_paused)
    action = "topic.pause" if new_paused else "topic.resume"
    await store.log_admin_action(action, str(row["name"]), "paused" if new_paused else "resumed")
    label = "Resume" if new_paused else "Pause"
    cls = "bg-green-700 hover:bg-green-600" if new_paused else "bg-yellow-700 hover:bg-yellow-600"
    badge = (
        '<span id="pause-badge" class="text-yellow-400 text-xs font-semibold">PAUSED</span>'
        if new_paused
        else '<span id="pause-badge" class="text-green-400 text-xs font-semibold">ACTIVE</span>'
    )
    btn = (
        f'<div id="pause-block" class="flex items-center gap-3">'
        f"{badge}"
        f'<button hx-post="/admin/topics/{topic_id}/pause"'
        f' hx-target="#pause-block" hx-swap="outerHTML"'
        f' class="px-3 py-1 rounded text-xs text-white {cls}">{label}</button>'
        f"</div>"
    )
    return HTMLResponse(btn)


@router.post("/topics/{topic_id}/clear-seen")
async def topic_clear_seen(
    topic_id: UUID,
    elephant_admin: str | None = Cookie(default=None),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role != "full":
        return HTMLResponse(_RO_HTMX)
    n = await store.clear_seen(topic_id)
    logger.info("admin: cleared {} seen records for topic {}", n, topic_id)
    name_row = await db.fetchrow("SELECT name FROM elephant_topics WHERE id = $1", topic_id)
    entity = str(name_row["name"]) if name_row else str(topic_id)
    await store.log_admin_action("topic.clear-seen", entity, f"cleared {n} seen records")
    return HTMLResponse(
        '<span id="seen-info" class="text-gray-400 text-sm">Cleared — 0 records now</span>'
    )


@router.post("/topics/{topic_id}/run")
async def topic_run_now(
    topic_id: UUID,
    elephant_admin: str | None = Cookie(default=None),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role != "full":
        return HTMLResponse(_RO_RUN_HTMX)
    bot = get_bot()
    if bot is None:
        return HTMLResponse(
            '<span id="run-status" class="text-red-400 text-sm">Bot not ready</span>'
        )

    async def _run() -> None:
        try:
            await jobs.run_topic_now(topic_id, bot)
            logger.info("admin: run-now complete for topic {}", topic_id)
        except Exception:
            logger.exception("admin: run-now failed for topic {}", topic_id)

    asyncio.create_task(_run())
    logger.info("admin: run-now started for topic {}", topic_id)
    name_row = await db.fetchrow("SELECT name FROM elephant_topics WHERE id = $1", topic_id)
    entity = str(name_row["name"]) if name_row else str(topic_id)
    await store.log_admin_action("topic.run", entity, "digest triggered manually")
    return HTMLResponse(
        '<span id="run-status" class="text-green-400 text-sm">'
        "Running… check Telegram in ~30s"
        "</span>"
    )


@router.post("/topics/{topic_id}/clear-and-run")
async def topic_clear_and_run(
    topic_id: UUID,
    elephant_admin: str | None = Cookie(default=None),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role != "full":
        return HTMLResponse(_RO_RUN_HTMX)
    bot = get_bot()
    if bot is None:
        return HTMLResponse(
            '<span id="run-status" class="text-red-400 text-sm">Bot not ready</span>'
        )
    n = await store.clear_seen(topic_id)
    logger.info("admin: cleared {} seen records before run-now for topic {}", n, topic_id)

    async def _run() -> None:
        try:
            await jobs.run_topic_now(topic_id, bot)
            logger.info("admin: clear-and-run complete for topic {}", topic_id)
        except Exception:
            logger.exception("admin: clear-and-run failed for topic {}", topic_id)

    asyncio.create_task(_run())
    name_row = await db.fetchrow("SELECT name FROM elephant_topics WHERE id = $1", topic_id)
    entity = str(name_row["name"]) if name_row else str(topic_id)
    details = f"cleared {n} records + triggered digest"
    await store.log_admin_action("topic.clear-and-run", entity, details)
    return HTMLResponse(
        '<span id="run-status" class="text-green-400 text-sm">'
        f"Cleared {n} seen records — running… check Telegram in ~30s"
        "</span>"
    )


# ---------------------------------------------------------------------------
# Topics list (all topics — quick access for editing)
# ---------------------------------------------------------------------------


@router.get("/topics", response_class=HTMLResponse)
async def topics_list(
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)

    rows = await db.fetch(
        """
        SELECT t.id, t.name, t.description, t.paused, t.last_sent_at,
               t.telegram_id, t.source_guidance, t.feedback_notes,
               u.first_name, u.username,
               (SELECT COUNT(*) FROM elephant_seen s WHERE s.topic_id = t.id) AS seen_count
        FROM elephant_topics t
        JOIN elephant_users u ON u.telegram_id = t.telegram_id
        ORDER BY t.last_sent_at DESC NULLS LAST
        """
    )
    return _render("admin/topics.html", request, role=role, topics=[dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Logs + audit
# ---------------------------------------------------------------------------


def _parse_log_line(line: str) -> dict[str, str]:
    parts = line.split(" | ", 3)
    if len(parts) == 4:
        return {
            "ts": parts[0].strip(),
            "level": parts[1].strip(),
            "source": parts[2].strip(),
            "msg": parts[3].strip(),
        }
    return {"ts": "", "level": "INFO", "source": "", "msg": line.strip()}


def _read_log_tail(n: int = _LOG_TAIL, level_filter: str = "") -> list[dict[str, str]]:
    path = Path(LOG_FILE)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    parsed = [_parse_log_line(ln) for ln in lines if ln.strip()]
    if level_filter and level_filter != "ALL":
        parsed = [p for p in parsed if p["level"].startswith(level_filter)]
    return list(reversed(parsed))  # newest first


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
    level: str = "ALL",
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)
    audit = await store.get_admin_log()
    lines = _read_log_tail(level_filter=level)
    return _render("admin/logs.html", request, role=role, audit=audit, lines=lines, level=level)


@router.get("/logs/lines", response_class=HTMLResponse)
async def log_lines_partial(
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
    level: str = "ALL",
) -> Response:
    role = await _get_role(elephant_admin)
    if role is None:
        return Response(status_code=401)
    lines = _read_log_tail(level_filter=level)
    return _render("admin/log_lines.html", request, role=role, lines=lines)


# ---------------------------------------------------------------------------
# Bot settings
# ---------------------------------------------------------------------------


@router.get("/bot", response_class=HTMLResponse)
async def bot_settings_page(
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
    saved: str | None = None,
    error: str | None = None,
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)

    bot = get_bot()
    bot_info = bot_name = bot_desc = bot_short_desc = bot_photo_b64 = None
    fetch_error: str | None = error

    if bot:
        try:
            bot_info_obj = await bot.get_me()
            bot_info = {
                "id": bot_info_obj.id,
                "username": bot_info_obj.username,
                "first_name": bot_info_obj.first_name,
            }
            name_obj = await bot.get_my_name()
            desc_obj = await bot.get_my_description()
            short_obj = await bot.get_my_short_description()
            bot_name = name_obj.name if name_obj else ""
            bot_desc = desc_obj.description if desc_obj else ""
            bot_short_desc = short_obj.short_description if short_obj else ""
            try:
                photos = await bot.get_user_profile_photos(bot_info_obj.id, limit=1)
                if photos.total_count > 0:
                    thumb = photos.photos[0][0]
                    f = await bot.get_file(thumb.file_id)
                    data = await f.download_as_bytearray()
                    bot_photo_b64 = base64.b64encode(bytes(data)).decode()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            fetch_error = str(exc)

    return _render(
        "admin/bot.html",
        request,
        role=role,
        bot_info=bot_info,
        bot_name=bot_name or "",
        bot_desc=bot_desc or "",
        bot_short_desc=bot_short_desc or "",
        bot_photo_b64=bot_photo_b64,
        saved=saved,
        error=fetch_error,
    )


@router.post("/bot", response_class=HTMLResponse)
async def bot_settings_save(
    elephant_admin: str | None = Cookie(default=None),
    name: str = Form(default=""),
    description: str = Form(default=""),
    short_description: str = Form(default=""),
    photo: UploadFile | None = None,
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)
    if role != "full":
        return Response("Read-only access", status_code=403)

    bot = get_bot()
    if not bot:
        return RedirectResponse("/admin/bot?error=Bot+not+available", status_code=303)

    changes: list[str] = []
    try:
        if name.strip():
            await bot.set_my_name(name.strip())
            changes.append(f"name → {name.strip()!r}")
        if description.strip():
            await bot.set_my_description(description.strip())
            changes.append("description updated")
        if short_description.strip():
            await bot.set_my_short_description(short_description.strip())
            changes.append("short description updated")
        if photo and photo.filename:
            photo_bytes = await photo.read()
            if photo_bytes:
                await bot.set_my_profile_photo(InputProfilePhotoStatic(photo_bytes))
                changes.append("profile photo updated")
    except Exception as exc:  # noqa: BLE001
        logger.exception("admin: bot settings save failed")
        msg = str(exc)[:120].replace(" ", "+")
        return RedirectResponse(f"/admin/bot?error={msg}", status_code=303)

    if changes:
        await store.log_admin_action("bot.settings", "bot", "; ".join(changes))
        logger.info("admin: bot settings updated: {}", ", ".join(changes))

    return RedirectResponse("/admin/bot?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Access settings (full admin only — manages read-only admin password)
# ---------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    elephant_admin: str | None = Cookie(default=None),
    saved: str | None = None,
    revoked: str | None = None,
    error: str | None = None,
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)
    if role != "full":
        return Response("Admin access required", status_code=403)

    s = get_settings()
    ro_active = await store.get_admin_setting(_RO_SETTINGS_KEY) is not None
    ro_username = await store.get_admin_setting(_RO_USERNAME_KEY) or ""
    return _render(
        "admin/settings.html",
        request,
        role=role,
        ro_active=ro_active,
        ro_username=ro_username,
        full_username=s.admin_username,
        saved=saved,
        revoked=revoked,
        error=error,
    )


@router.post("/settings/ro-set")
async def settings_ro_set(
    elephant_admin: str | None = Cookie(default=None),
    ro_username: str = Form(default=""),
    password: str = Form(default=""),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role != "full":
        return Response(status_code=403)
    s = get_settings()
    uname = ro_username.strip()
    pw = password.strip()
    if uname and uname == s.admin_username:
        return RedirectResponse("/admin/settings?error=username_conflict", status_code=303)
    if uname:
        await store.set_admin_setting(_RO_USERNAME_KEY, uname)
    if pw:
        await store.set_admin_setting(_RO_SETTINGS_KEY, _hash(pw, _RO_SALT))
    if uname or pw:
        await store.log_admin_action("settings.ro-set", "read-only admin", "credentials updated")
        logger.info("admin: read-only admin credentials updated")
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


@router.post("/settings/ro-revoke")
async def settings_ro_revoke(
    elephant_admin: str | None = Cookie(default=None),
) -> Response:
    if not _enabled():
        return Response(status_code=404)
    role = await _get_role(elephant_admin)
    if role != "full":
        return Response(status_code=403)
    await store.delete_admin_setting(_RO_SETTINGS_KEY)
    await store.delete_admin_setting(_RO_USERNAME_KEY)
    await store.log_admin_action("settings.ro-revoke", "read-only admin", "access revoked")
    logger.info("admin: read-only admin access revoked")
    return RedirectResponse("/admin/settings?revoked=1", status_code=303)


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _time_ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    now = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    diff = now - dt
    if diff < timedelta(minutes=1):
        return "just now"
    if diff < timedelta(hours=1):
        m = int(diff.total_seconds() / 60)
        return f"{m}m ago"
    if diff < timedelta(days=1):
        h = int(diff.total_seconds() / 3600)
        return f"{h}h ago"
    d = diff.days
    return f"{d}d ago"


_templates.env.globals["time_ago"] = _time_ago
