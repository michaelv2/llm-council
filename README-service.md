# LLM Council — service mode

A one-shot `council` CLI (plus Vibe skills and agents) that runs the
three-stage council: individual answers, anonymized peer evaluation, chairman
synthesis. Modes: `answer`, `plan` (brainstorming) and `review` (code review).
Design decisions are in [DESIGN.md](DESIGN.md).

## Setup (host)

```bash
cd ~/projects/llm-council
cp .env.example .env              # then fill in MISTRAL_API_KEY
uv sync                           # installs the project, the `council` script and pytest
uv run python -m pytest tests -q  # offline, no network
uv run council "What is 2+2? Reply with just the number." --json
```

`uv run python -m backend.cli ...` works too and doesn't depend on the
installed script. The CLI finds `council.yaml` next to the package (or in the
current directory); use `--config` or `$COUNCIL_CONFIG` for another file.

## Vibe integration (host)

```bash
cp -r skills/council-plan skills/council-review skills/council-loop ~/.vibe/skills/
cp agents/*.toml ~/.vibe/agents/
# merge config.snippet.toml into ~/.vibe/config.toml (providers + model aliases)
```

- **council-plan / council-review** run this CLI, so they work on the host
  only: they `cd "${COUNCIL_HOME:-$HOME/projects/llm-council}"` and need the
  repo's `.env`. Inside the agentsafe container (`/.dockerenv`) the repo's
  `.env` is hidden, so use council-loop there.
- **council-loop** runs the council in-session with subagents (the
  `agents/*.toml` profiles) and Vibe's own key, so it works anywhere.
- Re-copy after changing `skills/` or `agents/`; the deployed copies don't
  update themselves.

## council.yaml

- Model ids are verified against the live Mistral API (see VERIFIED.md):
  `mistral-large-4`, `deepseek-v4-1-flash-1017`, `zai-glm-5-3`,
  `zai-glm-5-3-flash`; Ollama: `qwen3.8:27b-128k`.
- `providers.ollama.base_url`: `http://llm-server:11434/v1` (works on the
  host and inside the agentsafe container).
- Plan council: local qwen + deepseek-flash + glm-flash, chairman
  mistral-large-4. Review council: deepseek-flash + glm-flash, chairman
  zai-glm-5-3. Local qwen was tried as a third review seat and removed: too
  slow with thinking, wrong without (see DESIGN.md).
- **Never add the implementer model to `councils.review`**: models listed
  under `implementers` are refused as review seats or chairman.

## CLI usage

```bash
council "task"                          # answer mode, default council
council --task-file task.md --mode plan --council default
cat diff.md | council --mode review --council review --json
council --timeout 900 --config /path/to/council.yaml "..."
council --mode plan --no-angles "..."   # plan without the random per-seat angles
```

- **plan** gives each seat a random angle (simplest-possible, most-robust,
  different-architecture); `roles` in the JSON says who got which.
- **review** reviewers write `SEVERITY | KIND | LOCATION | description |
  evidence | check` lines after a `CHECKED:` line (KIND is BUG, MISSING or
  RULE); stage 2 votes REAL / FALSE POSITIVE / DUPLICATE per issue id and
  may add NEW issues; `issues` in the JSON has every issue with its votes,
  and `dropped` the ones the chairman discarded, with reasons.
- **answer** mode: `recommended` (and the digest's RECOMMENDED SOLUTION
  section) holds the best-ranked model's stage-1 response, every model on a
  tie. Empty in plan mode (the ranking mostly reflects the assigned angle)
  and review mode (no ranking).
- `timings` in the JSON: seconds per stage plus every call (model, start
  offset, duration, ok).
- `failures` covers every stage: `name (stage 2)` for a seat that answered
  but failed its stage-2 call, `name (chairman)` for the chairman.
- A review reply that is empty or has no `CHECKED:` line counts as a failed
  seat. Stage 2 is skipped when there is nothing to cross-vote on (no
  issues, or a single surviving reviewer).
- `--timeout` replaces provider default timeouts but not a `timeout:` set on
  an entry (e.g. the 600s chairmen).
- API keys are checked only for the council and chairman a run uses.
- A council may not list the same provider/model twice (results are keyed
  by it) and must have 1-26 seats (labels A-Z); config errors print one
  `error:` line naming the file and entry.

Exit code 0 on success, 1 if all stage-1 models failed or the chairman failed;
an empty task is rejected before any model is called. Diagnostics go to
stderr; `--json` stdout is machine-readable.

## httpx vs httpx2

**Decided: stay on httpx (0.28.1) for now** — the CLI is verified live with
it. `httpx2` (pydantic's official continuation of httpx; the only breaking
change is the `import httpx` -> `import httpx2` rename) remains a drop-in
future option: swap the import in `backend/providers.py`, set
`httpx2>=2.16` in pyproject.toml, and bump `requires-python` to `>=3.11`.
