"""Experiment: which model gives the best rhetorical-signal analysis for the cheapest price?

Reuses the REAL system prompt, checklist, and output schema from elephant/propaganda.py
(imported, not copy-pasted) so this tests the actual product prompt — only the model
underneath changes. Runs on several known-biased test stories (plus one neutral control,
to check for false positives) with an obvious answer key, so output quality is easy to
eyeball.

    uv run python scripts/experiments/compare_propaganda_models.py

Candidates chosen from the OpenRouter catalog check (2026-06-23): claude-sonnet-4.6 is
the current production model ("balanced" tier); the others are cheaper or differently-
trained reasoners worth checking before swapping anything in propaganda.py.
"""

from __future__ import annotations

import asyncio

from _harness import ModelResult, print_aggregate_by_model, run_model, save_results
from pydantic_ai import Agent

from elephant.models import SourceView, Story
from elephant.propaganda import _CHECKLIST, _format_prompt, _Output  # noqa: SLF001

CANDIDATES = [
    "anthropic/claude-sonnet-4.6",  # current production ("balanced" tier) — baseline
    "anthropic/claude-opus-4.8",  # current "smart" tier — is it worth the 8x cost here?
    "deepseek/deepseek-r1-0528",  # cheap open chain-of-thought reasoner
    "x-ai/grok-4.3",  # different training data/perspective
    # qwen/qwen3-235b-a22b-thinking-2507 dropped — rejects forced structured output
    # in thinking mode (ModelHTTPError 400), not usable with pydantic-ai output_type here.
]

_SYSTEM_PROMPT = (
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
)


def _agent_factory(model_slug: str) -> Agent[None, _Output]:
    """Mirrors the real Agent() construction in propaganda.py, model swapped."""
    from agent.services.llm import build_model

    return Agent(build_model(model_slug), output_type=_Output, system_prompt=_SYSTEM_PROMPT)


# Test stories, each with a different bias pattern from the checklist, plus one
# neutral control. A human reads the printed output and checks whether each model
# (a) finds the pattern the story is built around and (b) doesn't hallucinate
# signals on the control story.
TEST_STORIES: dict[str, Story] = {
    "missile_strike (loaded vocab + omission)": Story(
        headline="Missiles strike capital",
        source_views=[
            SourceView(
                source="BBC",
                url="https://bbc.com/a",
                summary="Russia fired missiles at Kyiv overnight, killing three civilians "
                "in a residential building, officials said.",
            ),
            SourceView(
                source="TASS",
                url="https://tass.ru/b",
                summary="Russia conducted a precision strike on military infrastructure in "
                "Kyiv. Civilian casualties were not confirmed by the Ministry of Defence.",
            ),
        ],
    ),
    "domestic_reform (loaded vocab + appeal to obviousness)": Story(
        headline="Government passes reform package",
        source_views=[
            SourceView(
                source="GovernmentTV",
                url="https://govtv.example/a",
                summary="The reform package will modernise an outdated system, the minister "
                "said, adding that only ill-informed agitators could oppose it.",
            ),
            SourceView(
                source="OppositionPaper",
                url="https://oppo.example/b",
                summary="Critics say the so-called reform is a power grab that will gut "
                "protections for ordinary people.",
            ),
        ],
    ),
    "labeling_fines (false equivalence + omission)": Story(
        headline="Regulator fines food company over labeling",
        source_views=[
            SourceView(
                source="IndustryNews",
                url="https://industrynews.example/a",
                summary="Industry groups said the labeling delay was no different from any "
                "routine paperwork issue, calling the proposed fines disproportionate.",
            ),
            SourceView(
                source="ConsumerWatch",
                url="https://consumerwatch.example/b",
                summary="Regulators found the mislabeled allergen warnings caused real harm "
                "to consumers, including several hospitalizations.",
            ),
        ],
    ),
    "battery_study (neutral control — should find little or nothing)": Story(
        headline="New battery chemistry shows higher energy density in lab tests",
        source_views=[
            SourceView(
                source="ScienceDaily",
                url="https://sciencedaily.example/a",
                summary="Researchers published a study in Nature showing a new battery "
                "chemistry achieves 20% higher energy density in lab tests.",
            ),
            SourceView(
                source="TechWire",
                url="https://techwire.example/b",
                summary="The study was independently reviewed by three labs, which "
                "confirmed the results using standard testing protocols.",
            ),
        ],
    ),
}


def _print_outputs(story_name: str, results: list[ModelResult]) -> None:
    print(f"\n{'#' * 70}\n# {story_name}\n{'#' * 70}")
    for r in results:
        print(f"\n=== {r.model_slug} ===")
        if r.error:
            print(f"  ERROR: {r.error}")
            continue
        for analysis in r.output.analyses:
            print(f"  {analysis.source}:")
            for signal in analysis.signals or ["(none found)"]:
                print(f"    - {signal}")


async def main() -> None:
    all_results: list[ModelResult] = []
    for story_name, story in TEST_STORIES.items():
        prompt = _format_prompt(story)
        results = [await run_model(slug, _agent_factory, prompt) for slug in CANDIDATES]
        _print_outputs(story_name, results)
        all_results.extend(results)

    print(f"\n{'=' * 70}\nOVERALL TOTALS (across {len(TEST_STORIES)} stories)\n{'=' * 70}")
    print_aggregate_by_model(all_results)
    path = save_results("propaganda_signals", all_results)
    print(f"\nFull log saved to {path}")


if __name__ == "__main__":
    asyncio.run(main())
