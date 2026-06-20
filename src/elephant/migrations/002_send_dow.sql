-- Add day-of-week for weekly digests (0=Monday … 6=Sunday, matches Python weekday())
ALTER TABLE disputatio_topics
    ADD COLUMN IF NOT EXISTS send_dow INT NOT NULL DEFAULT 0;
