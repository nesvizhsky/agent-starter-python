-- Timezone moves from a per-topic setting to a per-profile (user-level) one —
-- users were confused that changing it only affected one topic. jobs.py still
-- reads topic.timezone directly for scheduling, so store.set_user_timezone()
-- fans out writes to every topic; this column is the one users actually edit.
ALTER TABLE elephant_users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';

-- Backfill: each user's most-recently-created non-UTC topic timezone becomes
-- their profile default, so anyone who'd already set a real timezone keeps it
-- instead of silently reverting to UTC.
UPDATE elephant_users u
SET timezone = sub.timezone
FROM (
    SELECT DISTINCT ON (telegram_id) telegram_id, timezone
    FROM elephant_topics
    WHERE timezone != 'UTC'
    ORDER BY telegram_id, created_at DESC
) sub
WHERE u.telegram_id = sub.telegram_id;
