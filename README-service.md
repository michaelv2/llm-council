# Applying this bundle to the llm-council repo (on the host)

This bundle was drafted and live-tested in an agentsafe container where
llm-council is read-only. Apply it on the host where the repo is writable.

## Option A — one command

```bash
./apply.sh /path/to/llm-council
```

The script refuses to run on a dirty git tree, creates/switches to a
`service-mode` branch, copies the files, and removes the web-backend files
(`openrouter.py`, `main.py`, `storage.py` — recoverable via git).

## Option B — manual

```bash
cd /path/to/llm-council

# new + replacement backend files
cp llm-council-service/backend/providers.py backend/providers.py
cp llm-council-service/backend/config.py     backend/config.py      # replaces OpenRouter config
cp llm-council-service/backend/council.py    backend/council.py     # replaces web version
cp llm-council-service/backend/cli.py        backend/cli.py

# service mode replaces the web backend (per SPEC.md section 6); frontend/ stays untouched
rm backend/openrouter.py backend/main.py backend/storage.py

# config + packaging
cp llm-council-service/council.yaml .
cp llm-council-service/pyproject.toml .
cp llm-council-service/.env.example .env   # then fill in MISTRAL_API_KEY

# skills (global scope; use .vibe/skills/ in a project instead if preferred)
cp -r llm-council-service/skills/council-plan   ~/.vibe/skills/
cp -r llm-council-service/skills/council-review ~/.vibe/skills/

# tests
cp llm-council-service/tests/test_service.py tests/

uv sync   # also installs the dev group (pytest); refreshes uv.lock automatically
uv run python -m pytest tests/test_service.py -q   # offline, no network
# one-off without the dev group: uv run --with pytest python -m pytest tests/ -q
# `uv run council` works because pyproject now has a build-system (hatchling),
# so uv sync installs the project and creates the script. If it is ever
# missing (e.g. stale venv), re-run `uv sync`, or use the packaging-free form:
#   uv run python -m backend.cli "What is the capital of France?" --json
uv run council "What is the capital of France?" --json
```

## Before first run — check council.yaml

- Model ids are verified against the live Mistral API (see VERIFIED.md):
  `mistral-large-4`, `deepseek-v4-1-flash-1017`, `zai-glm-5-3`,
  `zai-glm-5-3-flash`; Ollama: `qwen3.8:27b-128k`.
- `providers.ollama.base_url`: `http://llm-server:11434/v1` (the default;
  works on the host and inside the agentsafe container).
- **Never add the implementer model to `councils.review`** (see DESIGN.md).
- Review council: deepseek-flash and glm-flash (hot path). Local qwen was
  tried as a third seat and removed: too slow with thinking, wrong without.
  See DESIGN.md "cadence" rule.

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
  Any model under `implementers` in council.yaml is refused as a seat or
  chairman.
- `timings` in the JSON: seconds per stage plus every call (model, start
  offset, duration, ok).
- API keys are checked only for the council and chairman a run uses.
- A review reply that is empty or has no `CHECKED:` line counts as a failed
  seat (listed in `failures`). Stage 2 is skipped when there is nothing to
  cross-vote on (no issues, or a single surviving reviewer).
- `--timeout` replaces provider default timeouts but not a `timeout:` set on
  an entry (the capped qwen review seat, the 600s chairmen).
- A council may not list the same provider/model twice (results are keyed
  by it); config errors print one `error:` line.

Exit code 0 on success, 1 if all stage-1 models failed or the chairman failed;
an empty task is rejected before any model is called. Failed models are
listed under `failures` in `--json` output (FAILURES in the text digest).
Diagnostics go to stderr; `--json` stdout is machine-readable.

## httpx vs httpx2

**Decided: stay on httpx (0.28.1) for now** — the bundle is verified live with
it. `httpx2` (pydantic's official continuation of httpx; the only breaking
change is the `import httpx` -> `import httpx2` rename) remains a drop-in
future option: swap the import in `backend/providers.py`, set
`httpx2>=2.16` in pyproject.toml, and bump `requires-python` to `>=3.11`.
