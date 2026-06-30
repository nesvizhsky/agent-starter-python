-- Generic key-value store for admin panel settings.
-- Currently used to persist the read-only admin password hash.
CREATE TABLE elephant_admin_settings (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
