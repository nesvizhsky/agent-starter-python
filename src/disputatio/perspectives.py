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
from disputatio.models import Article, SourceView, Story

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
        "1. Two articles are about the SAME event if they describe the same occurrence "
        "(same attack, same decision, same discovery) even if the framing, vocabulary, "
        "or emphasis differs. BBC saying 'Russia fires missiles at Kyiv' and TASS saying "
        "'Russia conducts precision strike on military targets in Kyiv' are the SAME event.\n"
        "2. Create one story per distinct event. Add every article that covers it as a source_view.\n"  # noqa: E501
        "3. Write a short, neutral headline for each story — describe what happened without "
        "adopting any source's framing.\n"
        "4. For each source_view, write one sentence summarising the specific finding, "
        "conclusion, or claim that source reported — not just that they covered it. "
        "Use the article summary to do this accurately.\n"
        "5. Return ALL input articles. Do not omit any."
    ),
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def cluster(articles: list[Article]) -> list[Story]:
    """Group *articles* into stories by event.

    Returns an empty list for empty input without making an LLM call.
    Guarantees every input article appears in at least one story.
    """
    if not articles:
        return []

    prompt = _format_prompt(articles)
    result = await _agent.run(prompt)
    stories = result.output.stories

    # Safety net: ensure no article was lost in the LLM's grouping
    stories = _recover_missing(articles, stories)

    return stories


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_prompt(articles: list[Article]) -> str:
    lines = [f"Group these {len(articles)} articles into stories:\n"]
    for i, a in enumerate(articles, 1):
        lines.append(f"{i}. Source: {a.source} | Headline: {a.headline}")
        lines.append(f"   Summary: {a.summary}")
        lines.append(f"   URL: {a.url}")
    return "\n".join(lines)


def _recover_missing(articles: list[Article], stories: list[Story]) -> list[Story]:
    """Add any articles the LLM omitted as solo stories."""
    seen_urls = {view.url for story in stories for view in story.source_views}
    for article in articles:
        if article.url not in seen_urls:
            stories.append(
                Story(
                    headline=article.headline,
                    source_views=[
                        SourceView(
                            source=article.source,
                            url=article.url,
                            summary=article.summary,
                        )
                    ],
                )
            )
    return stories
