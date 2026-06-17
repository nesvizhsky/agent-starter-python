"""Cluster a flat list of articles into multi-source stories.

A "story" is one real-world event seen from multiple source perspectives.
Grouping is done by an LLM (fast tier — cheap, just needs to match headlines).

After clustering we check that every input article appears in the output;
any that the LLM dropped are added as solo stories so nothing is lost.
"""

from __future__ import annotations

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
        "1. Only include articles that are directly about the stated topic. "
        "Discard any article that is primarily about a different subject.\n"
        "2. Two articles are about the SAME event if they describe the same occurrence "
        "(same attack, same decision, same discovery) even if the framing, vocabulary, "
        "or emphasis differs. BBC saying 'Russia fires missiles at Kyiv' and TASS saying "
        "'Russia conducts precision strike on military targets in Kyiv' are the SAME event.\n"
        "3. Only create stories for things that SPECIFICALLY HAPPENED — new events, "
        "decisions, statements, or developments. Do not create a story for the general "
        "ongoing state of a conflict or situation.\n"
        "4. Create one story per distinct new event. Add every article that covers it "
        "as a source_view.\n"
        "5. Write a short, neutral headline for each story — what specifically happened.\n"
        "6. For each source_view, write one sentence stating the specific claim, finding, "
        "or framing that source used — drawn from the Research Context, not just the headline. "
        "What did they actually report?\n"
        "7. It is better to return fewer, concrete stories than many vague ones."
    ),
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def cluster(articles: list[Article], topic_name: str = "") -> list[Story]:
    """Group *articles* into stories by event.

    Returns an empty list for empty input without making an LLM call.
    Does NOT guarantee every article appears — off-topic articles are dropped.
    """
    if not articles:
        return []

    prompt = _format_prompt(articles, topic_name)
    result = await _agent.run(prompt)
    return result.output.stories


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
