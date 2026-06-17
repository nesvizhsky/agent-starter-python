"""Digest generation agent.

Takes a list of Story objects (with signals already populated by propaganda.py)
plus a Persona and optional user feedback notes, and writes the actual digest
text in the persona's voice.

Returns DigestOutput(main, overflow) where main <= 300 words.
overflow is stored in the DB and surfaced on /more.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from agent.services.llm import build_model
from disputatio.models import Story
from disputatio.personas import Persona


class DigestOutput(BaseModel):
    main: str = Field(
        description=(
            "The main digest, written in the persona's voice. 300 words maximum. Cover every story."
        )
    )
    overflow: str | None = Field(
        default=None,
        description=(
            "Extra depth, analysis, or source breakdowns — surfaced via /more. "
            "None if everything fits in main."
        ),
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


@dataclass
class _Deps:
    voice: str
    feedback_notes: str | None


_agent: Agent[_Deps, DigestOutput] = Agent(
    build_model("balanced"),
    deps_type=_Deps,
    output_type=DigestOutput,
)


@_agent.system_prompt
def _system_prompt(ctx: RunContext[_Deps]) -> str:
    notes_block = (
        f"\n\nUSER PREFERENCES (from past feedback — honour these):\n{ctx.deps.feedback_notes}"
        if ctx.deps.feedback_notes
        else ""
    )
    return (
        "You are writing a news digest for Disputatio, a multi-perspective intelligence bot. "
        "Your job: present today's stories in character, showing the reader what different sources "
        "said side by side — so they can see the full picture and judge for themselves.\n\n"
        "CHARACTER VOICE — stay in this throughout:\n"
        f"{ctx.deps.voice}\n\n"
        "FORMAT — Telegram HTML only, no other markup:\n"
        "- Each story: <b>📌 [what happened — short, factual]</b> on its own line\n"
        "- 2-3 sentences in your persona's voice: lead with the event, then how key "
        "parties reacted or framed it — do NOT lead with who reported it\n"
        "- Propaganda/bias signals: add inline immediately after the relevant phrase:\n"
        "    🚩 <i>state framing</i>  — when a source uses official/propaganda language\n"
        "    ⚠️ <i>omission</i>  — when a source leaves out a key fact\n"
        "  Use sparingly — only when clearly present. One per sentence max.\n"
        "- Last line of each story: HTML links in brackets: "
        '[<a href="URL1">Source1</a>, <a href="URL2">Source2</a>]\n'
        "  Use the URL provided for each source view.\n"
        "- Blank line between stories. Only <b> and <i> tags. No other HTML.\n\n"
        "RULES:\n"
        "1. WHAT HAPPENED comes first. Sources are evidence, not the subject.\n"
        "2. Facts must be concrete: who, what, where. Your persona colors the language "
        "and framing — it never replaces the facts with metaphor or abstraction.\n"
        "3. Voice runs through every sentence. Do not save character for the last line.\n"
        "4. 300 words max in main. Full source analysis goes in overflow.\n"
        "5. If nothing new happened, say so briefly — in character."
        f"{notes_block}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate(
    stories: list[Story],
    persona: Persona,
    feedback_notes: str | None = None,
) -> DigestOutput:
    """Write the digest in *persona*'s voice.

    stories: output of propaganda.analyze() — signals already populated.
    feedback_notes: raw text from topic.feedback_notes (user corrections).
    """
    prompt = _format_prompt(stories) if stories else _NO_NEWS_PROMPT
    deps = _Deps(voice=persona.voice, feedback_notes=feedback_notes)
    result = await _agent.run(prompt, deps=deps)
    return result.output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NO_NEWS_PROMPT = (
    "There is nothing significantly new to report today. "
    "Write a brief in-character message letting the reader know."
)


def _format_prompt(stories: list[Story]) -> str:
    noun = "story" if len(stories) == 1 else "stories"
    lines = [f"Write a digest covering these {len(stories)} {noun}:\n"]
    for i, story in enumerate(stories, 1):
        lines.append(f"## Story {i}: {story.headline}")
        for view in story.source_views:
            lines.append(f"\nSource: {view.source} | URL: {view.url}")
            lines.append(f"Says: {view.summary}")
            if view.signals:
                lines.append(f"Rhetoric signals: {'; '.join(view.signals)}")
    return "\n".join(lines)
