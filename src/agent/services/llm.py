"""Language models, via OpenRouter.

We use **OpenRouter** (https://openrouter.ai) as a single gateway to many models
(OpenAI, Anthropic, Google, Perplexity, open-source, ...). One API key, and
switching models is a one-line change.

You pick a model by **tier** — its job — not by guessing slugs:

    from agent.services.llm import build_model
    from pydantic_ai import Agent

    agent = Agent(build_model("smart"))        # for hard reasoning
    quick = Agent(build_model())               # defaults to "balanced"
    raw   = Agent(build_model("openai/gpt-5.1"))  # or pass any OpenRouter slug

Tiers live in one place (TIERS below), so when a better/cheaper model appears,
you change it here and every agent benefits. Prices are per 1M tokens (in/out).

pydantic-ai OpenRouter docs: https://ai.pydantic.dev/models/openrouter/
Browse all models + prices:  https://openrouter.ai/models
"""

from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from agent.config import get_settings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Chat/agent models, by tier. Update these as better models ship.
TIERS: dict[str, str] = {
    "fast": "google/gemini-2.5-flash-lite",  # $0.10/$0.40 — cheap & quick, simple tasks
    "balanced": "anthropic/claude-sonnet-4.6",  # $3/$15 — the everyday default
    "smart": "anthropic/claude-opus-4.8",  # $5/$25 — frontier; for the hard stuff
    "research": "perplexity/sonar",  # $1/$1 — web search + citations
    "research_deep": "perplexity/sonar-pro",  # $3/$15 — heavier multi-source research
}
DEFAULT_TIER = "balanced"

# Embeddings turn text into vectors (for search / similarity / RAG). Open-source,
# 1024-dim so it fits pgvector indexes cleanly. NOTE: embeddings are NOT a
# pydantic-ai concept — they use the OpenAI-compatible client below, not an Agent.
EMBED_MODEL = "baai/bge-m3"


def _resolve(model: str) -> str:
    """Turn a tier name into a slug, or pass a full slug straight through."""
    if model in TIERS:
        return TIERS[model]
    if "/" in model:  # looks like a real OpenRouter slug, e.g. "openai/gpt-5.1"
        return model
    raise ValueError(
        f"Unknown model tier {model!r}. Use one of {sorted(TIERS)} "
        "or a full OpenRouter slug like 'openai/gpt-5.1'."
    )


def build_model(model: str = DEFAULT_TIER) -> OpenRouterModel:
    """Build a chat model for an Agent.

    `model` is a tier name ("fast", "balanced", "smart", "research", ...) or a
    full OpenRouter slug. Reads OPENROUTER_API_KEY from your environment / .env.

    Note: the `research*` (Perplexity sonar) models do their own web search but
    may not support typed `output_type=` outputs — use them for plain-text answers.
    """
    return OpenRouterModel(
        _resolve(model),
        provider=OpenRouterProvider(api_key=get_settings().openrouter_api_key),
    )


@lru_cache
def _client() -> AsyncOpenAI:
    """A reusable OpenAI-compatible client pointed at OpenRouter."""
    return AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=get_settings().openrouter_api_key)


async def embed(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    """Turn a list of texts into a list of embedding vectors (one per text)."""
    response = await _client().embeddings.create(model=model, input=texts)
    return [item.embedding for item in response.data]


class Source(BaseModel):
    """A web source a research model cited."""

    url: str
    title: str | None = None


class Research(BaseModel):
    """The result of a web-grounded research call: prose plus where it came from."""

    text: str
    sources: list[Source] = Field(default_factory=list)


async def research(
    query: str,
    *,
    model: str | None = None,
    search_after_date: str | None = None,
) -> Research:
    """Web-grounded research via Perplexity Sonar — returns the answer AND its sources.

    We call OpenRouter directly here (not via a pydantic-ai Agent) because the
    Agent abstracts away the per-source citation links, which we want to show.
    Citations come back in `message.annotations` as url_citation entries.

    search_after_date: "MM/DD/YYYY" — passed to Perplexity's search_after_date_filter
      to restrict results at the API level (not just via prompt instructions).
    """
    messages: list[Any] = [{"role": "user", "content": query}]
    extra: dict[str, Any] = {}
    if search_after_date:
        extra["search_after_date_filter"] = search_after_date
    completion = await _client().chat.completions.create(
        model=model or TIERS["research"],
        messages=messages,
        extra_body=extra if extra else None,
    )
    message = completion.model_dump()["choices"][0]["message"]
    sources: list[Source] = []
    for annotation in message.get("annotations") or []:
        citation = annotation.get("url_citation") or {}
        if citation.get("url"):
            sources.append(Source(url=citation["url"], title=citation.get("title")))
    return Research(text=message.get("content") or "", sources=sources)


async def embed_one(text: str, model: str = EMBED_MODEL) -> list[float]:
    """Turn a single text into one embedding vector."""
    vectors = await embed([text], model=model)
    return vectors[0]
