ALTER TABLE disputatio_users
    ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'English';
