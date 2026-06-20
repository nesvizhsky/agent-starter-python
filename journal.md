# Journal

This is the running trace of your thinking as you build. It's the most important
document in the project — more than any single piece of code.

**How to use it**
- Add an entry at *meaningful* moments: a decision (and **why**), something you
  learned, a dead end you backed out of, a milestone reached.
- **Not** every edit. Capture the thinking, not the keystrokes.
- Always **timestamp** with date **and** time. Newest entries go at the bottom.
- Both you and Claude should add entries.

Format:

```
## YYYY-MM-DD HH:MM — Short title
What you were trying to do, what you decided, and why. What you learned.
```

---

## 2026-05-29 12:00 — Project initialized from the agent starter
Cloned the starter. Next: fill in `docs/problem.md` (what can't I do today?) and the
"Your project" section of `README.md` (what am I building?). Then design before coding.

## 2026-06-15 — Added example #3: inspiration_bot (Telegram, both-directions agent)
Built the third worked example as a Telegram bot, deliberately keeping the repo single-spirit
(pure Python toolbox) instead of going React/Next + Better Auth — that's a separate starter,
not this one. Telegram dissolves the "auth" question: identity is the verified `telegram_id`
on every update; the users table is keyed on it; authorization is a thin optional allowlist.

Key decisions:
- **Environments as a first-class concept.** Added `ENVIRONMENT` (+ telegram/cron settings) to
  config.py. Same code, different *values* in `.env` vs Railway. Separate bot token per env is
  mandatory, not hygiene: two consumers on one token = Telegram 409. This is the example's spine.
- **Webhook vs polling, chosen by environment.** Local = long polling (no public URL); prod =
  webhook into a FastAPI app that also hosts the cron endpoint. One codebase.
- **Cron = frequent tick + per-user due-check.** Railway Cron (hourly) → `run_due_sends`, which
  honours each user's hour/timezone/cadence and is idempotent via `last_sent_at`. `is_due` is a
  pure function, unit-tested offline. Same `cron` command forces an immediate send in dev.
- **Tool-using agent with injected scope.** pydantic-ai `Deps(telegram_id)` is injected, never a
  tool argument — so the model physically can't reach another user's data. Read tools granted
  freely; one reversible write tool (set_schedule); delete + image-gen kept out of the model's
  hands (human-confirmed / orchestrated). This is the security lesson I most want to land.
- **Packaging:** multi-file example as a package (`examples/__init__.py` + the bot's `__init__.py`),
  so absolute imports work under both `python -m ...` and `fastapi run ...` (verified how
  fastapi-cli walks `__init__.py` parents). Added `pythonpath=["."]` so pytest can import it.

Verified the installed APIs before writing (pydantic-ai 1.104 deps/tools/BinaryContent; PTB 22.8
Application/handlers/webhook) rather than trusting memory. ruff + pyright clean; 9 offline tests.

## 2026-06-16 — perspectives.py: fast model can't cluster reliably

Gemini Flash Lite ("fast" tier) failed to recognise "Russia fires missiles at Kyiv" (BBC)
and "Russia conducts precision strike on military targets in Kyiv" (TASS) as the same event —
it kept them as separate stories. Upgraded clustering to "balanced" (Claude Sonnet) with a
more explicit prompt that gives a concrete example of same-event-different-framing. Tests pass.

Trade-off: "balanced" costs ~30× more per call than "fast", but clustering input is small
(~10–20 article headlines) and only runs once per digest. Absolute cost is still negligible.
Lesson: for tasks that require semantic reasoning across differently-framed text, fast/cheap
models are not reliable enough. Use "balanced" minimum for anything that needs to understand
meaning across framings.

## 2026-06-16 — Disputatio: stages 1–5 complete, ready to build

Named the project Disputatio (medieval academic debate form: argue all sides, community decides).
Greek philosophy vibe layered in: Socratic method, dialectic structure, the agora as metaphor.

Key design decisions locked in:
- **Multi-perspective + propaganda analysis on all topics/sources**: the core differentiator.
  Propaganda module reports signals found, never verdicts; same criteria applied to every source.
- **Freshness filter**: two passes — URL match + semantic similarity via pgvector embeddings.
  Catches the same event republished under different headlines.
- **Personas**: 16 characters, each with a pre-generated avatar image in R2 and a character
  intro card. Randomized per digest, user can pin one per topic.
- **Feedback**: quick tap row (5 signals) + extended free-text correction offered on negative
  taps. Free text parsed into structured prefs + stored as notes in topic.feedback_notes.
- **Weekly synthesis**: separate agent call on `build_model("smart")` — the one expensive
  call, justified because it synthesises a week into something genuinely new.
- **Repo renamed** from `agent-starter-python` to `disputatio` — this is the main project,
  not an example. Docs moved from `examples/disputatio/docs/` to root `docs/`.

Architecture: 10 modules in `src/disputatio/`, clean dependency chain, build + test in order.
Next: build migration 001 + store.py (step 1–2 of build order).

## 2026-06-17 — Fixed "Message is too long" crash in digest delivery

Bug: `telegram.error.BadRequest: Message is too long` in `jobs._send_digest`. The digest `main`
field allows 800 words, but 800 × ~5 chars = ~4000 chars — right at Telegram's 4096-char hard
limit. When the LLM ran slightly long, it crashed. The subsequent `ConnectError` spam was just
the bot retrying after the crash, not a separate issue.

Fix (two parts):
1. `jobs.py`: added `_chunk_text()` that splits on paragraph boundaries into ≤4096-char pieces.
   `_send_digest` now sends each chunk as a separate message, attaching the feedback keyboard
   only to the last one.
2. `digest.py`: tightened the word-count instruction from 800 → 600 words (also in the
   `DigestOutput.main` field description). At 600 words the expected character count is ~3000,
   giving a real buffer. The chunking is the reliable fix; the word-count tightening reduces
   the chance of hitting it in the first place.

## 2026-06-17 — Auto-name topics; fix research context pipeline

**UX change: `/add_topic` flow reversed.**
Previously: name first → optional description. Problem: users typed their full description as
the name (the archaeology topic ended up with a 60-word label). Now: description first → LLM
(fast tier) generates a 2-4 word name → user confirms with "Continue →" or taps "Rename it"
to override. The description becomes the research focus (more useful) and the name is short
(better for display). `/rename` still exists as a fallback.

**Pipeline bug found and fixed: `result.answer` vs `result.text`.**
The previous session added `context=result.answer` to research.py to give the perspectives
agent real Perplexity prose — but the `Research` model field is actually `text`, not `answer`.
So `context` was always None, which meant the perspectives agent had no real content to draw
on. Fixed to `context=result.text`. Offline tests caught this immediately (AttributeError on
the mock Research object). Lesson: always run the offline tests before committing pipeline
changes, even small ones.

## 2026-06-17 21:18 — Scheduling overhaul: flexible frequency, free-text time, /schedule + /timezone

**Why:** The old system had three hard-coded options (daily/twice-daily/weekly) and
seven fixed time buttons. User wanted: any day combination, any time (typed), ability
to change schedule later without re-creating a topic, and timezone update when traveling.

**What changed:**

*New frequency types* (8 total): `daily`, `twice_daily`, `weekdays` (Mon–Fri), `mwf`
(Mon/Wed/Fri), `tuth` (Tue/Thu), `custom_days` (multi-select via toggle keyboard),
`weekly`, `biweekly`. All flow through `is_due()` in jobs.py.

*Days before time:* The UX flow is now: schedule type → day picker (if needed) → time →
timezone → sources. Previously it was type → tz → time → day.

*Free-text time input:* User types "9:00" or "21:30" or "9am". `_parse_time()` handles
common formats. Minutes are stored in `send_minute` (DB migration 003) but scheduling
is still hourly (cron fires once/hour and checks `send_hour`). Sub-hour display is shown
but actual precision is ±1h. This is acceptable and honest.

*`/schedule` command:* Lets users change the schedule for any existing topic at any time.
Implemented by adding `/schedule` as a second entry point in the same ConversationHandler
as `/add_topic` — both paths share the `_ask_sched_type → _ask_sched_days → _ask_time`
handlers. `context.user_data["sched_mode"]` ("create" vs "update") determines what happens
at the end.

*`/timezone` command:* Separate command that updates timezone for an existing topic.
Telegram can't read device timezone — this is a real platform limitation. The best we can
do is make it easy to update manually. Explained this to user in the timezone prompt message.

*Bug found:* Day-of-week checks for weekdays/mwf/tuth/custom_days were inside the
`match` block that runs AFTER `if last_sent_at is None: return True`. This meant the
first delivery always fired, even on the wrong day (e.g., Saturday on a weekdays schedule).
Fixed by splitting the logic: day-of-week checks run first, then the `last_sent_at is None`
early return, then the "how long since last sent" check.

*DB:* Migration 003 adds `send_minute INT DEFAULT 0` and `schedule_days TEXT DEFAULT ''`.
Both backward compatible — existing topics default to minute=0 and no custom days.

## 2026-06-18 — Switched UI localisation from hardcoded dicts to LLM-on-demand

Was maintaining `_TR` with 5 language copies of every UI string — and still missing Arabic,
Chinese, Portuguese. User asked the obvious question: why not just have a module do this?

Replaced the whole `_TR` / `_SCHED_L10N` / `_DOW_LABELS_L10N` etc. stack with:
- `_UI` dict — English source strings only, the single truth
- `_ensure_ui(lang)` — one LLM call (fast model) that batch-translates all ~35 strings
  into a new language and caches the result in `_ui_cache`
- `_lang()` calls `_ensure_ui()` on first access, so every handler gets warm cache for free

`_t(lang, key, **fmt)` stays synchronous — no changes to the 30+ call sites.

Trade-off: first time a user sends a command in a new language, there's a small delay for
the LLM call (~1s). Every request after that is instant. Acceptable for rare language changes.

## 2026-06-19 21:00 — Removed feedback, synthesis, /more, Digest model (~660 lines)

These features were built but never actually used: no user ever clicked a feedback button,
synthesis was never triggered, /more required overflow state that no longer existed.
They were adding complexity and dead code paths.

Removed in commit c946ff8:
- `synthesis.py` deleted entirely
- `on_feedback`, `cmd_synthesis`, `cmd_more` handlers removed from `bot.py`
- `record_digest`, `save_feedback`, `append_feedback_note`, `get_recent_digests` removed from `store.py`
- `Digest` model, `last_synthesis_at`, `pinned_persona` removed from `models.py`
- `jobs.py` stripped to just `run_due_digests`
- DB tables (`disputatio_digests`, `disputatio_feedback`) left in place — no destructive migration needed

The `feedback_notes` field on Topic still exists (it's a writable topic field) but there's no longer
a way to add notes via bot commands. Could be wired to a future feature if ever needed.

Code archived on `archive/abandoned-features` branch in case it's ever needed.

## 2026-06-20 11:30 — Renamed package disputatio -> elephant ("Eat the Elephant")

Repo/package was still called `disputatio` from before the project found its real name and
bot handle, `@eatelephant`. Renaming everything: package `src/disputatio/` -> `src/elephant/`,
project name in `pyproject.toml` -> `eat-the-elephant`, CLI entrypoints `disputatio-bot/-cron/-serve`
-> `elephant-bot/-cron/-serve`, test files, docs, `railway.toml`.

One name deliberately survives: `@disputatio_bot` is the actual Telegram handle of the dev/test
bot — Telegram bot usernames are effectively fixed once created, so it stays as the literal,
documented identity of the test bot in `docs/architecture.md`. The real production bot is
`@eatelephant`, not yet deployed.

Also renamed the live Postgres tables (`disputatio_users/topics/seen/digests/feedback` ->
`elephant_*`) via a new migration `007_rename_tables.sql` — pure `ALTER TABLE ... RENAME`,
no data touched. Per the no-edit-applied-migrations rule, migrations 001-006 were left as-is
(they're historically accurate: that's what actually ran); 007 bridges old names to new.
Index names created by those old migrations still carry the `disputatio_*` prefix internally
(Postgres doesn't rename indexes when you rename a table) — cosmetic, invisible anywhere in
code or docs, not worth the extra renames.

## 2026-06-20 17:30 — Added per-side outlet research + omission signal for source balance

Real-world test: a Russia-Ukraine topic on the production bot surfaced zero Russian-side
sources despite `source_guidance` explicitly naming TASS/RIA/Sputnik. Root cause, found by
direct experimentation against the live research() service: Perplexity's retrieval is
driven by the literal subject of a query, not by a "prioritise X" instruction buried in a
longer prompt — naming an outlet as the query's subject sometimes works, naming it in a
guidance list embedded in a broader query basically never does. Confirmed via repeated
calls that even subject-anchored named-outlet queries are genuinely probabilistic (~50-60%
hit rate in testing), not deterministic — this is a real limitation of the search backend,
not something prompt tuning fixes.

Decided against two things during the investigation:
- A country-specific "prioritise X" example in `_source_profiler`'s prompt — would bias
  toward conflicts we thought to list and fail to generalize. Replaced with a general
  principle (identify every conflicting party, name leaning outlets per party) — verified
  it generalizes correctly across Russia-Ukraine, Venezuela (government vs. opposition),
  and AI regulation (regulator vs. industry) without any hardcoded country list.
- Announcing "no Russian coverage found" in the digest when a side comes up empty — would
  mean confessing search failures conversationally. Instead: research tries (per-side
  focused queries, same mechanism as tracked sources), and if a side's coverage genuinely
  doesn't show up, the digest just has less to say about it. The *signal* that matters is
  the propaganda module's new "omits a clearly relevant other side" check — it flags a
  source for ignoring another party's position, which is actionable, rather than the bot
  narrating its own research gaps.

Architecture: new `identify_sides()` agent (research.py, "balanced" tier — "fast" tier was
empirically too weak for this judgment call, tested side by side) returns `list[Side]`
(name + outlets), empty for topics with no inherent sides. Generated once per topic
(creation or description edit), stored as `Topic.sides_json` (migration 008). `gather()`
fires one focused per-outlet query for up to 2 outlets per side, same `_query_source`
mechanism as user-tracked sources. `propaganda.analyze()` takes the sides list and checks
each source's coverage for ignoring a relevant other party — verified case manually: a
Kyiv Independent-style summary got flagged for omitting Russian statements, a TASS-style
summary got flagged for omitting Ukrainian claims. Symmetric, as the policy requires.

Found and fixed two real bugs while building this, surfaced by the first end-to-end test:
1. `_query_source()` (used for all per-outlet queries, not just the new side ones) was
   blindly labeling every citation with the outlet name it asked about, regardless of
   which domain the citation actually came from — querying "RT" returned mostly unrelated
   domains (understandingwar.org, youtube.com, nytimes.com...) all mislabeled "RT". Fixed
   by always deriving `Article.source` from the real URL domain, matching how the general
   query already worked. This was a pre-existing bug affecting tracked sources too, not
   something introduced by this feature — just much more consequential once the feature
   specifically targets contested-narrative outlets.
2. `_query_source()` had no domain blocklist at all (by original design — "if you typed a
   source, you want it regardless"), so YouTube/social links leaked through for per-outlet
   queries. That's a defensible choice for sources the *user* typed in, but auto-suggested
   side outlets were never chosen by the user, so they now get the same blocklist the
   general query already has.

## 2026-06-20 22:10 — Fixed the freshness filter being a silent no-op
User feedback: a digest meant to cover June 18-20 included a piece from May 29. Traced
it to `dedup.py`'s date filter (`a.published_at is None or a.published_at >= cutoff`):
Perplexity citations via OpenRouter only ever return `url` + `title`, never a date, so
every `Article.published_at` was always `None` and the `is None` clause let everything
through unconditionally. Verified live — even with `search_recency_filter="week"` set,
OpenRouter/Perplexity still cited a 2024 piece and several undated aggregator pages for
a "what happened this week" query. The lookback filter existed and was unit-tested, but
had never actually filtered anything in production.

Considered calling Perplexity's native API directly (it does return `search_results[].date`,
OpenRouter just drops it) — rejected for now: no extra paid credential, stay on what we
already have. Went with recovering the date ourselves instead, for free: most news URLs
embed the date in the path (`/2026/06/19/...`), and failing that, one short GET reads
standard SEO metadata every major CMS already emits (`article:published_time`, JSON-LD
`datePublished`, `<time datetime>`). New module `elephant/article_dates.py`, wired in at
the end of `research.gather()` so `dedup.py` needed zero changes — it was already correct,
just starved of real data. Confirmed end-to-end against a real article that had leaked
into an earlier test digest: it resolved to its true date (3 months old) and would now
be dropped by the existing lookback filter. Fails open (stays `None`, today's behavior)
for the minority of pages with no date signal — mostly section front pages, not articles.

## 2026-06-20 23:10 — Generalized scheduling from one frequency+time to independent slots
User feedback: picking "twice daily" only ever asked for one time, then silently
assumed the second send was exactly 12h later — typing "10:00 and 19:00" just got
parsed down to 10:00 and the 19:00 part was discarded. Worse: that 12h assumption
was baked into `jobs.is_due()`, so even fixing the input parsing wouldn't have let
users pick arbitrary gaps. Discussed it with the user — rather than bolt on a
second send_hour column, generalized the whole model: a topic now has a list of
independent `Slot`s (days-or-every-day, time, every_n_weeks), each tracking its
own `last_sent_at` in a new `elephant_topic_slots` table. Migration backfills
every existing topic's single frequency+time into the equivalent slot(s) — old
behavior preserved exactly, no manual fixes needed.

This subsumes daily/twice_daily/weekdays/mwf/tuth/custom_days/weekly/biweekly into
one mechanism (every_n_weeks=0 means "fire every matching day", >=1 means "once
every N weeks") and, as a side effect, finally supports the two concrete cases the
user described: arbitrary-gap twice-daily (two every-day slots, any two times) and
mixed schedules like "Mon 12:00 + Thu 10:00" (two single-day slots). The old
`_LOOKBACK`/`_RECENCY_FILTER` tables keyed by frequency string are gone too —
`jobs._lookback_hours()` derives the right window from whichever slot fired,
walking back to the slot's actual previous occurrence instead of guessing a fixed
gap. bot.py's UI keeps the same preset buttons (Every day, Mon-Fri, Once a week,
...); each preset now just builds one slot, and a new "Add another checkup time?"
prompt loops back to build more.

Old `frequency`/`send_hour`/`send_minute`/`send_dow`/`schedule_days` columns on
elephant_topics are left in place but unused, rather than dropped — no need for a
destructive migration when leaving them inert costs nothing.

## 2026-06-21 00:35 — Stopped digests fabricating "stories" from reference/hub pages
User caught a digest presenting a Britannica "Stonehenge: history, location, and
meaning..." overview as a dated news story (event_date hallucinated as "Jun 20").
Traced it: not from article_dates.py (Britannica 403s our fetch, so published_at
correctly stayed None) — it's perspectives.cluster()'s LLM ignoring its own Rule 3
("only create stories for things that SPECIFICALLY HAPPENED") for generic reference
material. Live-reran research.gather() for the exact topic and found Perplexity's
general query routinely mixes real articles with encyclopedia entries, magazine
issue listings, and category hub pages (AP News' archaeology hub, sci.news's
category page, newsletter front pages) — none of which match the existing
_ROUNDUP_RE (that only catches "Top 10 / weekly digest" phrasing).

Fixed with two layers: (1) research.py now blocks encyclopedia/reference domains
outright (britannica.com, wikipedia.org, ...) for the general query, and adds
_is_hub_page() — a handful of narrow, separately-anchored patterns (bare "X News",
"X Magazine", month/year issue listings, bare "X and Y" category labels, "Title:
history/location/meaning... of Y" overviews) plus a ≤2-word bare-category check.
(2) perspectives.py's clustering prompt gained an explicit instruction to discard
reference material rather than inventing an event/date for it, as a catch-all for
shapes the regex won't generalize to. Verified live: re-ran the full pipeline for
the same topic — regex caught the common shapes, and the LLM correctly discarded
the rest (newsletter front pages, an author-bio page) without fabricating stories,
producing only the 2 real findings that were actually new.
