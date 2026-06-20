"""Digest generation agent.

Takes a list of Story objects (with signals already populated by propaganda.py)
and optional user feedback notes, and writes the digest text.

Returns DigestOutput(main, overflow).
main: structured bullet-per-source digest, <=250 words.
overflow is stored in the DB and surfaced on /more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from agent.services.llm import build_model
from elephant.models import Story


class DigestOutput(BaseModel):
    main: str = Field(
        description=(
            "The structured digest. For each story: bold headline, 1-sentence summary, "
            "then one bullet per source. 250 words maximum."
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
    feedback_notes: str | None
    today: str
    language: str


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
    lang = ctx.deps.language
    return (
        "You are writing a news digest for Eat the Elephant, a multi-perspective intelligence bot. "
        "Your job: present today's stories clearly and factually, showing what different sources "
        "said side by side — so the reader can see the full picture and judge for themselves. "
        "Be precise and neutral. Never take sides on contested events.\n\n"
        f"Write the entire digest in {lang}. Translate everything — headlines, summaries, "
        f"source bullets, signal labels — into {lang}.\n\n"
        "FORMAT for main — Telegram HTML only:\n"
        f"<b>📌 [headline]</b> <i>· Jun 17</i>  ← event date if known, else {ctx.deps.today}\n"
        "[One sentence: the key fact. What, who, where.]\n"
        '• <a href="URL"><i>Source A</i></a> — [what they specifically said/claimed]\n'
        '• <a href="URL"><i>Source B</i></a> — [their framing] 🚩 <i>state framing</i>\n'
        f'⚡ Source A: "[claim translated into {lang}]" <tg-spoiler>original: "[claim exactly '
        'as given]"</tg-spoiler>  ← include only if contradictions are listed for this story\n'
        f'   Source B: "[claim translated into {lang}]" <tg-spoiler>original: "[claim exactly '
        'as given]"</tg-spoiler>\n'
        "[blank line between stories]\n\n"
        "Signals — add inline, sparingly, only when the language clearly warrants it:\n"
        "  🚩 <i>state framing</i>  — loaded or official propaganda language\n\n"
        "Contradictions — include after the source bullets, only if listed for that story:\n"
        f'  ⚡ Source A: "[claim, translated into {lang}]" <tg-spoiler>original: "[claim exactly '
        'as given]"</tg-spoiler>\n'
        f'     Source B: "[claim, translated into {lang}]" <tg-spoiler>original: "[claim exactly '
        'as given]"</tg-spoiler>\n'
        f"  Translate each claim into {lang} so the reader can follow it, but ALWAYS also "
        "include the claim exactly as given — verbatim, unmodified, never paraphrased or "
        "dropped — inside a <tg-spoiler> tag right after, so it's tap-to-reveal rather than "
        f"cluttering the line. If the claim as given is already in {lang}, omit the "
        "<tg-spoiler> part entirely since there's nothing extra to show.\n\n"
        "RULES:\n"
        "1. Headlines MUST be sentence case: lowercase except the first word and proper nouns.\n"
        "2. Present stories in the order given — most important (importance=1) first.\n"
        "3. Only cover events that SPECIFICALLY HAPPENED — new facts, not the general "
        "state of affairs.\n"
        "4. Lead with the event, not the source. Sources are bullets, not the subject.\n"
        "5. Every bullet must state what that source specifically claimed.\n"
        "6. 250 words max in main. Depth in overflow.\n"
        "7. If nothing genuinely new happened, say so briefly and factually."
        f"{notes_block}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate(
    stories: list[Story],
    feedback_notes: str | None = None,
    language: str = "English",
) -> DigestOutput:
    """Write the digest.

    stories: output of propaganda.analyze() — signals already populated.
    feedback_notes: raw text from topic.feedback_notes (user corrections).
    """
    from datetime import UTC, datetime

    today = datetime.now(UTC).strftime("%b %d")
    prompt = _format_prompt(stories) if stories else _NO_NEWS_PROMPT
    deps = _Deps(feedback_notes=feedback_notes, today=today, language=language)
    result = await _agent.run(prompt, deps=deps)
    output = result.output
    output.main = _strip_redundant_spoilers(output.main)
    if output.overflow:
        output.overflow = _strip_redundant_spoilers(output.overflow)
    return output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Matches: "<claim>" <tg-spoiler>original: "<claim>"</tg-spoiler>
# The LLM is told to add the spoiler only when the visible claim was translated
# from a different language, but doesn't reliably skip it when they're already
# identical (e.g. an English digest quoting an English source) — strip those
# redundant reveals deterministically rather than trust the model's judgment.
_REDUNDANT_SPOILER_RE = re.compile(r'"([^"]+)"\s*<tg-spoiler>original:\s*"([^"]+)"</tg-spoiler>')


def _strip_redundant_spoilers(text: str) -> str:
    def _collapse(m: re.Match[str]) -> str:
        visible, original = m.group(1), m.group(2)
        if visible.strip().casefold() == original.strip().casefold():
            return f'"{visible}"'
        return m.group(0)

    return _REDUNDANT_SPOILER_RE.sub(_collapse, text)


_NO_NEWS_PROMPT = (
    "No article in this check qualified as a specific, reportable news event for this topic. "
    "Write ONE short sentence telling the reader there's nothing new to report.\n\n"
    "You have NO information about what was searched, why nothing qualified, what sources "
    "were checked, or what time period was covered beyond 'this check' — do not invent or "
    "guess at any of that. Do not mention dates, months, source names, source types "
    "(e.g. 'aggregators'), or reasons. Just state plainly that there's nothing new."
)


def _format_prompt(stories: list[Story]) -> str:
    noun = "story" if len(stories) == 1 else "stories"
    lines = [
        f"Write a digest covering these {len(stories)} {noun} (already sorted by importance):\n"
    ]
    for i, story in enumerate(stories, 1):
        date_str = f" | Date: {story.event_date}" if story.event_date else ""
        lines.append(f"## Story {i} [importance={story.importance}]: {story.headline}{date_str}")
        for view in story.source_views:
            lines.append(f"\nSource: {view.source} | URL: {view.url}")
            lines.append(f"Says: {view.summary}")
            if view.signals:
                lines.append(f"Rhetoric signals: {'; '.join(view.signals)}")
        if story.contradictions:
            lines.append("\nContradictions (include these verbatim after the bullets):")
            for c in story.contradictions:
                lines.append(f'  {c.source_a}: "{c.claim_a}"')
                lines.append(f'  {c.source_b}: "{c.claim_b}"')
    return "\n".join(lines)
