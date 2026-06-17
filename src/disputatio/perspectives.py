"""Cluster a flat list of articles into multi-source stories.

A "story" is one real-world event seen from multiple source perspectives.
Grouping is done by an LLM (balanced tier). The agent also sets importance
(1–3) and extracts an approximate event date from research context.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel
from pydantic_ai import Agent

from agent.services.llm import build_model
from disputatio.models import Article, Story

# ---------------------------------------------------------------------------
# Agent definition
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
        "3. Only create stories for things that SPECIFICALLY HAPPENED during the stated lookback "
        "window. Discard articles whose event clearly falls outside this window — an anniversary "
        "piece about something from months ago is NOT new news.\n"
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
# Public API
# ---------------------------------------------------------------------------


async def cluster(
    articles: list[Article], topic_name: str = "", lookback: str = "48 hours"
) -> list[Story]:
    """Group *articles* into stories by event, ranked by importance.

    Returns an empty list for empty input without making an LLM call.
    Does NOT guarantee every article appears — off-topic or stale articles are dropped.
    """
    if not articles:
        return []

    today = datetime.now(UTC).strftime("%B %d, %Y")
    prompt = _format_prompt(articles, topic_name, lookback, today)
    result = await _agent.run(prompt)
    return result.output.stories


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_prompt(articles: list[Article], topic_name: str, lookback: str, today: str) -> str:
    lines: list[str] = []

    lines.append(f"TODAY: {today}. Only include events from the last {lookback}.\n")
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
