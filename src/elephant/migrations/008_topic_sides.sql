-- Stores the auto-identified sides/parties for a topic (JSON-encoded list[Side]),
-- generated once when the topic is created/described and reused on every digest tick.
ALTER TABLE elephant_topics ADD COLUMN sides_json TEXT;
