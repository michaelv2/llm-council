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
- Review council is intentionally flash-only (hot path); the local model
  stays in the plan council (cold path). See DESIGN.md "cadence" rule.

## CLI usage

```bash
council "task"                          # answer mode, default council
council --task-file task.md --mode plan --council default
cat diff.md | council --mode review --council review --json
council --timeout 900 --config /path/to/council.yaml "..."
```

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
