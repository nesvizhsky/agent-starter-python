-- Disputatio: multi-perspective news bot
-- Run via db.apply_migrations() on startup.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE disputatio_users (
    telegram_id  BIGINT PRIMARY KEY,
    first_name   TEXT NOT NULL,
    username     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE disputatio_topics (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id      BIGINT NOT NULL REFERENCES disputatio_users ON DELETE CASCADE,
    name             TEXT NOT NULL,
    description      TEXT,
    frequency        TEXT NOT NULL DEFAULT 'daily',   -- 'daily' | 'twice_daily' | 'weekly'
    send_hour        INT  NOT NULL DEFAULT 8,          -- local hour (0-23)
    timezone         TEXT NOT NULL DEFAULT 'UTC',
    paused           BOOL NOT NULL DEFAULT false,
    sources          TEXT[] NOT NULL DEFAULT '{}',
    excluded_sources TEXT[] NOT NULL DEFAULT '{}',
    trusted_sources  TEXT[] NOT NULL DEFAULT '{}',
    pinned_persona   TEXT,
    feedback_notes   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_sent_at     TIMESTAMPTZ,
    last_synthesis_at TIMESTAMPTZ
);

CREATE INDEX ON disputatio_topics (telegram_id);

-- Every article ever shown to a user on a topic, used for freshness filtering.
-- content_embedding allows semantic dedup: same story, different headline.
CREATE TABLE disputatio_seen (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id          UUID NOT NULL REFERENCES disputatio_topics ON DELETE CASCADE,
    article_url       TEXT NOT NULL,
    headline          TEXT NOT NULL,
    content_embedding vector(1024),
    published_at      TIMESTAMPTZ,
    seen_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON disputatio_seen (topic_id);
CREATE INDEX ON disputatio_seen (topic_id, article_url);

-- Every digest sent, kept so the weekly synthesis can look back across them.
CREATE TABLE disputatio_digests (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id    UUID   NOT NULL REFERENCES disputatio_topics ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL,
    content     TEXT   NOT NULL,
    persona     TEXT   NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON disputatio_digests (topic_id, created_at DESC);

-- Quick-tap signals and extended free-text corrections from the user.
CREATE TABLE disputatio_feedback (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id BIGINT NOT NULL,
    topic_id    UUID REFERENCES disputatio_topics ON DELETE SET NULL,
    digest_id   UUID REFERENCES disputatio_digests ON DELETE SET NULL,
    signal      TEXT,   -- 'good' | 'too_shallow' | 'too_long' | 'already_knew' | 'wrong_persona'
    note        TEXT,   -- extended free-text correction
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
