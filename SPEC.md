# SPEC: LLM Council in Service Mode (Multi-Provider, No Web Frontend)

Convert the LLM Council from an OpenRouter-only web app into a service/CLI that takes a
prompt or task, runs the same 3-stage deliberation pipeline, and produces a digest with
aggregate rankings, the recommended solution (top-ranked raw response), and the chairman
synthesis. Replace OpenRouter as the single transport with a set of providers across
platforms (local Ollama, Mistral API, Anthropic API, Fireworks API).

The existing pipeline (`backend/council.py`) is transport-agnostic: the work is a provider
layer, a config loader, and a CLI (and optionally a headless daemon).

## 1. Provider abstraction (core change)

Key insight: Ollama, Mistral, and Fireworks are all **OpenAI chat-completions compatible** —
only the base URL and key differ. Only **Anthropic has a different wire format** (different
endpoint, `x-api-key` + `anthropic-version` headers, mandatory `max_tokens`, response body
`content[0].text`). So we need exactly **two transports**, not four.

```python
# backend/providers.py
@dataclass
class ModelEntry:
    provider: str   # "ollama" | "mistral" | "fireworks" | "anthropic"
    model: str      # provider-specific model id
    base_url: str
    key: str

class ProviderRegistry:
    def __init__(self, entries: list[ModelEntry]): ...

    async def query(self, entry: ModelEntry, messages, timeout=120.0) -> dict | None:
        if entry.provider == "anthropic":
            return await self._query_anthropic(entry, messages, timeout)
        return await self._query_openai_compatible(entry, messages, timeout)
```

- **OpenAI-compatible transport** (Ollama, Mistral, Fireworks): the current logic in
  `backend/openrouter.py`, parameterized per entry. Endpoint is `{base_url}/chat/completions`,
  `Authorization: Bearer {key}`, body is `{"model", "messages"}`. For Ollama the key can be a
  dummy string (`"ollama"`); Ollama exposes an OpenAI-compatible API at
  `http://localhost:11434/v1`.
- **Anthropic transport**: `POST https://api.anthropic.com/v1/messages`,
  headers `x-api-key` and `anthropic-version: 2023-06-01`, body adds a required `max_tokens`
  (e.g. 4096), response content extracted from `content[0].text` (handle the `type: "text"`
  block check).
- Return shape unchanged: `{"content": str, "reasoning_details": ... | None}` so
  `council.py` keeps working. `None` on failure, same graceful-degradation policy.
- `query_models_parallel` moves into the registry (or `council.py` calls
  `registry.query` per entry under `asyncio.gather`).
- Timeouts are per-provider (see section 5).

## 2. Configuration (replaces hardcoded `COUNCIL_MODELS`)

New file `council.yaml` at project root:

```yaml
council:
  - {provider: ollama,    model: qwen3:32b}
  - {provider: mistral,   model: mistral-large-latest}
  - {provider: fireworks, model: accounts/fireworks/models/llama-v3p3-70b-instruct}
  - {provider: anthropic, model: claude-sonnet-4-5}

chairman:
  provider: anthropic
  model: claude-opus-4-1

providers:
  ollama:
    base_url: http://localhost:11434/v1
    timeout: 600
  mistral:
    base_url: https://api.mistral.ai/v1
    key_env: MISTRAL_API_KEY
    timeout: 120
  fireworks:
    base_url: https://api.fireworks.ai/inference/v1
    key_env: FIREWORKS_API_KEY
    timeout: 120
  anthropic:
    key_env: ANTHROPIC_API_KEY
    max_tokens: 4096
    timeout: 120
```

- API keys stay in `.env` / environment via `key_env` — never written into the YAML.
- New `.env` (replacing the OpenRouter one):
  ```
  MISTRAL_API_KEY=...
  FIREWORKS_API_KEY=...
  ANTHROPIC_API_KEY=...
  ```
- `config.py` becomes a loader: parses YAML, resolves `providers` + `council`/`chairman`
  into `list[ModelEntry]` plus the chairman `ModelEntry`. Missing `key_env` for a provider
  in use should fail fast at startup (except Ollama).
- This also removes a latent bug: `generate_conversation_title` in `council.py` hardcodes
  an OpenRouter model ID. In service mode, use a local Ollama model for title generation
  (cheap/offline) or drop it, since conversation titles are a web-UI concern.

## 3. Service mode (replaces the web frontend)

Two options; implement both behind the same core function.

### a) One-shot CLI (primary)

```
council "How do I implement RCU in C?"      # task as arg
council --task-file task.md                  # task from file (bigger prompts)
cat task.md | council                        # task from stdin
council --json ...                           # machine-readable output
council --timeout 900                        # override per-request timeout
council --config /path/to/council.yaml       # non-default config
```

- New entry point via `pyproject.toml` `[project.scripts]` (e.g. `council = backend.cli:main`).
- CLI body: load config → `run_full_council(task)` (already returns everything needed at
  `council.py:296`) → render digest (section 4) or JSON.
- Exit code 0 on success; non-zero if all of stage 1 failed (mirrors current handling).

### b) Headless daemon (optional)

- Keep FastAPI but a single endpoint: `POST /council` with body `{"task": "..."}` returning
  the same JSON shape as the CLI `--json` output.
- Drop conversation storage entirely unless the user wants a JSONL audit log of requests.
- Run under `systemd` (a simple unit file) so it is a real service.
- CORS config, conversation routes in `main.py`, and `backend/storage.py` can be removed.

## 4. Output format

Human-readable digest (CLI default), rendered from `run_full_council` return values:

```
COUNCIL VERDICT — "How do I implement RCU in C?"

Aggregate peer rankings (avg position, #votes):
  1. fireworks/llama-v3p3-70b-instruct   avg 1.75   (4 votes)   ← RECOMMENDED SOLUTION
  2. ollama/qwen3:32b                    avg 2.25   (4 votes)
  3. anthropic/claude-sonnet-4-5         avg 2.50   (3 votes)
  4. mistral/mistral-large-latest        avg 3.50   (4 votes)

RECOMMENDED SOLUTION (raw stage-1 response from #1):
  <full text>

CHAIRMAN SYNTHESIS:
  <full text>

FAILURES: <list of models that failed any stage, if any>
```

- The "recommended solution" falls out for free: `aggregate_rankings[0]` → de-anonymize via
  `label_to_model` → pull that model's stage-1 text. No new pipeline logic.
- `--json` shape:
  ```json
  {
    "task": "...",
    "rankings": [
      {"model": "...", "provider": "...", "average_rank": 1.75, "rankings_count": 4}
    ],
    "recommended": [{"model": "...", "response": "..."}],
    "stage1": [{"model": "...", "response": "..."}],
    "stage2": [{"model": "...", "ranking": "...", "parsed_ranking": [...]}],
    "synthesis": "..."
  }
  ```

## 5. Gotchas specific to this setup

- **Local models and the strict `FINAL RANKING:` contract** (`council.py:76-81`): smaller
  local models are the weakest at exact-output compliance. The existing fallback regex in
  `parse_ranking_from_text` helps; if ranking sections come back empty often, add a
  per-model prompt variant (e.g. "Reply using ONLY this template…") or keep the council floor
  at instruction-tuned models ≥13B.
- **Timeouts**: local inference is slow (GPU contention, large models). Make timeouts
  per-provider in `council.yaml` (e.g. 600s Ollama, 120s hosted APIs) instead of the current
  fixed 120s default in `openrouter.py:9`. A global `--timeout` CLI flag can override.
- **Anthropic `max_tokens` is required** — omitting it returns 400. Include it in the
  provider config (default 4096) so any Anthropic entry works.
- **Rate limits (429)** from hosted APIs are expected under parallel burst; the existing
  "log and continue" error policy already covers this. Consider a small retry (1 attempt,
  backoff) for 429/5xx before giving up.
- **Model ID mismatch**: council members and the chairman are no longer the same identifier
  namespace; use `provider/model` (or a custom `name`) in all output so logs and the verdict
  are readable.
- **Cost/latency semantics change**: per query = (N stage-1) + (M stage-2) + (1 chairman)
  API calls where M = models that succeeded in stage 1 (today it's `COUNCIL_MODELS` size
  regardless — a small correctness bug: models that failed stage 1 are still asked to rank
  in stage 2). Fix while refactoring: pass the stage-1 success set to stage 2.
- **Offline mode**: all-local (Ollama-only) config is possible; no network required.

## 6. File changes

| File | Change |
|---|---|
| `backend/providers.py` | **New** — `ModelEntry`, `ProviderRegistry`, two transports (~120 LOC) |
| `backend/config.py` | **Rewrite** — load `council.yaml` + `.env`, expose `COUNCIL: list[ModelEntry]`, `CHAIRMAN: ModelEntry`, timeouts |
| `backend/council.py` | Swap `str` model IDs for `ModelEntry` in stage 1/2/3 (mechanical); pass stage-1 success set into stage 2; drop `generate_conversation_title` or point it at a local model |
| `backend/openrouter.py` | **Delete** (logic folded into `providers.py`) |
| `backend/main.py` | Keep only if daemon option is wanted; otherwise **delete** |
| `backend/storage.py` | **Delete** (or keep as optional JSONL audit log behind the daemon) |
| `backend/cli.py` | **New** — argparse/typer entry, digest renderer, `--json`, `--task-file`, stdin |
| `council.yaml` | **New** — default multi-provider config |
| `pyproject.toml` | Add `typer` (or stick with argparse), `pyyaml`; add `[project.scripts] council = "backend.cli:main"`; drop unused deps |
| `.env.example` | **New** — `MISTRAL_API_KEY`, `FIREWORKS_API_KEY`, `ANTHROPIC_API_KEY` |
| `frontend/` | Not needed for service mode; keep in the repo untouched |

## 7. Out of scope

- Web frontend (stays as is, untouched by this change)
- Streaming responses
- Conversation history persistence
- Multi-turn deliberation (each invocation is a single task)
- Custom scoring criteria beyond accuracy/insight
