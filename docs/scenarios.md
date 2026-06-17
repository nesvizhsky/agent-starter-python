# Scenarios

> Stage 3: Concrete end-to-end walkthroughs with real inputs and expected results.

---

## S1 — Happy path: Ukraine war, first digest

**Setup:**
User sends `/add_topic`. Bot asks:
- *"What topic?"* → "War in Ukraine"
- *"How often?"* → daily
- *"Any sources you want me to track? (I'll suggest some defaults)"*
  → User says: "TASS, Ukrainska Pravda, BBC, Meduza"
- Bot confirms and schedules.

**Next morning digest:**

Bot sends: *[avatar image of Socrates — white marble bust, slightly cracked, dramatic lighting]*

> 🏛️ **SOCRATES has entered the agora**
> *"I know that I know nothing — but let us examine what these four sources claim to know."*

> Three things happened yesterday that our witnesses describe... quite differently.
>
> **Story: Kherson shelling — 4 perspectives**
> - 🇷🇺 TASS: "Ukrainian forces shelled civilian infrastructure in the liberated territories."
> - 🇺🇦 Ukrainska Pravda: "Russian strike on Kherson kills 2 civilians."
> - 🌍 BBC: "Shelling reported in Kherson; casualties unconfirmed, both sides blame each other."
> - 📰 Meduza: "Local residents describe overnight strikes; no official Russian acknowledgment."
>
> **[Grumpy media scholar — propaganda signals]**
> TASS: "liberated territories" (contested framing), passive construction hides agency.
> UP: factual, but sourced only to Ukrainian officials. BBC: appropriately hedged.
> Meduza: eyewitness-led, low state influence.
>
> *"So, my friend — four witnesses, one night. What do you believe?"*

**Expected result:** user reads it, gets a genuine multi-perspective picture, notices the
propaganda note without being lectured.

---

## S2 — Nothing new

User has a daily update on "EU AI Act".

Research finds no articles newer than the previous digest timestamp.

Bot sends:
> *"Nothing new on EU AI Act since yesterday. Next check tomorrow at 8am."*

No padding. No invented context.

---

## S3 — User asks for a deep dive

After receiving a digest, user replies: *"Tell me more about the TASS framing"*

Bot responds with a deeper analysis: the specific rhetorical techniques used, historical
context for why TASS uses this language, examples of similar framing in past conflicts.
Persona stays on (Socrates asking probing questions about the language).

---

## S4 — User adds a custom source mid-topic

User: `/add_source "War in Ukraine" https://kyivindependent.com`

Bot: *"Added Kyiv Independent to your Ukraine war sources. I'll include it from your next update."*

Next digest includes it automatically.

---

## S5 — User excludes a source

After a digest, user: *"Exclude TASS — it's too obviously propaganda to be useful"*

Bot: *"Got it — I'll drop TASS from your Ukraine war feed. You can re-add it anytime with /add_source."*

TASS never appears in that user's Ukraine digests again.

---

## S6 — Light topic: archaeology, weekly

**Setup:** "Archaeology news", weekly, sources: "Archaeology Magazine, LiveScience, ScienceDaily"

**Weekly digest (Shakespeare persona, randomly assigned):**

> *"What light through yonder trowel breaks? Three stories from the earth this week:"*
>
> 1. 3,000-year-old Egyptian bakery found in Luxor (Archaeology Mag)
> 2. New Neanderthal DNA study revises interbreeding timeline (LiveScience)
> 3. Intact Roman road uncovered in London Crossrail extension (ScienceDaily)
>
> **[Grumpy media scholar]**
> ScienceDaily: press-release sourcing, no independent verification. Archaeology Mag: solid.
> LiveScience: hedged appropriately. *Low manipulation signals overall — these sources want
> grants, not votes. Different incentives, different spin.*

**Expected result:** lighter tone, propaganda module still runs but findings are mild —
which is itself informative (compare to Ukraine coverage).

---

## S7 — Extra check outside schedule

User reads breaking news: "There's a ceasefire rumor — check the Ukraine topic now"

User: `/check Ukraine war`

Bot immediately runs the digest pipeline and replies with fresh results, regardless of schedule.
Stamps `last_sent_at` so the regular daily update doesn't double-fire.

---

## S8 — Weekly synthesis

User has daily Ukraine updates for 3 weeks.

On Sunday, bot sends the weekly synthesis:

> *"[Terminator voice]*
> *Three weeks. 21 updates. Here is what I have learned about your narrative battlefield:*
>
> - Russian sources consistently avoided the word 'retreat'; Ukrainian sources used 'liberation'
>   for the same events.
> - BBC hedged more in week 1 than week 3 — editorial confidence shifted.
> - Meduza used eyewitness sourcing 70% of the time; TASS used official statements 90%.
>
> *Contested facts as of today: [list]. Confirmed across all sources: [list].*
> *I will be back."*

---

## S9 — Onboarding a new user

User finds the bot and sends `/start`.

Bot:
> *"Welcome to Disputatio — where every story gets a fair trial.*
> *I'll track topics for you, gather what different sources say, and flag the spin.*
> *Ready to add your first topic? Send /add_topic to begin."*
