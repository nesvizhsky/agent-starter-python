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

## 2026-06-21 00:50 — Fixed the no-news fallback hallucinating specifics
User report: "AI" topic said "no confirmed events for June" and listed
aggregators, when neither of those was ever told to it. Traced it to
digest.py's _NO_NEWS_PROMPT — when clustering correctly finds zero real events
(not a bug; the strengthened Stonehenge-fix prompt was working as intended),
the digest LLM call got "there is nothing new, write a brief message" with
*zero* actual context — no article list, no dates, no sources. With nothing
to ground it, the model filled in plausible-sounding specifics it was never
given. Classic hallucination: confident, fluent, and fabricated, with no
signal to the reader that it's a guess. Per docs/failure_modes.md's rule
("a confident wrong answer is worse than 'I'm not sure'"), this needed a
hard fix, not a shrug.

Fix: rewrote the prompt to explicitly say the model has no information about
what was searched, why nothing qualified, or what time period was covered —
and forbid mentioning dates, months, source names/types. Verified across
three live calls: all came back flat and accurate ("No new reportable events
were found..."), no invented specifics. Added a regression test asserting
month names and "aggregator" never appear in the no-news output.

## 2026-06-21 01:05 — Translate contradiction quotes, keep the original tap-to-reveal
User feedback: Russian digests left contradiction quotes (⚡ Source A: "...") in
English on purpose — to avoid distorting the original wording — but that left
non-English-speaking readers unable to read the very quotes the contradiction
is built on. Their suggestion: translate it, but keep the original available.

Telegram's HTML mode supports <tg-spoiler> (tap-to-reveal) — fits this exactly.
digest.py's prompt now asks for the claim translated into the digest language,
with the verbatim original right after inside a spoiler tag, so nothing is lost
and nothing clutters the line. Tested live: a Russian digest now shows the
translated quote with the English original behind a tap; an English digest
quoting an English source shows just the quote once.

The model didn't reliably follow "skip the spoiler if it'd be identical to the
visible text" on its own (an English digest of an English quote still got a
redundant tap-to-reveal of the same text) — added a deterministic post-process
step (_strip_redundant_spoilers) that collapses any spoiler whose hidden text
case-insensitively matches the visible text, rather than trusting the model's
judgment on something checkable in code.

## 2026-06-21 01:15 — Stopped a slow digest from blocking the whole bot
User shared a fix from their own bot: don't await long jobs inline — it blocks
the event loop. Checked our case: the "fetch_status" UI text literally admitted
"Other commands won't respond until it's done" — a known limitation, never
actually fixed. Root cause: python-telegram-bot processes updates one at a time
off its internal queue unless concurrent_updates is enabled, which is exactly
what was happening on the local/dev polling path. (The production webhook path
calls ptb.process_update() directly per FastAPI request rather than going
through PTB's queue, so it was already less affected — but enabling this is
still the documented correct setting and costs nothing.)

Fix: ApplicationBuilder().concurrent_updates(True) in build_application() — PTB's
built-in equivalent of "run it as a background task," backed by a worker pool
(confirmed via app.concurrent_updates == 256) instead of a single serial queue.
PTB's ConversationHandler still locks per-conversation internally, so the
add_topic/schedule flow can't race with itself — only *unrelated* updates (other
users, other commands from the same user) now run concurrently instead of
queueing behind a slow digest. Updated the now-inaccurate "Other commands won't
respond" copy to "feel free to keep using me meanwhile."

## 2026-06-21 09:20 — Found and fixed why scheduled digests silently failed
User report: a digest never arrived, plus a Railway "Deploy Crashed!" notification
for elephant-cron-tick. Checked metrics: p50/p90/p95/p99 latency on the main app
were ALL exactly 30000ms — a flat ceiling, not organic variance, meaning every
request was hitting a hard timeout. Root cause: POST /cron/tick awaited the
*entire* run_due_digests() pipeline inline — every due topic's full research +
LLM pipeline, sequentially, each taking 1-2 min per the bot's own UI text. The
elephant-cron-tick service is just `curl -f .../cron/tick` on an hourly Railway
cron; curl's connection got cut by the proxy timeout long before the real work
finished, curl exited non-zero, and Railway reported the cron container itself
as "crashed" — while the killed request likely cut off whatever digest was
mid-flight, which is why nothing arrived. Manual /check never hit this because
it runs through the bot's own conversation handling, not this HTTP roundtrip.

This was very likely silently dropping most scheduled digests whenever a tick
had real topics due, not a one-off — a single topic's pipeline alone exceeds a
30s window, so almost every non-empty tick should have been timing out.

Fix: /cron/tick now fires run_due_digests() as a background asyncio task and
returns an immediate ack, decoupling actual processing time from the HTTP
request's lifetime entirely. curl gets its 200 in well under a second regardless
of how many topics are due; the real work continues after the response is sent.
Known trade-off: a background task can still get cut short if the process
redeploys mid-run — acceptable for now since it's strictly better than the
previous guaranteed-timeout failure mode, but worth hardening later (e.g. a
shutdown hook that waits for in-flight runs) if it turns out to matter in practice.

## 2026-06-23 17:30 — Built scripts/experiments/ sandbox, then grounded source identification

Wanted a place to compare models/prompts before changing product code — added
`scripts/experiments/` (sibling to `scripts/tests/`), a plain CLI convention (no
server — `uv run python scripts/experiments/<name>.py`), with a shared `_harness.py`
for running candidates and saving timestamped JSON logs to `results/` (gitignored).
First two experiments: `compare_propaganda_models.py` and `compare_research_models.py`.

Propaganda-analysis comparison (claude-sonnet-4.6 vs opus-4.8, deepseek-r1, grok-4.3)
across 4 test stories (3 biased + 1 neutral control): Sonnet stayed the most thorough
analyst — no reason to swap `propaganda.py`'s model. Notably, Qwen's thinking-mode
models reject pydantic-ai's forced structured-output tool call (400 error) — not
usable for this pipeline's output_type pattern.

Bigger finding, on source identification: `identify_sides()` and
`generate_source_guidance()` were both a single ungrounded LLM call asking the model
to *name* outlets from memory — a real hallucination risk, since a plausible-sounding
outlet name isn't necessarily real. Tested grounded alternatives (Perplexity Sonar
tiers, plus Grok/Gemini/DeepSeek/GPT-5.1 via OpenRouter's `:online` plugin) — plain
`perplexity/sonar` won on cost, reliability, *and* specificity simultaneously. Pricier
tiers and other providers' `:online` mode added 4-7x cost without naming more accurate
outlets, and `gemini-3.1-flash-lite:online` silently dropped citations on one query —
same "looks fine until you check" failure mode that ruled out `sonar-pro-search`.

Rewired both functions to a two-step pattern: ground in `_research()` (sonar) first,
then a cheap `fast`-tier pass extracts structured output from that real prose — never
inventing beyond what the search actually returned. Also dropped `_MAX_OUTLETS_PER_SIDE`
4→3, and the extractor now explicitly excludes wire services (BBC, Reuters, etc.) from
being attributed to any one side — those stay in the general catch-all query only, per
the original design intent that was already correct but undocumented.

Caught one flakiness during integration testing: the extraction step occasionally
returned an empty side list even when the grounded research clearly named multiple
sides (confirmed by rerunning the same input 3x — always succeeded). Added a one-retry
safety net on empty extraction in `identify_sides()`, justified by the fact this only
runs once per topic — cheap insurance against silently leaving a real conflict topic
side-less for its whole lifetime.

## 2026-06-23 21:10 — Built fetch.py: direct sitemap/RSS retrieval, before falling back to Perplexity

Continued the news-gathering improvement thread. Probed real outlets first
(`scripts/experiments/probe_feed_discovery.py`, then manual curl) before designing
anything: BBC, Reuters, and TASS — the exact outlets `research.py` already documents
as Perplexity's weak spot — all killed or hid RSS years ago, but all three still
expose a sitemap via `robots.txt`. Sitemaps, not RSS, are the real backbone for
"what did this specific outlet just publish."

Built `fetch.py`: resolves an outlet name to a domain (LLM guess + live HTTP
verification, cached globally in new `elephant_outlet_domains` table since the mapping
never depends on which topic asks), then tries the Google News Sitemap standard, then
RSS/Atom at a common path. A cheap fast-tier LLM call classifies which of the
resulting headlines are actually about the topic, since a sitemap mixes
sport/weather/politics indiscriminately. Wired into `research.py`'s `_query_source()`
as the first attempt — Perplexity's recall-based query only runs when neither method
finds a usable feed at all for that outlet.

Two real bugs caught by testing against live outlets, not assumptions:
1. BBC's robots.txt lists "archive" before "news" in its sitemap list — walking
   robots.txt order first pulled mostly-stale archive entries. Fixed by sorting
   discovered sitemap URLs to prefer ones with "news" in the path, at both the
   top-level and the leaf level inside a sitemap index.
2. TASS's sitemap uses the older Sitemaps 0.91 protocol — no `news:title` field at
   all, just `loc`/`lastmod`. Headlines fell back to the bare numeric URL ID
   ("2149717"), which made the relevance classifier unable to discriminate (it
   accepted ~100% of candidates when fed meaningless IDs — confirmed via a quick
   isolated test that the same classifier correctly filters 2/4 when given real
   headlines, so the bug was garbage input, not a broken classifier). Added
   `_recover_titles()`: a bounded page-fetch pass (same `<title>`/`og:title` regex
   scan as `article_dates.py` already uses for dates) for any candidate whose
   headline is the URL fallback, before relevance filtering runs.

Also hit a classic ElementTree gotcha while writing the Atom parser: `_child(entry,
"updated") or _child(entry, "published")` is wrong — an Element with no child
elements (just text) is falsy under ElementTree's truthiness rules even when found,
so `or` silently skipped a present `<updated>` tag. Caught by an offline test, not
by reading the code — worth remembering for any future XML work in this codebase.

Used `defusedxml` instead of stdlib `ElementTree` for all parsing here, since this
is the one module that parses XML from arbitrary third-party domains (XXE risk).

Known limitations, accepted rather than chased further right now: leaf-sitemap
walking is capped at `_MAX_SITEMAP_LEAVES` and not perfectly ordered by article
recency (only by when the leaf file was last regenerated), so very high-volume
outlets may miss some in-window articles; the relevance classifier is cheap and
misfired once on a foreign-language headline batch during testing (one Serbian
Iran-talks article leaked into football-topic results). Outlets with neither a
sitemap/RSS feed nor reliable Perplexity coverage (e.g. `rt.com`) still have no
good option — that's `source_audit.py`/vetting territory, not yet built.

## 2026-06-23 21:40 — fetch.py: added excerpt fetching, closing the "read inside" gap

User framed the target behavior precisely: a human researcher scrolling TASS's
feed doesn't just read headlines — for the ones that look relevant, they open
the article and read it. fetch.py's first version stopped at the headline;
articles it returned had `context=None`, much thinner than the old
Perplexity-based path (which always returns a paragraph of real prose, since
Perplexity already "read" the page to answer the query). `perspectives.py` and
`propaganda.py` both lean on that context/summary text, so this was a real gap,
not a cosmetic one.

Added `_fetch_excerpts()`: after relevance filtering narrows candidates down to
a handful, fetch each article's page once and pull its meta description
(`og:description` / `name="description"` / `twitter:description` — near-
universal on real news sites for SEO/social-sharing). Deliberately scoped to
the post-filter set, not all candidates, so cost stays bounded regardless of
how many headlines the outlet published. Confirmed live against TASS: excerpts
came back as real prose ("According to Moscow Mayor Sergey Sobyanin, 80 drones
heading for Moscow have been downed since midnight"), not just headlines.

Extracted `_extract_excerpt(html) -> str | None` as a pure function (matching
the rest of the module's pattern of separating parsing from the network call)
so the regex logic has offline test coverage instead of only being exercised
by a live HTTP integration test.

## 2026-06-23 22:05 — ria.ru / rbc.ru fetch.py failures: two different causes, neither a code bug

While running fetch.py against the real "Russia-Ukraine" topic (риа новости,
рбк tracked as sources), both fell back to Perplexity instead of direct fetch.
Investigated rather than assumed:

- **ria.ru**: resolves to 127.0.0.1 via this machine's local DNS — confirmed
  it's NOT a global block by querying Cloudflare's (1.1.1.1) and Google's
  (8.8.8.8) public DNS-over-HTTPS directly, both returned a real IP
  (194.190.139.47), and `curl --resolve` to that IP got a clean 200. This is
  sanctions-related DNS filtering specific to this network/ISP, not a problem
  with the site or with `resolve_domain()`. Decision: don't build around it —
  verify once deployed to Railway, where this filtering almost certainly
  doesn't apply, rather than adding complexity (e.g. forcing a public DNS
  resolver in httpx) for a problem that's local-machine-specific.
- **rbc.ru**: a real anti-bot wall — 307-redirects to a UUID-suffixed
  challenge URL that returns 401, and a full real-browser User-Agent string
  didn't help either, so it's deeper than a UA check (likely JS-challenge or
  IP-reputation based). Decision: don't try to evade this — that's the site
  deliberately blocking automated access, a different thing from "couldn't
  reach a real public feed." Left it to the existing Perplexity fallback,
  which should cover a mainstream outlet like RBC reasonably (this isn't the
  obscure-state-media case Perplexity specifically struggles with).

## 2026-06-24 15:00 — Eight onboarding fixes from real user feedback

A real user hit friction during /add_topic; the user (Ana) relayed 8 concrete
complaints rather than one vague "onboarding is bad." Fixed all eight:

1. **Technical source_guidance leak**: the "Prioritise: TASS, RIA Novosti...
   Do not cite: YouTube..." research-prompt text was shown verbatim on the
   name-confirmation screen — internal LLM-prompt material, not user-facing.
   Removed it from both places that screen renders (initial + the "back" path).
2. **`/start` ignored language setting**: `_WELCOME` was a bare English
   constant, never run through `_t()`/`_lang()`. Added "welcome" +
   button-label keys to `_UI`, converted `_START_KEYBOARD` to a
   `_start_keyboard(lang)` function. Also caught and fixed `on_start_nav`,
   which had the same bug for its own strings.
3. **Confirmation easy to miss**: a user didn't notice their topic saved and
   thought it failed — the final message was just another small "✓ ..." line,
   visually identical to every per-step confirmation earlier in the same
   conversation (✓ name, ✓ schedule, ✓ timezone...). Replaced with a
   distinct 🎉-headed box.
4. **No manual-only option**: added "🔕 No schedule — check manually" to the
   schedule-type step (first slot only — doesn't make sense as an *additional*
   slot). Jumps straight past days/time to wherever the normal flow lands
   next. due_slots() already handles an empty slot list correctly (nothing to
   iterate → never fires), so no jobs.py change needed.
5. **Timezone was per-topic**: confusing — changing it only affected one
   topic. Moved to profile-level: new `elephant_users.timezone` column
   (migration 011, backfilled from each user's most recent non-UTC topic),
   `store.get_user_timezone()`/`set_user_timezone()`. jobs.py still reads
   `topic.timezone` directly for scheduling, so `set_user_timezone()` fans the
   write out to every topic instead of touching jobs.py — smaller, lower-risk
   change than threading a user-timezone lookup through the scheduler.
   Topic creation now silently applies the profile timezone (no question
   asked); `/timezone` sets it for everything at once instead of picking a
   topic first.
6. **Only 12 timezones shown**: expanded to 26, covering every populated
   whole-hour offset plus the common half/quarter-hour ones (India, Tehran,
   Adelaide), not a sparse sample.
7. **Garbled timezone button labels** ("utc...gapore"): the old layout was 3
   long, double-spaced labels per row — too wide for some clients. Single
   space, shorter city names, 2 per row.
8. **No quick check after creating a topic**: realized the existing
   `ta:check:{topic_id}` callback (already used by the topic-picker's /check
   flow) could be reused directly — just added a button with that same
   callback_data to the new-topic confirmation message. No new handler needed.

Removed dead code along the way: the entire per-topic `_ASK_TZ` conversation
state, `_ask_tz`/`_got_tz`/`_back_from_tz`, the per-topic `set_tz` action in
`on_topic_action`, and `on_tz_set` — all replaced by the profile-level path.

## 2026-06-24 15:30 — Testing in the dev bot surfaced the real onboarding bug

Live-testing item #2 from the user-feedback list in the dev bot immediately
showed the fix was incomplete: /start was localized, but every other message
in the /add_topic conversation ("What do you want to track?", schedule
prompts, timezone prompts, sources prompt, the final confirmation...) was
still hardcoded English, completely bypassing `_t()`/`_lang()`. The user's
original complaint was specific to /start; testing revealed the entire
onboarding flow had the same bug.

Localized the whole /add_topic + /schedule conversation: ~33 new `_UI` keys,
threading `lang` through every handler in the chain (most already had
`update`/`context` in scope, so this meant fetching `tg.id` and calling
`_lang()` at the top of each one). Also found and fixed two day-name
constants (`_DOW_SHORT`, `_DOW_LABELS`) that duplicated existing translated
`_UI` keys (`dows_0..6`, `dow_0..6`) without ever using them — removed the
dead duplicates, wired the day-picker keyboards to the real translation.

Separately, the user reported that even the parts that *were* translatable
broke down differently: typing a Russian topic description got the LLM-
generated name and expanded description translated back to English. Root
cause: `_name_agent` (bot.py) and `_expander` (research.py) both had system
prompts with English-only few-shot examples and no instruction to preserve
input language — the model defaulted to following the examples' language
rather than the user's. Added an explicit "respond in the same language the
user wrote in" instruction to both; verified with a Russian input that both
the generated name and expanded description now stay in Russian.

Also fixed a real bug surfaced by a live monitor on the dev bot's logs while
testing: `telegram.error.BadRequest: Message is not modified` was an
unhandled exception on every one of the 40 `query.edit_message_text()` call
sites whenever Telegram delivered a duplicate/retried update for an edit
that would produce byte-identical content (e.g. a double-tap). Added
`_safe_edit_text()` — catches specifically that error message text (not a
blanket `except BadRequest`, since that would also hide real bugs like
unescaped-Markdown parse errors) and bulk-replaced all 40 call sites.

## 2026-06-24 15:55 — Two more real bugs caught live-testing in the dev bot

User reported `/add_topic` "got stuck" — sent a Russian description, no reply
at all, no error logged. Reproduced the exact same LLM/research calls
standalone and they completed in ~12s, so the backend itself wasn't the
problem. Restarted the dev bot process and had the user retry the identical
input — it worked the second time (topic created, digest pipeline started).
Read as a one-off hang in that specific long-running process (possibly a
stuck connection in the cached OpenRouter client) rather than a code bug —
noted, not chased further since it didn't reproduce after restart.

The restart did surface a second, real, reproducible bug though: clicking
"✖ {source}" or "🚫 {source}" on a topic's sources view crashed with
`ValueError: badly formed hexadecimal UUID string`. Root cause: those two
buttons encode 4 colon-separated parts (`tp:rm_src:{topic_id}:{index}`), but
`on_topic_panel`'s top-level parser splits on only the first 2 colons,
leaving `topic_id_str = "{topic_id}:{index}"` — not a valid UUID — and
`UUID(topic_id_str)` blew up before the function ever reached the rm_src/
rm_blk branches further down (which already correctly re-derive the index
from the raw `query.data` themselves). Fixed by stripping anything after the
first colon from `topic_id_str` right after the initial split.

Also fixed a real (if separate) gap: PTB logged "No JobQueue set up... 
Ignoring conversation_timeout" on every startup — the `[job-queue]` extra
was never installed, so abandoned /add_topic conversations were never
cleaned up after the configured 10-minute timeout. Added
`python-telegram-bot[job-queue]` to pyproject.toml.

## 2026-06-24 16:20 — Global error handler + "still running" reassurance

User reported a batch of 6 issues from real production use, including: ran
3 /check requests back to back, the last 2 delivered results, the first
"never did". Checked production logs directly (`railway logs`) rather than
guessing — the first one actually DID send, just ~7 minutes after starting
(vs ~20-40s for the other two, which had little/no new content). Not a
hang or a bug — that topic genuinely has far more sources/articles to run
through perspective-clustering + propaganda-analysis. The real problem is
UX: nothing told the user it was still working, so 7 minutes of silence
reads identically to "broken".

Production logs also confirmed both bugs already fixed in dev
(`_safe_edit_text`, the rm_src/rm_blk UUID parse) are still live in
production and firing right now — motivates deploying soon.

Two fixes:
1. `app.add_error_handler()` — PTB had none registered at all (every prior
   "No error handlers are registered, logging exception" log line was this
   gap). Without it, any exception that escapes a handler's own try/except
   vanishes with the user seeing nothing. Now logs the real traceback via
   loguru AND tells the user something went wrong, in their language.
2. `_run_digest_with_progress()` — wraps the existing `jobs._run_digest()`
   call; if it's still running after 60s, sends a "still working, hang
   tight" message before continuing to await it. Wired into all 3 call
   sites (cmd_check, the topic-picker check, the topic-panel check).

## 2026-06-24 16:40 — YouTube leaked through despite an existing blocklist

User: "youtube sources keep appearing." `_BLOCKED_GENERAL_DOMAINS` already
existed and already included youtube.com — but research.py's own module
docstring documents a deliberate policy: "User-configured sources bypass
all domain filtering — if you added a source, the bot queries it
regardless." The bypass was meant to mean "don't apply our quality bar to
an outlet you explicitly picked," but the implementation applied it to
*any* domain Perplexity happened to cite while researching that source —
including an embedded YouTube clip cited alongside the real outlet, which
the user obviously never asked for.

Split the blocklist into two tiers: `_ALWAYS_BLOCKED_DOMAINS` (pure social/
video platforms — YouTube, Instagram, TikTok, X, Facebook, Reddit) now
applies unconditionally in `_parse()`, even for user-tracked source
queries; `_BLOCKED_GENERAL_DOMAINS` (the quality-journalism bar — marketing
blogs, listicle aggregators, encyclopedias) still only applies to
general/auto-suggested-side-outlet queries, preserving the original intent
of the bypass. Verified with a synthetic BBC+YouTube citation pair and
added a regression test.

## 2026-06-24 16:45 — Multi-source input got jammed into one unsplit blob

User pasted several sources at once during /add_topic and the sources view
later showed one button with everything concatenated:
"ria.rutass.rurbc.rukommersant.ru...". `_got_sources_text` only split on
commas (`raw.split(",")`); the user evidently typed one source per line
(no commas) — Telegram preserves newlines in message text, but a rendered
InlineKeyboardButton label silently swallows them, so the unsplit blob's
embedded `\n` characters just vanished visually, jamming everything
together with no visible separator at all. Explains the exact symptom.

Added `_split_sources()`: splits on comma, semicolon, OR newline — not
plain whitespace, since some outlets are legitimately multi-word names
("Al Jazeera", "The Guardian"). Created `scripts/tests/test_elephant_bot.py`
(bot.py had no offline tests at all before this) with the newline case as
the primary regression test.

## 2026-06-24 17:05 — Full localization audit: every command, not just /add_topic

User: "language often appears english while doing different commands." Did a
systematic pass over the entire file — every command handler, every topic-card
button, every inline awaiting_* text flow, every confirmation message. Found
and fixed:

- `cmd_topics` called `store.get_user_language()` directly instead of
  `_lang()`, skipping `_ensure_ui()` — any `_t()` call in that handler would
  silently fall back to English if the cache wasn't already warmed by an
  earlier command in the session.
- `_topic_card`/`_topic_card_expanded` button labels (Check now, Edit, Pause/
  Resume, Rename, Query, Sources, Reset, Close) were all hardcoded English,
  even though both functions already accepted a `lang` parameter — it just
  wasn't being used for the buttons, only the card text.
- `_show_sources_view` took no `lang` parameter at all.
- `_topic_picker` took a raw English `prompt` string from each of its 6 call
  sites (pause/resume/delete/reset/check) instead of a `_UI` key.
- `cmd_pause`, `cmd_resume`, `cmd_rename`, `cmd_describe`, `cmd_add_source`,
  `cmd_del_source`, `on_topic_panel`'s rename/describe/add_src/add_blk/delete
  branches, `on_topic_delete_confirm`, and every `awaiting_*` text flow in
  `on_text` — all had hardcoded English confirmation/prompt messages.
- `on_topic_panel`'s `del_cancel` branch called `_topic_card_expanded(topic)`
  with no `lang` arg at all (silently defaulting to English).

Added 45 new `_UI` keys (118 total, up from 73). Verified all translate
correctly to Russian with placeholders intact via a direct `_ensure_ui()`
check before restarting the dev bot.

## 2026-06-24 17:20 — Refined the YouTube block: allow specific tracked channels

User pushed back on the earlier "always block social/video platforms"
fix: a user should be able to track a *specific* YouTube channel or
Instagram account as a source — the block should only stop an unrelated
platform citation Perplexity tacks onto a *different* source's query, and
bare platform domains ("youtube.com" with no channel) shouldn't be
addable as a source at all, since there's no such thing as "all of
YouTube" as one trackable source.

research.py: `_parse()` gained `allow_domain` — `_ALWAYS_BLOCKED_DOMAINS`
still blocks unconditionally *unless* the cited domain matches
`allow_domain`, which `_query_source()` only computes (via new
`_own_domain()`) when `is_user_tracked=True` — set in `gather()` only for
the user's own `active` sources list, never for auto-suggested side
outlets or the general query. So tracking "youtube.com/c/SomeChannel"
works, but an unrelated YouTube citation while researching "BBC" still
gets blocked, and side outlets/general search can never bypass the block
at all.

bot.py: added `_is_bare_platform_domain()` + `_filter_valid_sources()`,
wired into all 3 places sources get added (the /add_topic sources step,
`/add_source`, and the inline "add source" flow from the sources view).
Caught a second real bug while doing this: the inline flow's domain
parser did `.split("/")[0]`, silently collapsing any channel path down to
the bare domain before the new validation could even see it — fixed to
keep the path intact.

Caught a bug in my own `_own_domain()` via its own test: it checked
"does this look like a domain" using the string BEFORE stripping the
`https://` scheme, so `https://www.instagram.com/someaccount` split on
"/" gave "https:" as the first segment (no dot) and wrongly returned
None. Fixed by stripping the scheme first.

## 2026-06-24 17:40 — Multi-source fix missed a second, different code path

User: added sources to an *existing* topic ("Влияние войны") via the sources
view's "➕ Always check" button, and they still showed up jammed together.
The earlier fix only patched `_got_sources_text` (the /add_topic *creation*
flow) — the inline "add source to an existing topic" handler in `on_text`
is a completely separate code path that never called `_split_sources()` at
all; it treated the entire message as one single domain string, so multiple
sources pasted at once became one jammed entry. Same root cause, different
handler — I'd only checked the creation flow, not this one.

Fixed both inline flows ("add source" and "add ignored source") to split on
the same comma/semicolon/newline separators and add all of them at once.
The "ignored" side deliberately skips the bare-platform-domain rejection —
excluding all of youtube.com site-wide is a legitimate use case, unlike
trying to *track* "all of YouTube" as one source.

Also found and repaired the actual corrupted data this had already produced
in the dev DB: topic "Влияние войны" had one source entry equal to
`"ria.ru\ntass.ru\nrt.ru\nkommersant.ru\nrbc.ru\nmeduza.io"` — ran
`_split_sources()` against it directly and wrote the corrected 6-source list
back, verified in the DB before restarting the bot.

## 2026-06-24 18:15 — Found why "language doesn't change": translation truncation, permanently cached

User tested in production after deploy: language was still showing English
everywhere despite the user's profile correctly set to "Russian" in the DB
(checked directly — not a data bug). Checked production logs for the
specific "UI translation failed" warning `_ensure_ui()` already logs on
failure: `"UI translation failed for Russian (Unterminated string starting
at: line 1 column 5442 (char 5441))"`. Root cause: `_UI` had grown to 119
keys across this session's localization work — too much for one LLM call's
output, so the response got cut off mid-JSON, `json.loads()` failed, and the
existing `except` block fell back to `_ui_cache[lang] = _UI` (the English
dict) — **permanently**, for the rest of that process's lifetime, since
`_ensure_ui()` short-circuits on `if lang in _ui_cache: return` with no
expiry and no retry. One transient truncation = stuck in English forever
(until next deploy/restart).

Fixed by batching: split `_UI` into chunks of `_UI_BATCH_SIZE = 20` keys,
translate each batch with its own LLM call (run concurrently via
`asyncio.gather`), one retry per batch on failure, and only fall back to
English per-key for whichever specific keys still failed after the retry —
never the whole language. Verified 3x against the live 119-key dict: 0
keys fell back each time (one run even hit a transient batch-level
truncation on attempt 1, and the retry caught it cleanly).

Also addressed a second report from the same test session: tapped
"Continue" during /add_topic while a digest was running concurrently, and
got no response for ~2 minutes — wanted some acknowledgment that the tap
registered. Checked for the most common cause (a blocking synchronous call
that would stall the whole event loop) — found none; the DB pool and all
LLM/HTTP calls are properly async, so I couldn't conclusively root-cause
the delay itself. Added `_with_quick_step_reminder()` (same pattern as the
digest progress message, just an 8s threshold instead of 60s, since this
navigation step normally completes instantly) to the "Continue" step
specifically, so a recurrence at least produces a visible "still working"
message instead of silence.

## 2026-06-24 18:25 — Found the real cause of a "duplicate story" report

User reported a digest containing two near-identical "📌" entries — both
about satellite images of damage to a Voronezh plant, both attributed to
Meduza, same date, just worded slightly differently. Traced the actual
pipeline: `dedup.filter_seen()` only compares each new article against the
*historical* seen corpus (`store.max_similarity`) — it never compares
articles against each other within the same batch. So two different URLs
(or even the same article re-summarized) about the same event both pass
dedup as "fresh", and from there it's entirely up to one clustering LLM
call (`perspectives.cluster()`) to recognize they're the same event and
merge them. The clustering prompt already says to merge same-event
articles regardless of framing, but it's a single judgment call with no
programmatic backup — and it occasionally misses when two articles from
the *same* source are worded just differently enough.

Added `_merge_near_duplicate_stories()`: after clustering, embed each
story's headline+first-source-summary and merge any pair whose cosine
similarity is ≥0.92 (deliberately higher than dedup's 0.85 seen-article
threshold — merging two stories is more consequential than dropping one
already-seen article, so this stays conservative). Split the comparison
logic into a pure `_merge_stories_by_similarity()` so it's testable
offline with synthetic embeddings instead of needing a live embed() call
for every test case. Fails open (keeps stories unmerged) if the embed
call itself errors. Verified the existing real-LLM integration tests
(distinct articles stay separate, same-event-different-source still
merges) still pass with this added.

## 2026-06-24 18:45 — Manual checks with nothing new sent no response at all

User: launched a manual check, saw the "searching" status, then it just
disappeared with no digest, no error, nothing. Checked production logs for
the exact run — it worked correctly (gathered articles, dedup correctly
found 0 fresh after the semantic filter) and logged "nothing new ... —
skipping" — then `_run_digest` just `return`ed. No message was ever sent.
The caller deletes the "fetching" status message and refreshes the topic
card, but neither of those is an explicit "we checked, there's nothing new"
notice, so a correct "nothing to report" outcome was indistinguishable from
a silent failure.

There's already a `_NO_NEWS_PROMPT` in digest.py that generates exactly the
right sentence — but it's only reachable from `digest.generate(stories=[])`,
called after clustering, and `_run_digest` returns *before* ever reaching
clustering when `dedup.filter_seen()` (or `research.gather()`) comes back
empty. Fixed: on that early-return path, if `slot is None` (a manual
/check or "Check now" tap, not a scheduled cron tick), generate and send
the no-news message anyway. Deliberately scoped to manual checks only —
notifying on every scheduled tick that finds nothing new would be spammy;
a user who explicitly asked for a check right now deserves an answer
either way.

Verified live against the exact production topic that triggered the
report ("Prehistoric Beliefs" / "Доисторические верования"): first run
found one real fresh article and sent a normal digest; running it again
immediately (now correctly deduped) sent "По этой теме новых событий для
репортажа не обнаружено." — confirmed via a stub Bot that only prints,
not the real Telegram API, so no live message was sent to the user.

## 2026-06-24 19:20 — Found the actual cause of the "Check" tap delay

User asked directly: why is there a delay between tapping "Check" and
seeing the running notification? Checked production logs for timing
evidence rather than guessing: `_ensure_ui()` (UI translation) runs as the
very first await in `on_topic_panel`'s "check" branch — `_lang()` is
called before the "fetching" status message can even be built, since that
message's text itself goes through `_t()`. If the in-memory `_ui_cache`
is cold (every deploy/restart clears it — and I've deployed many times
today), the FIRST command after that restart pays for the full batched
LLM translation (~3-4s) before the user sees anything at all. Production
logs showed exactly this: `_ensure_ui` → "UI translated to Russian" →
digest pipeline starting 3 seconds later, all within one request.

Fixed at the root: added `store.get_distinct_languages()` and warm the
cache for every language any existing user has set in `_post_init()`,
which runs once at startup before the bot serves any traffic. Verified
against the real production DB: warms both stored languages (English —
already pre-cached at module load, no LLM call needed — and Russian) in
~3.5s total, entirely during deploy, never during a user's request.
Confirmed the same in dev: the "UI translated to Russian" log line now
appears right after "starting elephant bot", before any command is
possible.

## 2026-06-24 19:28 — Russian-language input biased source-finding toward Russian outlets

User: created a new "general" archaeology topic, but the digest's sources
skewed Russian (TSN.ua, Izvestia). Checked the topic's stored guidance
directly: "Prioritise: Archaeolog.ru, New-Science.ru, Historyrussia.org,
Scanos.ru..." — a real side effect of an earlier fix from this session.
Making `expand_query()`/`_name_agent` preserve the user's input language
(so a Russian description displays in Russian, which the user explicitly
wanted) means the topic's *stored* `description` is in Russian — and
`generate_source_guidance()`'s grounding `research()` call uses that same
description as its search query. Verified directly: querying Perplexity
with the Russian-language topic name/description produced a response
literally headed "Top Russian Outlets" — Perplexity's search naturally
follows the query's language, regardless of the prompt's English
instructions about what to prioritise.

Fixed by explicitly telling the grounding prompt that the topic's
language is not a signal about which country/region it's about, and to
treat it as general/international unless the topic is *itself*,
substantively, about one specific place. Verified directly against the
exact Russian input that triggered the report — 2 runs both led with
genuine international specialists (Nature, Science, Antiquity, BBC),
with the Russian institute correctly demoted to "also include" rather
than dominating. Applied the same instruction to `_sides_research_prompt`
for consistency, since the same bias risk applies there.

## 2026-06-24 19:45 — Same bias, deeper path: the actual news-search query, not just guidance

User retested the same archaeology topic in production — still mostly
Russian/Ukrainian sources (Ведомости, Известия, ТСН). The earlier fix
only touched `generate_source_guidance()`'s grounding step (what sources
to *recommend*); `gather()`'s actual `_query_general()` call — the real
per-digest news search — separately uses `topic.description` (still
Russian) directly as Perplexity's query text. Verified directly: an
English instruction layered ON TOP of an otherwise-Russian-language query
made things WORSE, not better (Perplexity's search matches on the
literal query text, not meta-instructions about it — confirmed by
comparing 10 result URLs with vs. without the added instruction, both
predominantly Russian/Ukrainian). Translating the query text itself to
English before searching, though, produced genuinely international
results (US News, AP News, Phys.org, Sci.News) — a completely different
mechanism (search engine behavior) from the guidance case (LLM judgment
following an instruction), which is why the same prompt-instruction fix
didn't transfer.

Added `_to_english()`: skips the LLM call entirely for already-ASCII text
(cheap, the common case — most topics are already English), translates
otherwise, falls back to the original text if translation fails. Wired
into `gather()` for the general/catch-all query specifically — NOT for
per-tracked-source queries, since matching a source's own natural
language there is appropriate (querying TASS in Russian makes sense;
querying "what's new in archaeology" in Russian doesn't, for a topic
with no inherent language affinity).

## 2026-06-24 19:50 — Found a real race condition behind "duplicated results"

Same testing session, separate report: the bot sent the same "nothing
new" response 3 times, and earlier sent real digest content repeatedly.
Checked production logs: the user tapped "Check" 5 times within 5 minutes
for one topic, and 3 of those taps sent a FULL digest — each one finding
"8 fresh articles" via dedup despite a near-identical batch having just
been sent moments before. Checked `elephant_seen` directly: the *exact
same* article URL was inserted twice, 40 seconds apart, for the same
topic_id — a real race, not a content-duplication issue (that's
perspectives.py's merge step, already fixed separately).

Root cause: `_run_digest()` reads `store.get_seen_urls()` at the START of
the pipeline but only writes back via `record_seen()` at the very END,
10-40+ seconds later (after gather + cluster + propaganda + digest
generation). Two overlapping runs for the same topic both read the same
"not yet seen" state and both proceed independently — nothing serializes
concurrent checks on one topic.

Fixed with `_run_digest_guarded()`: a per-topic `asyncio.Lock` (module-
level dict keyed by topic.id) that makes a second concurrent check for
the same topic bail out immediately with "already checking this topic"
instead of racing the first. Wired into all 3 call sites that trigger a
check (topic picker, topic panel, `/check` command). Added 2 offline
tests using `monkeypatch` on `jobs._run_digest` to simulate a slow
pipeline and assert the second concurrent call returns False while the
first is in flight, and that sequential calls (lock released) both
succeed normally.
