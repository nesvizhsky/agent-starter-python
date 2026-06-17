"""Weekly synthesis agent.

Reads the last 7 days of digests for a topic and surfaces what a week
of multi-perspective coverage actually revealed: which facts were contested,
which solidified, how the narrative drifted, and what source patterns emerged.

Uses build_model("smart") — the one expensive call in the pipeline, justified
because it synthesises a week of material into something genuinely new.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from agent.services.llm import build_model
from disputatio.models import Digest


class SynthesisOutput(BaseModel):
    """Structured weekly synthesis — each field is a distinct analytical lens."""

    contested_facts: list[str] = Field(
        description=(
            "Facts or claims that different sources disagreed about during the week. "
            "Each item names the claim and who contested it."
        )
    )
    confirmed_facts: list[str] = Field(
        description=(
            "Facts that were reported consistently across sources and/or confirmed over time. "
            "These are the things we now know with higher confidence."
        )
    )
    narrative_drift: str = Field(
        description=(
            "How the story evolved across the week: what shifted, what was walked back, "
            "what escalated. One to three paragraphs."
        )
    )
    source_patterns: list[str] = Field(
        description=(
            "Patterns in how specific sources or source types covered this topic: "
            "recurring omissions, consistent framings, predictable emphases."
        )
    )
    summary: str = Field(
        description=(
            "One paragraph executive summary: what this week taught us about the topic "
            "that a single day's digest could not."
        )
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

_agent: Agent[None, SynthesisOutput] = Agent(
    build_model("smart"),
    output_type=SynthesisOutput,
    system_prompt=(
        "You are a senior analyst synthesising a week of multi-perspective news digests "
        "on a single topic. The digests were written in theatrical character voices "
        "(a medieval monk, a Terminator, Socrates, etc.) — look through the persona framing "
        "to the underlying news content.\n\n"
        "Your job is not to summarise what happened — the reader already received daily digests. "
        "Your job is to surface what a WEEK of coverage revealed that no single day could:\n\n"
        "- Which facts were initially contested and later confirmed (or refuted)?\n"
        "- Which claims turned out to be spin that didn't hold up?\n"
        "- How did the dominant narrative shift across the week?\n"
        "- What patterns do you notice in how specific sources or source types "
        "consistently framed or omitted things?\n\n"
        "Be specific. Quote dates and sources where the digests provide them. "
        "Avoid vague generalisations. If the week's coverage was thin or uneventful, say so."
    ),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate(topic_name: str, digests: list[Digest]) -> SynthesisOutput | None:
    """Synthesise *digests* into a weekly analysis for *topic_name*.

    Returns None if there are fewer than 2 digests — not enough to synthesise.
    """
    if len(digests) < 2:
        return None

    prompt = _format_prompt(topic_name, digests)
    result = await _agent.run(prompt)
    return result.output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_prompt(topic_name: str, digests: list[Digest]) -> str:
    lines = [
        f"Topic: {topic_name}",
        f"Period: {len(digests)} digest(s) from the past 7 days\n",
        "Synthesise the following digests:\n",
    ]
    for i, digest in enumerate(digests, 1):
        date_str = digest.created_at.strftime("%Y-%m-%d")
        lines.append(f"--- Digest {i} ({date_str}, persona: {digest.persona}) ---")
        lines.append(digest.content)
        lines.append("")
    return "\n".join(lines)
