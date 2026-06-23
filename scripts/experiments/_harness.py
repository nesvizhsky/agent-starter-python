"""Shared plumbing for one-off experiments: run the same input through several
models, capture cost/latency/tokens, and save a timestamped JSON log.

This is NOT product code — nothing in src/ imports from here. An experiment
script imports real prompts/logic from src/elephant (read-only) so it's
testing the actual thing, then uses run_model() below to try alternatives
before anything changes in the product.

Usage pattern (see compare_propaganda_models.py for a full example):

    from _harness import run_model, save_results, print_summary

    results = [await run_model(model_slug, agent_factory, prompt) for model_slug in CANDIDATES]
    save_results("propaganda_signals", results)
    print_summary(results)
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent

RESULTS_DIR = Path(__file__).parent / "results"

# OpenRouter model pricing, fetched live and cached for the process lifetime —
# prices change often enough that hardcoding them would silently go stale.
_pricing_cache: dict[str, tuple[float, float]] = {}


async def _get_pricing(model_slug: str) -> tuple[float, float]:
    """Return (prompt_price_per_token, completion_price_per_token) for a model slug."""
    if not _pricing_cache:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://openrouter.ai/api/v1/models", timeout=15)
            resp.raise_for_status()
        for m in resp.json()["data"]:
            p = m["pricing"]
            _pricing_cache[m["id"]] = (float(p["prompt"]), float(p["completion"]))
    return _pricing_cache.get(model_slug, (0.0, 0.0))


class ModelResult(BaseModel):
    """Outcome of running one prompt through one model."""

    model_slug: str
    output: Any = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


async def run_model(
    model_slug: str,
    agent_factory: Callable[[str], Agent[None, Any]],
    prompt: str,
) -> ModelResult:
    """Run `prompt` through `model_slug`, capturing cost/latency. Fails open per-model.

    agent_factory(model_slug) must return a pydantic-ai Agent built for that model —
    pass a small closure that mirrors the real Agent() construction in the product
    module (same output_type, same system_prompt), just with the model swapped.
    """
    start = time.monotonic()
    try:
        agent = agent_factory(model_slug)
        result = await agent.run(prompt)
        latency = time.monotonic() - start
        usage = result.usage
        prompt_price, completion_price = await _get_pricing(model_slug)
        cost = usage.input_tokens * prompt_price + usage.output_tokens * completion_price
        return ModelResult(
            model_slug=model_slug,
            output=result.output,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            latency_s=latency,
        )
    except Exception as exc:  # noqa: BLE001 — experiments must never crash on one model
        return ModelResult(
            model_slug=model_slug,
            error=f"{type(exc).__name__}: {exc}",
            latency_s=time.monotonic() - start,
        )


async def run_research_model(model_slug: str, query: str) -> ModelResult:
    """Run a web-grounded research query through any OpenRouter model/plugin combo
    (e.g. "perplexity/sonar" or "x-ai/grok-4.3:online").

    Mirrors agent.services.llm.research()'s low-level call instead of reusing it,
    because research() doesn't expose OpenRouter's real billed `usage.cost` — and
    that field is the only accurate way to price ":online" plugin calls, which add
    a web-search fee on top of base token pricing. Doesn't touch src/ — this stays
    experiment-only plumbing.
    """
    from agent.services.llm import _client

    start = time.monotonic()
    try:
        completion = await _client().chat.completions.create(
            model=model_slug,
            messages=[{"role": "user", "content": query}],
        )
        latency = time.monotonic() - start
        message = completion.model_dump()["choices"][0]["message"]
        usage = completion.model_dump().get("usage") or {}
        sources = [
            {"url": a["url_citation"]["url"], "title": a["url_citation"].get("title")}
            for a in (message.get("annotations") or [])
            if a.get("url_citation", {}).get("url")
        ]
        return ModelResult(
            model_slug=model_slug,
            output={"text": message.get("content") or "", "sources": sources},
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cost_usd=usage.get("cost", 0.0),
            latency_s=latency,
        )
    except Exception as exc:  # noqa: BLE001 — experiments must never crash on one model
        return ModelResult(
            model_slug=model_slug,
            error=f"{type(exc).__name__}: {exc}",
            latency_s=time.monotonic() - start,
        )


def save_json(experiment_name: str, data: Any) -> Path:
    """Write any JSON-serializable result to a timestamped log in results/.

    For experiments that aren't model comparisons (e.g. probing whether a feed
    URL exists) — save_results() below is for ModelResult lists specifically.
    """
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"{experiment_name}_{stamp}.json"
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


def save_results(experiment_name: str, results: list[ModelResult]) -> Path:
    """Write a timestamped JSON log to scripts/experiments/results/."""
    return save_json(experiment_name, [r.model_dump(mode="json") for r in results])


def print_summary(results: list[ModelResult]) -> None:
    """Quick human-readable table: model, latency, tokens, cost, error if any."""
    print(f"\n{'model':<40} {'latency':>8} {'in/out tok':>12} {'cost':>10}  status")
    print("-" * 90)
    for r in results:
        tok = f"{r.input_tokens}/{r.output_tokens}"
        status = r.error or "ok"
        print(f"{r.model_slug:<40} {r.latency_s:>7.1f}s {tok:>12} ${r.cost_usd:>8.4f}  {status}")


def print_aggregate_by_model(results: list[ModelResult]) -> None:
    """Sum cost/latency/tokens per model across multiple runs (e.g. several test stories)."""
    by_model: dict[str, list[ModelResult]] = {}
    for r in results:
        by_model.setdefault(r.model_slug, []).append(r)

    print(f"\n{'model':<40} {'runs':>5} {'errors':>7} {'total cost':>11} {'avg latency':>12}")
    print("-" * 90)
    for slug, runs in by_model.items():
        errors = sum(1 for r in runs if r.error)
        total_cost = sum(r.cost_usd for r in runs)
        avg_latency = sum(r.latency_s for r in runs) / len(runs)
        print(f"{slug:<40} {len(runs):>5} {errors:>7} ${total_cost:>10.4f} {avg_latency:>11.1f}s")
