# CLAUDE.md - Technical Notes for LLM Council

Technical details, architectural decisions, and implementation notes for future development sessions.

## Project Overview

LLM Council is a 3-stage deliberation system where multiple LLMs answer a task, review each other's anonymized answers, and a chairman model synthesizes the result. This fork runs in **service mode**: a one-shot `council` CLI (`backend/cli.py`) and Vibe skills/agents, instead of upstream's web app. See `README-service.md` (setup and usage), `DESIGN.md` (decisions) and `SPEC.md` (original spec).

## Architecture

### Backend (`backend/`)

**`config.py`**
- Loads `council.yaml` (or `--config` / `$COUNCIL_CONFIG`) into named councils and chairmen of `ModelEntry` objects
- API keys come only from the environment (`key_env` per provider, `.env` via python-dotenv), never from the YAML; `check_keys()` fails fast for the council and chairman a run actually uses
- `implementers` (list of provider/model) names the models that write the code under review
- Per-provider `base_url`, `timeout`, `max_tokens`; an entry may override `timeout`/`base_url`

**`providers.py`**
- `ProviderRegistry.query()` / `query_parallel()`: OpenAI-compatible chat completions (Mistral, Ollama) or the Anthropic messages API
- One retry on 429/5xx; any other failure logs to stderr and returns None (graceful degradation)
- Every call is timed into `registry.calls`; keyless providers (Ollama) get no Authorization header; Anthropic uses `base_url` (default https://api.anthropic.com)
- `_message_content()` normalizes Mistral's list-of-typed-parts content (reasoning models) to plain text

**`council.py`** - the core logic, three modes (`answer`, `plan`, `review`) with mode-specific prompts
- `stage1_collect_responses()`: parallel queries to all council models; in plan mode each seat gets its own system prompt with a random angle from `PLAN_ANGLES` (`assign_angles()`)
- `stage2_collect_rankings()`: only stage-1 successes take part; responses are shuffled before labelling "Response A, B, ..." (avoids position bias); returns (results, label_to_model, issues)
- Review mode: stage-1 lines in `REVIEW_FORMAT` (SEVERITY | KIND | LOCATION | description | evidence | check, KIND = BUG/MISSING/RULE, after a CHECKED: line) are parsed into issues with ids (A1, B2, ...); stage 2 votes per id (`parse_votes()`) and may add `NEW |` issues; `tally_votes()` ignores a reviewer's votes on its own issues; the chairman gets the issues with vote counts and must list each issue it drops under `DROPPED:` (parsed into `dropped` by `parse_dropped()`). No peer ranking in review mode
- `stage3_synthesize_final()`: chairman synthesis; on failure returns an error string with `"failed": True`
- `parse_ranking_from_text()`: reads the LAST "FINAL RANKING:" section, prefers its numbered list, drops duplicate labels; without the heading the evaluation counts as no vote
- `calculate_aggregate_rankings()`: average rank position per model

**`cli.py`**
- Task from an argument, `--task-file` or stdin; an empty task is rejected before any model is called
- `--json` output includes `failures` (failed council seats, and the chairman as `"<name> (chairman)"`)
- Exit code 1 if all stage-1 models failed or the chairman failed, else 0
- Review mode refuses any seat or chairman listed under `implementers`; `--no-angles` turns off plan angles
- `--json` also has `roles` (plan), `issues` (review) and `timings` (per stage, plus each call)

### Vibe integration
- `skills/` (council-plan, council-review, council-loop) and `agents/*.toml` are deployed by copying to `~/.vibe/skills/` and `~/.vibe/agents/`; `config.snippet.toml` lists the providers and model aliases the agents expect (`mistral4`, `deepseek-flash`, `glm-flash`, `glm`, `qwen-local`)
- `skills/council-loop/prompts.md` mirrors the prompts in `council.py`: keep them in sync

### Frontend (`frontend/`)
Kept from upstream (React + Vite) but unused: it expects the removed FastAPI backend on port 8001 (`frontend/src/api.js`). Leave it in place.

## Key Design Decisions

- **Councils per cadence** (`council.yaml`): the plan council (cold path) includes the slow local model; the review council (hot path) is hosted flash models only, since `asyncio.gather` waits for the slowest seat.
- **Never put the implementer model on the review council**, not even as chairman (familiarity bias). See DESIGN.md.
- **Ollama address**: `http://llm-server:11434/v1`, the same on the host and in the agentsafe container. `qwen-lan` does not resolve.
- **Anonymized peer review**: models see only "Response X" labels; the chairman and the output see model names.

## Implementation Notes

- Backend modules use relative imports (`from .config import ...`); run as `python -m backend.cli` from the project root, or `uv run council` (installed by `uv sync` via the hatchling build-system).
- Diagnostics go to stderr so `--json` stdout stays machine-readable.
- Model ids are verified against the live APIs in VERIFIED.md (a historical log; don't edit it to match later changes).

## Testing

`uv run python -m pytest tests -q` runs the offline suite (`tests/test_service.py`, no network: fake registry and config). `council.test.yaml` has cheap smoke councils for live checks.
