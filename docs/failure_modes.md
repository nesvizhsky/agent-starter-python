# Failure Modes

> Stage 3: How does this go wrong, and how should it fail gracefully?

## Content failures

### F1 — Bot invents quotes or sources (hallucination)
The LLM fabricates a quote, paraphrases incorrectly, or cites an article that doesn't exist.
**Must never happen silently.**
Mitigation: all claims in the digest must be grounded in the actual `research()` results passed
to the model. The model is instructed to quote and attribute, not paraphrase from memory.
If the research call returns nothing usable, the bot says so rather than fills the gap.
Hard rule: **never present something as "X source says…" without a real URL to back it.**

### F2 — Nothing new, but the bot sends anyway
The bot pads an update with old stories to look useful.
Mitigation: the digest pipeline checks whether any retrieved stories are newer than the user's
last update timestamp for this topic. If nothing clears the bar, the bot sends:
*"Nothing new on [topic] since your last update. I'll check again at [next scheduled time]."*

### F3 — Same story appears multiple times in one digest
Two sources covered the same event; the bot presents them as two separate stories.
Mitigation: after research, a deduplication step clusters stories by headline similarity and
date. Duplicate coverage of the same event is collapsed into one entry with multiple perspectives,
not listed separately.

### F4 — Propaganda analysis is itself biased
The bot consistently flags one side's sources harder than the other's, importing its own
(or the model's) ideological lean.
Mitigation: the propaganda module is given identical criteria for all sources and is explicitly
instructed to apply them symmetrically. The output always names the source being analyzed and
shows the specific signals found, so the user can evaluate the analysis itself.
The fun-persona framing ("the grumpy media scholar notes…") signals that this is interpretation,
not verdict. **The bot must never conclude "this source is propaganda" — only "here are the
signals I found."**

### F5 — Source is paywalled or unavailable
A tracked URL returns a paywall, 404, or rate-limit.
Mitigation: log and skip the source for this run. If a source consistently fails over several
cycles, notify the user: *"I've been unable to reach [source] for 3 updates — want to remove it?"*

## UX failures

### F6 — Persona swamps the content
The creative delivery is so heavy that the actual information is buried.
Mitigation: persona is applied to the *framing and transitions*, not to the facts themselves.
Quotes and source attributions are always presented straight. The persona is the narrator's
voice, not a rewrite of the news.

### F7 — Digest is too long to read in Telegram
A hot topic produces a wall of text.
Mitigation: each digest has a hard cap per topic. If there's more to say, the bot ends with
*"There's more — reply /more for the full picture."* This is the default shape; the user
can change their preferred depth.

### F8 — User gives feedback but the bot ignores it
User says "exclude [source]" and the next digest includes it again.
Mitigation: source preferences are stored per user per topic and applied before the research
step, not after. The bot confirms every preference change immediately.

## System failures

### F9 — Cron tick doubles a send
The scheduler fires twice in one window; the user gets two identical digests.
Mitigation: `last_sent_at` is stamped before sending, and `run_due_sends()` checks it is not
already today (for daily) before sending. Idempotent — a double tick can't double-send.

### F10 — Research step is slow or times out
The topic has many sources; the research call takes too long.
Mitigation: cap research calls per digest run. Notify the user if a digest is delayed:
*"Your [topic] update is taking longer than usual — I'll send it when it's ready."*
