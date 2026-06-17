# Architecture

> Stage 5: Break it into atomic modules.

## Overview

Same dev/prod split as the inspiration_bot pattern:

| | **development** | **production** |
|---|---|---|
| Bot token | `@yourname_dev_bot` | `@disputatio_bot` |
| Database | dev Neon branch | prod Neon branch |
| Updates | long polling (`disputatio-bot`) | webhook into FastAPI (`disputatio-serve`) |
| Cron | `disputatio-cron` run by hand | Railway Cron (hourly) → `POST /cron/tick` |

## Source layout

```
src/
├── agent/            # shared services (unchanged): llm, media, storage, db, config
└── disputatio/       # this project
    ├── store.py          # all DB access, every query scoped by telegram_id via topic FK
    ├── research.py       # gather articles: research() calls per source + topic
    ├── dedup.py          # freshness filter: URL match + semantic similarity
    ├── perspectives.py   # cluster articles into stories (one event, N source views)
    ├── propaganda.py     # rhetoric analysis agent — signals, never verdicts
    ├── personas.py       # persona definitions, avatar R2 keys, selection
    ├── digest.py         # pydantic-ai agent: write digest in persona voice
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
CREATE TABLE disputatio_users (
    telegram_id  BIGINT PRIMARY KEY,
    first_name   TEXT NOT NULL,
    username     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per topic per user.
CREATE TABLE disputatio_topics (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id      BIGINT NOT NULL REFERENCES disputatio_users ON DELETE CASCADE,
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

CREATE INDEX ON disputatio_topics (telegram_id);

-- Every article sent to a user on a topic. Used for freshness filtering.
CREATE TABLE disputatio_seen (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id          UUID NOT NULL REFERENCES disputatio_topics ON DELETE CASCADE,
    article_url       TEXT NOT NULL,
    headline          TEXT NOT NULL,
    content_embedding vector(1024),    -- embed(headline + summary), for semantic dedup
    published_at      TIMESTAMPTZ,
    seen_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON disputatio_seen (topic_id);
CREATE INDEX ON disputatio_seen (topic_id, article_url);

-- Every digest sent, kept for weekly synthesis.
CREATE TABLE disputatio_digests (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id    UUID    NOT NULL REFERENCES disputatio_topics ON DELETE CASCADE,
    telegram_id BIGINT  NOT NULL,
    content     TEXT    NOT NULL,
    persona     TEXT    NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON disputatio_digests (topic_id, created_at DESC);

-- Feedback signals: quick taps and extended corrections.
CREATE TABLE disputatio_feedback (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id  BIGINT NOT NULL,
    topic_id     UUID REFERENCES disputatio_topics ON DELETE SET NULL,
    digest_id    UUID REFERENCES disputatio_digests ON DELETE SET NULL,
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
                   # pass 1 — drop URLs already in disputatio_seen for this topic
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

       persona   = personas.pick(topic)
                   # random, or topic.pinned_persona if set
                   # returns Persona(name, avatar_key, intro, voice_instructions)

       text      = await digest.generate(stories, persona, topic.feedback_notes)
                   # pydantic-ai agent, model: build_model("balanced")
                   # returns DigestOutput(main: str, overflow: str | None)
                   # main ≤ 800 words; overflow stored for /more

       → send: avatar photo (R2) + intro card + main text + feedback buttons
       → store.record_digest(...)
       → store.record_seen(topic.id, fresh)    # saves URLs + embeddings
       → store.stamp_sent(topic.id)
```

## Module responsibilities

### `research.py`
Calls `llm.research()` for each source plus one general query:
```python
research(f'recent news about "{topic.name}" from {source} last 48 hours')
research(f'"{topic.name}" latest developments multiple perspectives')
```
Parses the `Research.sources` list into `list[Article]`.

**Known limitation:** Perplexity Sonar doesn't index all sources equally — Russian state
media and paywalled outlets may not surface reliably. Direct-URL fetching is v2 scope.

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
of facts present in other sources). Report signals found, never a verdict.

### `personas.py`
All 16 personas defined as data in a list of `Persona` dataclasses. `pick()` draws
randomly (or returns pinned). Avatar images live in R2 at `disputatio/personas/<name>.png`,
pre-generated once by `scripts/generate_personas.py` using `media.text_to_image()`.

### `digest.py`
pydantic-ai agent that writes the full digest. Receives stories + persona voice
instructions + user feedback notes (all go into the system prompt). Returns
`DigestOutput(main, overflow)`. The agent is instructed to keep `main` under 800 words
and dump the rest into `overflow`.

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
record_digest(topic_id, telegram_id, content, persona) -> UUID
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
Tapping one saves to `disputatio_feedback` and offers extended correction if negative.

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
| `media.text_to_image()` | Persona avatar generation (one-shot setup script) |
| `storage.store_file()` | Save persona avatars to R2 |
| `storage.public_url()` | Serve persona avatars in Telegram |
| `db.apply_migrations()` | On startup |

## Entrypoints (pyproject.toml)

```
disputatio-bot    → polling mode (dev)
disputatio-cron   → run due digests + syntheses once, then exit (dev/test)
disputatio-serve  → FastAPI app (prod)
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
7.  personas.py               → selection logic + R2 avatar keys
8.  digest.py                 → full digest generation end-to-end
9.  synthesis.py              → synthesis from synthetic digest history
10. bot.py + jobs.py + app.py → full pipeline: cron tick → Telegram message
```
