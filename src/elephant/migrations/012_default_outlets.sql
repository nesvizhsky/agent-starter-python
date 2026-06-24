-- Stores 3-5 outlets auto-identified as authoritative for this topic (same
-- grounded research as source_guidance, just extracted as a structured list
-- instead of prose). gather() queries these directly via the same
-- sitemap/RSS-first mechanism as user-tracked sources, instead of relying
-- only on the general catch-all AI search's recall for topics with no
-- user-added sources.
ALTER TABLE elephant_topics ADD COLUMN default_outlets TEXT[] NOT NULL DEFAULT '{}';
