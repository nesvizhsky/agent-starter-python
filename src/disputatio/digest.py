"""Digest generation agent.

Takes a list of Story objects (with signals already populated by propaganda.py)
plus a Persona and optional user feedback notes, and writes the actual digest
text in the persona's voice.

Returns DigestOutput(main, character_note, overflow).
main: structured bullet-per-source digest, <=250 words.
character_note: persona's closing observation, sent as a separate message.
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
            "The structured digest. For each story: bold headline, 1-sentence summary, "
            "then one bullet per source. 250 words maximum."
        )
    )
    character_note: str | None = Field(
        default=None,
        description=(
            "A short closing observation in the persona's voice — their take on what "
            "today's stories reveal. 2-3 sentences max. This is the character moment."
        ),
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
        "FORMAT for main — Telegram HTML only:\n"
        "<b>📌 [what specifically happened — one concrete event]</b>\n"
        "[One sentence: the key fact. What, who, where.]\n"
        '• <a href="URL"><i>Source A</i></a> — [what they specifically said/claimed]\n'
        '• <a href="URL"><i>Source B</i></a> — [their framing] 🚩 <i>state framing</i>\n'
        '• <a href="URL"><i>Source C</i></a> — [key omission noted] ⚠️ <i>omission</i>\n'
        "[blank line between stories]\n\n"
        "Signals — add inline, sparingly, only when clearly present:\n"
        "  🚩 <i>state framing</i>  — propaganda/official language\n"
        "  ⚠️ <i>omission</i>  — a key fact left out\n\n"
        "For character_note: 2-3 sentences purely in your persona's voice — "
        "your take on what today's pattern reveals. This is your character moment.\n\n"
        "RULES:\n"
        "1. Only cover events that SPECIFICALLY HAPPENED — new facts, not the general "
        "state of affairs. 'Russia's invasion continues' is not a story.\n"
        "2. Lead with the event, not the source. Sources are bullets, not the subject.\n"
        "3. Every bullet must state what that source specifically claimed — not that "
        "they 'covered' something.\n"
        "4. 250 words max in main. Depth in overflow.\n"
        "5. If nothing genuinely new happened, say so — in character, very briefly."
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
