-- Generalizes scheduling: a topic used to have exactly one frequency + one send
-- time (twice_daily faked a second send by adding 12h in code, with no way to
-- pick the actual second time or a non-daily multi-slot pattern like "Mon 12:00
-- + Thu 10:00"). Replace the single frequency/send_hour/send_dow/schedule_days
-- columns with a child table of independent (days, time, cadence) slots — a
-- topic can now have any number of them, each tracked separately so they never
-- interfere with each other's due-ness.
--
-- The old columns on elephant_topics are left in place (unused going forward)
-- rather than dropped, to avoid a destructive schema change.
CREATE TABLE IF NOT EXISTS elephant_topic_slots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id UUID NOT NULL REFERENCES elephant_topics(id) ON DELETE CASCADE,
    days TEXT NOT NULL DEFAULT '',  -- comma-separated weekday ints (0=Mon..6=Sun); '' = every day
    hour INT NOT NULL,
    minute INT NOT NULL DEFAULT 0,
    every_n_weeks INT NOT NULL DEFAULT 0,  -- 0 = every matching day; >=1 = once every N weeks
    last_sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_elephant_topic_slots_topic_id ON elephant_topic_slots (topic_id);

-- Backfill: turn each existing topic's single frequency+time into one slot,
-- preserving exactly the schedule it already had. twice_daily gets a second
-- slot using the old "+12h" assumption, since that's what it actually did.
INSERT INTO elephant_topic_slots (topic_id, days, hour, minute, every_n_weeks, last_sent_at)
SELECT id,
       CASE frequency
           WHEN 'weekdays' THEN '0,1,2,3,4'
           WHEN 'mwf' THEN '0,2,4'
           WHEN 'tuth' THEN '1,3'
           WHEN 'custom_days' THEN schedule_days
           WHEN 'weekly' THEN send_dow::text
           WHEN 'biweekly' THEN send_dow::text
           ELSE ''
       END,
       send_hour,
       send_minute,
       CASE frequency WHEN 'biweekly' THEN 2 WHEN 'weekly' THEN 1 ELSE 0 END,
       last_sent_at
FROM elephant_topics;

INSERT INTO elephant_topic_slots (topic_id, days, hour, minute, every_n_weeks, last_sent_at)
SELECT id, '', (send_hour + 12) % 24, send_minute, 0, last_sent_at
FROM elephant_topics
WHERE frequency = 'twice_daily';
