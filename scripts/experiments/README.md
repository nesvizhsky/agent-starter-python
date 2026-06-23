# Experiments

A sandbox for trying things — new models, prompts, or approaches — before they go
into `src/elephant/`. Nothing here is imported by product code; scripts here
import *from* `src/elephant` (read-only) so an experiment tests the real prompt
or logic, with only the one variable you're testing swapped out.

Each script is a plain async Python file you run directly:

```bash
uv run python scripts/experiments/compare_propaganda_models.py
```

It prints a readable comparison to the terminal and saves a full JSON log
(inputs, raw outputs, tokens, cost, latency) to `results/` (gitignored —
logs are run output, not source).

## Shared harness (`_harness.py`)

- `run_model(model_slug, agent_factory, prompt)` — runs one prompt through one
  model, fails open (catches errors per-model so one bad model doesn't kill the run),
  and returns a `ModelResult` with cost computed from live OpenRouter pricing.
- `save_results(name, results)` — writes a timestamped JSON log.
- `print_summary(results)` — prints a latency/tokens/cost table.

## Adding a new experiment

1. Copy the shape of `compare_propaganda_models.py`: import the real prompt/agent
   logic from the relevant `src/elephant/` module, define an `agent_factory` that
   mirrors the product's `Agent()` construction with the model swapped, pick a
   small fixed test input with an obvious right answer, then loop `run_model()`
   over your candidate model list.
2. Run it, read the printed comparison, decide if anything's worth changing.
3. If a result changes your mind about the product, make the change in
   `src/elephant/` (and note why in `journal.md`) — this folder never gets wired
   into the running bot.

## Current experiments

- `compare_propaganda_models.py` — bias/rhetoric-signal quality across models,
  using the real checklist from `propaganda.py` on a known-biased test story.
