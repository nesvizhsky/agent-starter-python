# Architecture

> Stage 5: Break it into atomic modules.

## Overview

Same dev/prod split as the inspiration_bot pattern:

| | **development** | **production** |
|---|---|---|
| Bot token | `@disputatio_bot` (test bot, name fixed at creation) | `@eatelephant` |
| Database | dev Neon branch | prod Neon branch |
| Updates | long polling (`elephant-bot`) | webhook into FastAPI (`elephant-serve`) |
| Cron | `elephant-cron` run by hand | Railway Cron (hourly) → `POST /cron/tick` |

## Source layout

```
src/
├── agent/            # shared services (unchanged): llm, media, storage, db, config
└── elephant/       # this project
    ├── store.py          # all DB access, every query scoped by telegram_id via topic FK
    ├── research.py       # gather articles: research() calls per source + topic
    ├── dedup.py          # freshness filter: URL match + semantic similarity
    ├── perspectives.py   # cluster articles into stories (one event, N source views)
    ├── propaganda.py     # rhetoric analysis agent — signals, never verdicts
    ├── digest.py         # pydantic-ai agent: write the digest
    ├── synthesis.py      # weekly synthesis agent
    ├── bot.py            # python-telegram-bot Application + all handlers
    ├── jobs.py           # run_due_digests(), run_due_syntheses()
    └── app.py            # FastAPI: /telegram/webhook + /cron/tick
```

## Data model

```sql
-- 001_init.sql
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per Telegram user who has started the bot.
CREATE TABLE elephant_users (
    telegram_id  BIGINT PRIMARY KEY,
    first_name   TEXT NOT NULL,
    username     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per topic per user.
CREATE TABLE elephant_topics (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id      BIGINT NOT NULL REFERENCES elephant_users ON DELETE CASCADE,
    name             TEXT NOT NULL,
    description      TEXT,
    frequency        TEXT NOT NULL DEFAULT 'daily',   -- 'daily'|'twice_daily'|'weekly'
    send_hour        INT  NOT NULL DEFAULT 8,          -- local hour (0-23)
    timezone         TEXT NOT NULL DEFAULT 'UTC',
    paused           BOOL NOT NULL DEFAULT false,
    sources          TEXT[] NOT NULL DEFAULT '{}',     -- tracked source names/URLs
    excluded_sources TEXT[] NOT NULL DEFAULT '{}',
    trusted_sources  TEXT[] NOT NULL DEFAULT '{}',
    pinned_persona   TEXT,                             -- NULL = random each time
    feedback_notes   TEXT,                             -- running notes from user corrections
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_sent_at     TIMESTAMPTZ,
    last_synthesis_at TIMESTAMPTZ
);

CREATE INDEX ON elephant_topics (telegram_id);

-- Every article sent to a user on a topic. Used for freshness filtering.
CREATE TABLE elephant_seen (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id          UUID NOT NULL REFERENCES elephant_topics ON DELETE CASCADE,
    article_url       TEXT NOT NULL,
    headline          TEXT NOT NULL,
    content_embedding vector(1024),    -- embed(headline + summary), for semantic dedup
    published_at      TIMESTAMPTZ,
    seen_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON elephant_seen (topic_id);
CREATE INDEX ON elephant_seen (topic_id, article_url);

-- Every digest sent, kept for weekly synthesis.
CREATE TABLE elephant_digests (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id    UUID    NOT NULL REFERENCES elephant_topics ON DELETE CASCADE,
    telegram_id BIGINT  NOT NULL,
    content     TEXT    NOT NULL,
    persona     TEXT    NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON elephant_digests (topic_id, created_at DESC);

-- Feedback signals: quick taps and extended corrections.
CREATE TABLE elephant_feedback (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id  BIGINT NOT NULL,
    topic_id     UUID REFERENCES elephant_topics ON DELETE SET NULL,
    digest_id    UUID REFERENCES elephant_digests ON DELETE SET NULL,
    signal       TEXT,   -- 'good'|'too_shallow'|'too_long'|'already_knew'|'wrong_persona'
    note         TEXT,   -- extended free-text correction
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Digest pipeline

Runs once per (user, topic) per cron tick when a topic is due.

```
jobs.run_due_digests(bot)
  └─ for each topic where is_due(topic):

       articles  = await research.gather(topic)
                   # research() call per source in topic.sources (excluded_sources skipped)
                   # + one general call for the topic
                   # returns list[Article(url, headline, source, published_at, summary)]

       fresh     = await dedup.filter(topic.id, articles, lookback_days)
                   # pass 1 — drop URLs already in elephant_seen for this topic
                   # pass 2 — embed remaining; drop if cosine_similarity to any
                   #          stored embedding > 0.85 (pgvector <=> operator)
                   # pass 3 — drop articles older than lookback window
                   # if nothing fresh → send "nothing new" → return

       stories   = await perspectives.cluster(fresh)
                   # LLM groups articles into Story objects (one event, N source views)
                   # model: build_model("fast") — cheap, just grouping

       stories   = await propaganda.analyze(stories)
                   # adds SourceSignals(source, signals: list[str]) to each source view
                   # model: build_model("balanced")
                   # identical criteria applied to all sources

       text      = await digest.generate(stories, topic.feedback_notes, language)
                   # pydantic-ai agent, model: build_model("balanced")
                   # returns DigestOutput(main: str, overflow: str | None)
                   # main ≤ 800 words; overflow stored for /more

       → send: main text + topic card + feedback buttons
       → store.record_digest(...)
       → store.record_seen(topic.id, fresh)    # saves URLs + embeddings
       → store.stamp_sent(topic.id)
```

## Module responsibilities

### `research.py`
Calls `llm.research()` for each tracked source, each auto-identified side outlet, plus
one general query:
```python
research(f'recent news about "{topic.name}" from {source} last 48 hours')
research(f'"{topic.name}" latest developments multiple perspectives')
```
Parses the `Research.sources` list into `list[Article]`. `Article.source` is always the
citation's actual URL domain — never the outlet name we asked about — because Perplexity
often cites unrelated domains even when asked specifically about one outlet; trusting the
query target would mislabel them.

`identify_sides(description, topic_name) -> list[Side]` identifies the distinct
parties/perspectives in a topic (states, government vs. opposition, regulator vs.
industry, etc.), each with a few example outlets — empty for topics with no inherent
sides (archaeology, science). Generated once per topic (creation or description edit),
stored as `Topic.sides_json`. `gather()` fires one focused per-outlet query for up to
2 outlets per side (`_MAX_OUTLETS_PER_SIDE`), same mechanism as tracked sources, with the
general-query domain blocklist applied (auto-suggested outlets weren't user-chosen, so
they get the same quality bar).

**Known limitation:** Perplexity Sonar doesn't index all sources equally — Russian state
media and paywalled outlets may not surface reliably even when a focused per-outlet query
asks for them directly; this is genuinely probabilistic, not a guarantee. Direct-URL
fetching (bypassing Perplexity's index for known outlets) is v2 scope.

### `dedup.py`
Two-pass freshness filter using `store.get_seen_urls()` and `store.get_seen_embeddings()`.
Cosine similarity via pgvector: `SELECT 1 - (embedding <=> $1) AS similarity`.
Lookback window: 48h for daily topics, 7 days for weekly.

### `perspectives.py`
Groups `list[Article]` into `list[Story]` using a fast LLM call. Output:
```python
class SourceView(BaseModel):
    source: str
    url: str
    summary: str
    signals: list[str] = []    # filled in by propaganda.py

class Story(BaseModel):
    headline: str              # neutral event description
    source_views: list[SourceView]
```

### `propaganda.py`
Standalone pydantic-ai agent. Takes one `Story`, returns `list[SourceSignals]`:
```python
class SourceSignals(BaseModel):
    source: str
    signals: list[str]   # e.g. ["loaded vocabulary: 'liberated territories'",
                         #        "passive construction hides agency"]
```
System prompt: apply the same checklist to every source (one-sided framing, loaded
vocabulary, dehumanizing language, false equivalences, appeal to common sense, omission
of a clearly relevant other side). Report signals found, never a verdict.

`analyze(stories, sides=topic.sides)` passes the topic's identified sides into the prompt
so the omission check has a concrete "other side" to check against — without it, the
check still runs but relies on the model's own background knowledge of what's relevant.

### `digest.py`
pydantic-ai agent that writes the full digest. Receives stories + feedback notes + language
(all go into the system prompt). Returns `DigestOutput(main, overflow)`. The agent is
instructed to keep `main` under 800 words and dump the rest into `overflow`.

### `synthesis.py`
Runs weekly. Fetches `store.get_recent_digests(topic_id, days=7)`, passes them to a
`build_model("smart")` agent, asks it to surface: contested facts, confirmed facts,
narrative drift, source patterns. This is the expensive weekly call — justified because
it synthesizes a week of material into something genuinely new.

### `store.py`
All DB access. Key functions:
```python
get_or_create_user(telegram_id, first_name, username) -> User
get_topics(telegram_id) -> list[Topic]
get_topic_by_name(telegram_id, name) -> Topic | None
create_topic(telegram_id, *, name, frequency, sources, ...) -> Topic
update_topic(topic_id, **fields) -> None
get_seen_urls(topic_id) -> set[str]
get_seen_embeddings(topic_id, since_days: int) -> list[list[float]]
record_seen(topic_id, articles: list[Article]) -> None
record_digest(topic_id, telegram_id, content) -> UUID
get_recent_digests(topic_id, days: int) -> list[Digest]
stamp_sent(topic_id) -> None
save_feedback(telegram_id, topic_id, digest_id, signal, note) -> None
append_feedback_note(topic_id, note) -> None   # appends to topic.feedback_notes
```

### `bot.py`
All python-telegram-bot handlers.

Commands:
```
/start                      → welcome, upsert user
/add_topic                  → ConversationHandler: name → frequency → sources → confirm
/topics                     → list topics (name, frequency, paused/active, last sent)
/pause <name>               → pause updates
/resume <name>              → resume updates
/check <name>               → immediate digest, stamps last_sent_at
/more                       → send stored overflow from last digest
/synthesis <name>           → trigger weekly synthesis now
/add_source <name> <source> → add a source to a topic
/del_source <name> <source> → remove a source
```

Inline keyboard on each digest:
`👍 good` · `📚 too shallow` · `📖 too long` · `🔁 already knew` · `🎭 wrong persona`
Tapping one saves to `elephant_feedback` and offers extended correction if negative.

Free text outside a conversation: dispatcher agent (`build_model("fast")`) detects
intent — feedback, question, command in natural language — and routes accordingly.

### `jobs.py`
```python
async def run_due_digests(bot: Bot) -> int: ...
async def run_due_syntheses(bot: Bot) -> int: ...
```
`is_due()` mirrors inspiration_bot: local time from timezone, compare to send_hour,
check `last_sent_at` not already today, check cadence. Idempotent — double tick safe.
Failures per user are caught and logged; one user's error doesn't block others.

### `app.py`
FastAPI. Lifespan: init PTB + apply migrations + register webhook (if `PUBLIC_URL` set).
- `POST /telegram/webhook` — verified by `X-Telegram-Bot-Api-Secret-Token` header
- `POST /cron/tick` — verified by `X-Cron-Secret` header; calls both job runners

## Services used

| Service | Used for |
|---|---|
| `llm.research()` | News gathering (`research.py`) |
| `llm.embed_one()` | Semantic dedup (`dedup.py`) |
| `llm.build_model("fast")` | Event clustering, intent dispatcher |
| `llm.build_model("balanced")` | Propaganda analysis, digest generation |
| `llm.build_model("smart")` | Weekly synthesis only |
| `db.apply_migrations()` | On startup |

## Entrypoints (pyproject.toml)

```
elephant-bot    → polling mode (dev)
elephant-cron   → run due digests + syntheses once, then exit (dev/test)
elephant-serve  → FastAPI app (prod)
```

## Build order

Each module can be built and tested in isolation before the next depends on it:

```
1.  migrations/001_init.sql   → apply, verify schema + pgvector
2.  store.py                  → CRUD tests against dev DB
3.  research.py               → gather articles (integration, costs a little)
4.  dedup.py                  → URL filter + embedding similarity
5.  perspectives.py           → event clustering on sample articles
6.  propaganda.py             → signal extraction on known-biased text
7.  digest.py                 → full digest generation end-to-end
8.  synthesis.py              → synthesis from synthetic digest history
10. bot.py + jobs.py + app.py → full pipeline: cron tick → Telegram message
```
