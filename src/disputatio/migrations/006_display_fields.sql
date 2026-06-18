ALTER TABLE disputatio_topics
    ADD COLUMN IF NOT EXISTS display_name        TEXT,
    ADD COLUMN IF NOT EXISTS display_description TEXT;
