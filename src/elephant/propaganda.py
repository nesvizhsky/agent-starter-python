"""Rhetoric and propaganda signal analysis.

Runs on every story after clustering. For each source view, identifies
specific rhetorical signals — never a verdict on whether content is "propaganda".

The same checklist is applied to every source equally, so the analysis is
structurally fair even when the signals it finds are not.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pydantic import BaseModel
from pydantic_ai import Agent

from agent.services.llm import build_model
from elephant.models import Side, SourceView, Story  # noqa: F401  (SourceView used in type hints)

# ---------------------------------------------------------------------------
# Output model (intermediate — merged back into Story.source_views)
# ---------------------------------------------------------------------------


class SourceSignals(BaseModel):
    """Rhetorical signals found in one source's coverage of a story."""

    source: str
    signals: list[str]  # e.g. ["loaded vocabulary: 'liberated territories'",
    #                            "passive construction hides agency: 'buildings were destroyed'"]


class _Output(BaseModel):
    analyses: list[SourceSignals]


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

_CHECKLIST = """\
- Loaded vocabulary: emotionally charged words that frame the event
  (e.g. 'terrorist attack' vs 'military operation', 'settlers' vs 'colonists')
- One-sided framing: selecting which facts to include so only one interpretation looks valid
- Dehumanising language: referring to a group as vermin, a threat, or an abstraction
- False equivalence: treating clearly unequal things as equivalent to appear balanced
- Appeal to obviousness: 'everyone knows', 'it is clear that', 'obviously'
- Passive construction hiding agency: 'buildings were destroyed' — destroyed by whom?
- Omits a clearly relevant other side: the story involves another named party, but this
  source's coverage doesn't engage with their position, statements, or actions at all"""

_agent: Agent[None, _Output] = Agent(
    build_model("balanced"),
    output_type=_Output,
    system_prompt=(
        "You are a media-literacy analyst. For each news source covering a story, identify "
        "specific rhetorical signals — the kind a journalism professor would highlight in a "
        "seminar. Apply the SAME checklist to EVERY source; no source is exempt.\n\n"
        f"Checklist:\n{_CHECKLIST}\n\n"
        "Rules:\n"
        "1. Return one entry per source, in the same order as the input. "
        "Include every source even if you find zero signals.\n"
        "2. Each signal must be specific: name the technique and quote the phrase. "
        "Example: \"loaded vocabulary: 'precision strike' implies surgical accuracy\"\n"
        "3. Do NOT issue a verdict ('this source is propaganda'). Only report observations.\n"
        "4. If a source reports facts without rhetorical colouring, return an empty signals list."
    ),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def analyze(stories: list[Story], sides: list[Side] | None = None) -> list[Story]:
    """Fill in signals on each SourceView across all stories.

    Fires one LLM call per story, concurrently. Updates and returns the
    same list — signals on each SourceView are populated in place.
    Fails open: an analysis error leaves signals empty; the story still runs.

    sides: the topic's identified parties/perspectives (if any), so the omission
    check knows which "other side" a source might be ignoring.
    """
    if not stories:
        return stories

    tasks = [_analyze_story(story, sides) for story in stories]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for story, result in zip(stories, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("propaganda.analyze failed for story '{}': {}", story.headline, result)
        else:
            _merge(story, result)

    return stories


# ---------------------------------------------------------------------------
# Helpers (exported for testing)
# ---------------------------------------------------------------------------


async def _analyze_story(story: Story, sides: list[Side] | None = None) -> list[SourceSignals]:
    prompt = _format_prompt(story, sides)
    result = await _agent.run(prompt)
    return result.output.analyses


def _format_prompt(story: Story, sides: list[Side] | None = None) -> str:
    lines = []
    if sides:
        side_names = ", ".join(s.name for s in sides)
        lines.append(
            f"This topic involves these sides/parties: {side_names}. When checking for "
            "omission, consider whether each source's coverage engages with the other "
            "relevant side(s) given what this specific story is about — not every story "
            "touches every side equally, so only flag a genuine, story-relevant omission.\n"
        )
    lines += [
        f"Story: {story.headline}\n",
        f"Analyse these {len(story.source_views)} source(s) for rhetorical signals:\n",
    ]
    for i, view in enumerate(story.source_views, 1):
        lines.append(f"{i}. Source: {view.source}")
        lines.append(f"   Summary: {view.summary}")
        if view.context:
            lines.append(f"   Research context: {view.context[:700]}")
    return "\n".join(lines)


def _merge(story: Story, analyses: list[SourceSignals]) -> None:
    """Write signals back into story.source_views, matching by source name."""
    # Exact match first; case-insensitive fallback in case the LLM varies capitalisation
    exact: dict[str, list[str]] = {a.source: a.signals for a in analyses}
    lower: dict[str, list[str]] = {a.source.lower(): a.signals for a in analyses}
    for view in story.source_views:
        signals = exact.get(view.source) or lower.get(view.source.lower()) or []
        view.signals = signals
