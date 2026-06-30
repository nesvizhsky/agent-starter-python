-- Admin audit log: records every change made through the admin panel.
CREATE TABLE elephant_admin_log (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    action     TEXT        NOT NULL,           -- e.g. 'topic.edit', 'user.edit', 'bot.settings'
    entity     TEXT,                           -- human label: topic name, user id, 'bot'
    details    TEXT,                           -- human-readable summary of what changed
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
