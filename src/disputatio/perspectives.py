"""Cluster a flat list of articles into multi-source stories.

A "story" is one real-world event seen from multiple source perspectives.
Grouping is done by an LLM (balanced tier). The agent also sets importance
(1–3) and extracts an approximate event date from research context.

After clustering, a second pass detects direct factual contradictions between
sources on the same story (different numbers, timelines, who did what).
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel
from pydantic_ai import Agent

from agent.services.llm import build_model
from disputatio.models import Article, Contradiction, Story

# ---------------------------------------------------------------------------
# Clustering agent
# ---------------------------------------------------------------------------


class _Output(BaseModel):
    stories: list[Story]


_agent: Agent[None, _Output] = Agent(
    build_model("balanced"),
    output_type=_Output,
    system_prompt=(
        "You are a news editor grouping articles by the real-world event they describe.\n\n"
        "Rules:\n"
        "1. Only include articles directly about the stated topic. Discard anything else.\n"
        "2. Two articles cover the SAME event if they describe the same occurrence "
        "(attack, decision, discovery) regardless of framing. BBC 'Russia fires missiles at Kyiv' "
        "and TASS 'Russia conducts precision strike on military targets in Kyiv' = SAME event.\n"
        "3. Only create stories for things that SPECIFICALLY HAPPENED — new events, "
        "decisions, statements, or discoveries. Do not create a story for the general "
        "ongoing state of a situation. If an event is clearly old (months ago), "
        "set importance=3 rather than dropping it.\n"
        "4. Create one story per distinct event. Add every article covering it as a source_view.\n"
        "5. Headline: sentence case, informative — a reader should know what happened from "
        "the headline alone. 'Russia tightens small-business taxes to fund the war' not "
        "'Russian Economic Policy Update'.\n"
        "6. event_date: extract the approximate date from the research context if present "
        "(e.g. 'Jun 15'). Set to null if genuinely unknown.\n"
        "7. importance: 1 for concrete breaking events (attack, arrest, vote, signed deal, "
        "death, launch), 2 for trends or developments, 3 for analysis/reports/anniversaries.\n"
        "8. Return stories sorted by importance ascending (1 first, 3 last).\n"
        "9. For each source_view: one sentence stating the specific claim or framing that "
        "source used, drawn from the Research Context. What did they actually report?\n"
        "10. Fewer concrete stories beat many vague ones."
    ),
)

# ---------------------------------------------------------------------------
# Contradiction detection agent
# ---------------------------------------------------------------------------


class _ContradictionOutput(BaseModel):
    contradictions: list[Contradiction]


_contradiction_agent: Agent[None, _ContradictionOutput] = Agent(
    build_model("balanced"),
    output_type=_ContradictionOutput,
    system_prompt=(
        "Find direct factual contradictions between news sources covering the same event.\n\n"
        "A contradiction is when two sources make opposing or incompatible claims about the "
        "SAME specific fact: a number (casualties, votes, distances), a date or timeline, "
        "who did what, or whether a specific thing happened.\n\n"
        "NOT a contradiction:\n"
        "- Different tone or framing of the same facts\n"
        "- One source mentioning something the other omits\n"
        "- Minor number differences within normal reporting uncertainty (e.g. 'about 100' vs '97')\n"  # noqa: E501
        "- Different interpretations or opinions\n\n"
        "Return only clear, hard conflicts where both sources make explicit, incompatible "
        "factual claims. If none, return an empty list.\n\n"
        "claim_a and claim_b: the shortest exact phrase that shows the conflict. "
        "Prefer direct quotes or specific figures over paraphrases."
    ),
)


async def _detect_contradictions(story: Story) -> list[Contradiction]:
    """Find factual contradictions between source_views for one story."""
    if len(story.source_views) < 2:
        return []
    lines = [f"Story: {story.headline}\n"]
    for view in story.source_views:
        lines.append(f"{view.source}: {view.summary}")
    try:
        result = await _contradiction_agent.run("\n".join(lines))
        return result.output.contradictions
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def cluster(
    articles: list[Article], topic_name: str = "", lookback: str = "48 hours"
) -> list[Story]:
    """Group *articles* into stories by event, ranked by importance.

    Returns an empty list for empty input without making an LLM call.
    Does NOT guarantee every article appears — off-topic or stale articles are dropped.
    Contradiction detection runs concurrently across all stories.
    """
    if not articles:
        return []

    prompt = _format_prompt(articles, topic_name)
    result = await _agent.run(prompt)
    stories = result.output.stories

    # Attach research context to each source_view so propaganda.py has
    # richer material than the one-sentence summary alone.
    _attach_contexts(stories, articles)

    # Detect contradictions for all stories concurrently.
    contradiction_lists = await asyncio.gather(
        *[_detect_contradictions(s) for s in stories]
    )
    for story, contradictions in zip(stories, contradiction_lists, strict=True):
        story.contradictions = contradictions

    return stories


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attach_contexts(stories: list[Story], articles: list[Article]) -> None:
    """Best-effort: attach research context prose to each source_view.

    Matches on source name (case-insensitive, domain-aware). Falls back to
    the general query context if no source-specific match is found.
    """
    # Build lookup: normalised source name -> context text
    ctx_by_source: dict[str, str] = {}
    general_ctx: str | None = None
    for a in articles:
        if not a.context:
            continue
        if a.source == "general":
            general_ctx = a.context
        else:
            key = a.source.lower().removeprefix("www.")
            ctx_by_source[key] = a.context

    for story in stories:
        for view in story.source_views:
            # Try exact match, then domain suffix match (e.g. "Reuters" in "reuters.com")
            key = view.source.lower().removeprefix("www.")
            ctx = ctx_by_source.get(key)
            if ctx is None:
                for src_key, src_ctx in ctx_by_source.items():
                    if key in src_key or src_key in key:
                        ctx = src_ctx
                        break
            view.context = ctx or general_ctx


def _format_prompt(articles: list[Article], topic_name: str) -> str:
    lines: list[str] = []

    if topic_name:
        lines.append(f"TOPIC: {topic_name}\n")
        lines.append("Only include articles directly about this topic. Discard anything else.\n")

    # Include unique research contexts (one per source query) so the agent has
    # real content to draw on when writing source_view summaries.
    seen_contexts: set[str] = set()
    context_blocks: list[str] = []
    for a in articles:
        if a.context and a.context not in seen_contexts:
            seen_contexts.add(a.context)
            context_blocks.append(f"[{a.source}]\n{a.context[:600]}")
    if context_blocks:
        lines.append("RESEARCH CONTEXT (use this to write accurate source_view summaries):\n")
        lines.extend(context_blocks)
        lines.append("")

    lines.append(f"Group these {len(articles)} articles into stories:\n")
    for i, a in enumerate(articles, 1):
        lines.append(f"{i}. Source: {a.source} | Headline: {a.headline} | URL: {a.url}")
    return "\n".join(lines)
