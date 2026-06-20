-- Flexible scheduling: minute-precision time + multi-day custom schedules
ALTER TABLE disputatio_topics
    ADD COLUMN IF NOT EXISTS send_minute INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS schedule_days TEXT NOT NULL DEFAULT '';
-- schedule_days: comma-separated weekday numbers (0=Mon … 6=Sun)
--   used when frequency = 'custom_days'
-- send_minute: stored for display; scheduling granularity is per-hour
