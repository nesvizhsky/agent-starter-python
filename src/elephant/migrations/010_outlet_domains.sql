-- Caches outlet name -> homepage domain resolution (e.g. "BBC" -> "bbc.com"), so
-- fetch.py only resolves a given outlet name once, ever, regardless of how many
-- topics/users track it. Resolution is verified by an HTTP check before caching
-- (see fetch.py resolve_domain()), so a cached row means "this domain is real and
-- reachable" at the time it was checked, not just an LLM guess.
CREATE TABLE elephant_outlet_domains (
    outlet_name TEXT PRIMARY KEY,  -- lowercased outlet name, as a simple cache key
    domain      TEXT NOT NULL,
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
