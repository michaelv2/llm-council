---
name: council-plan
description: Load when a task is ambiguous, architecture-heavy, or open-ended with more than one plausible approach — convene the multi-model planning council (council-plan) to brainstorm approaches before implementing. Also for periodic "what should we try next" ideation in long-running improvement loops.
---

# council-plan

Brainstorm with the multi-model planning council before committing to an approach.

## When to use

- The task has more than one plausible approach, or
- it touches architecture / cross-cutting concerns, or
- you are in a long-running improvement loop and need fresh ideas
  ("given the system and the findings log, what should we try next, what are we missing?").

Skip for small, well-specified tasks — the council latency is not worth it.

## How to run

1. Compose the planning task: the goal, relevant context (findings log,
   current design), and constraints. Write it to a temp file, e.g.
   `/tmp/plan-task.md`.
2. Run the CLI from the llm-council repo (set `COUNCIL_HOME` once, e.g. in
   `~/.vibe/.env` or your shell profile; default `/path/to/llm-council`):
   `cd "${COUNCIL_HOME:-/path/to/llm-council}" && uv run python -m backend.cli --mode plan --council default --task-file /tmp/plan-task.md --json`
   (`uv run council ...` also works once `uv sync` has installed the script;
   `python -m backend.cli` works regardless of packaging)
3. Read the JSON output:
   - `stage1` — the raw proposals from each model. Show these to the
     implementer alongside the synthesis, not just the synthesis — the
     brainstorm should inform, not anchor.
   - `synthesis` — the chairman's committed plan: chosen approach, rejected
     alternatives with reasons, first steps, risks.
   - `rankings` — aggregate peer ranking of the proposals.
4. The orchestrator picks or adapts the plan, then implements in a fresh
   context (plan + repo as input, not the whole deliberation history).

## Gotchas

- Local models are the weakest at the strict `FINAL RANKING:` format; the
  parser has fallbacks, but if rankings come back empty often, keep the
  council floor at instruction-tuned models.
- Per-provider timeouts live in `council.yaml` (local Ollama gets 600s);
  override per run with `--timeout`.
