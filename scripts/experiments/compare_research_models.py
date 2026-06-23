"""Experiment: which model finds real, currently-active outlets best — Perplexity Sonar
(the only model research.py uses today) vs. other OpenRouter models with the universal
":online" web-search plugin attached (Grok, Gemini, DeepSeek, GPT)?

Triggered by checking what's on OpenRouter beyond Perplexity. Tests two topic shapes,
since identify_sides()/generate_source_guidance() need to work for both:
- a conflict topic (needs real outlet names per side)
- a non-conflict topic (needs real specialist publication names)

    uv run python scripts/experiments/compare_research_models.py
"""

from __future__ import annotations

import asyncio

from _harness import ModelResult, print_aggregate_by_model, run_research_model, save_results

CANDIDATES = [
    "perplexity/sonar",  # current production model for all research.py calls
    "x-ai/grok-4.3:online",
    "google/gemini-3.1-flash-lite:online",
    "deepseek/deepseek-v3.2:online",
    "openai/gpt-5.1:online",
]

QUERIES: dict[str, str] = {
    "conflict (Ukraine sides)": (
        "Who are the main Ukrainian government-aligned and Russian state-aligned media "
        "outlets covering the Russia-Ukraine war? Name specific outlets for each side."
    ),
    "non-conflict (AI safety specialist sources)": (
        "What are the most authoritative specialist publications and outlets for tracking "
        "AI safety research news? Name specific publications."
    ),
}


def _print_outputs(query_name: str, results: list[ModelResult]) -> None:
    print(f"\n{'#' * 70}\n# {query_name}\n{'#' * 70}")
    for r in results:
        print(f"\n=== {r.model_slug} ===")
        if r.error:
            print(f"  ERROR: {r.error}")
            continue
        print(f"  sources cited: {len(r.output['sources'])}")
        print(f"  {r.output['text'][:500]}")


async def main() -> None:
    all_results: list[ModelResult] = []
    for query_name, query in QUERIES.items():
        results = [await run_research_model(slug, query) for slug in CANDIDATES]
        _print_outputs(query_name, results)
        all_results.extend(results)

    print(f"\n{'=' * 70}\nOVERALL TOTALS (across {len(QUERIES)} queries)\n{'=' * 70}")
    print_aggregate_by_model(all_results)
    path = save_results("research_models", all_results)
    print(f"\nFull log saved to {path}")


if __name__ == "__main__":
    asyncio.run(main())
