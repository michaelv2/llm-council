# DESIGN — llm-council service mode + dev-cycle skills

Decisions from the design session (agentsafe container, fintools session).
This file is the durable record; code lives alongside it in this bundle.

## Target workflow

1. User delegates a task: a long-running list (e.g. research studies) or an
   open-ended goal ("keep finding improvements, notify me only when
   meaningful" — never "done").
2. Orchestrator + main-loop chairman = **Mistral Large 4** (via Mistral API).
   It runs the loop and calls `council-plan` when brainstorming is needed.
3. Once a plan is decided, the **implementer is also Mistral Large 4**, but in
   a **fresh context per task** (plan + repo as input, not the whole
   deliberation history — avoids anchoring, keeps the orchestrator's long
   context clean).
4. After implementation, call `council-review`; feed CHANGES NEEDED back as
   the next task (max 2 review iterations, then escalate to the user).

## Role -> model mapping

| Role | Model | Rationale |
|---|---|---|
| Main-loop orchestrator / plan chairman | mistral-large-4 | Open-ended loop needs broad judgment ("is this finding worth notifying?") + large context; small models lose the thread under compaction. |
| Implementer ("doer") | mistral-large-4, fresh context per task | koderz lesson: intelligence must live with the agent doing the work, or the large model rewrites a small model's output token-by-token. |
| council-plan reviewers (COLD path) | qwen3.8:27b-128k (local) + deepseek-v4-1-flash-1017 + zai-glm-5-3-flash (Mistral) | One seat per model family. Vendor surface kept to Mistral + local Ollama only (Fireworks/minimax-m3 was tried and removed); glm-5-3 full stays out of members on cost ($1.40/$4.40 per 1M) and remains review chairman. Local model kept because plan runs are periodic and latency is tolerable. |
| council-review reviewers (HOT path) | deepseek-v4-1-flash-1017 + zai-glm-5-3-flash only | Review runs every iteration; the local 27B takes minutes per call and `asyncio.gather` waits for the slowest entry, so it would gate every stage of every run. Flash-only = seconds. (Optional local seat: small model like phi3:3.8b, or per-entry `timeout: 120` so a slow call is skipped, not gating.) |
| council-review chairman | zai-glm-5-3 | **Must not be the implementer.** |

## Hard rules

- **The implementer model never sits in the review council** — not even as
  chairman. Stage 3 de-anonymizes model names, so a Large 4 chairman would
  know which code is its own; familiarity bias is exactly what review exists
  to escape. Enforced by keeping `councils.review` free of the implementer in
  `council.yaml`; do not add it.
- **Orchestrator size is per task shape, not a global rule.** Open-ended
  loops -> Large 4 (judgment + context). Well-specified batches of similar
  items -> qwen3.8:27b as cheap dispatcher routing to Large 4 doers (koderz
  shape).
- **Council membership is split by cadence, not just by role.** Hot loops
  (review) get fast models only; cold loops (plan) can afford the slow local
  model. A slow entry gates its whole stage (gather waits for the slowest),
  so latency belongs in the config decision.
- Review issues must carry evidence (file:line + quoted code); stage 2 of the
  review council filters FALSE POSITIVE / DUPLICATE.
- Review loop cap: 2 iterations; split council verdicts escalate to the user.

## Open-ended loop mechanics

- Persistent **findings log** (structured: finding, severity, status, rejected
  ideas + reasons) in the repo or scratchpad; each iteration starts from the
  log + current diff, never from raw conversation history.
- Notification = severity >= explicit threshold (calibrate once).
- Ideation engine = periodic `council-plan` run ("given the system and the
  findings log, what should we try next, what are we missing?"), every K
  iterations, Large 4 chairing.

## Council modes (implemented in backend/council.py)

- `answer` — original Karpathy pipeline (parallel answers, anonymized peer
  ranking, chairman synthesis).
- `plan` — divergence-first: stage 1 proposes distinct approaches (no
  convergence), stage 2 critiques, chairman commits to a plan with rejected
  alternatives. Implementer sees raw stage-1 proposals too, not just the
  synthesis.
- `review` — stage 1 emits evidence-backed issue lists, stage 2 validates
  (REAL / FALSE POSITIVE / DUPLICATE), chairman returns
  `VERDICT: APPROVE` / `VERDICT: CHANGES NEEDED` + deduped issue list.

## Bug fixes vs. the web version (from SPEC.md gotchas) + live testing

- Stage 2 now only queries models that succeeded in stage 1 (previously
  failed models were still asked to rank).
- `generate_conversation_title` dropped (web-UI concern; hardcoded an
  OpenRouter model id).
- Per-provider timeouts from council.yaml; one retry with backoff on
  429/5xx; `--timeout` CLI override.
- API keys only from env (`key_env`), never in YAML; missing key for an
  in-use provider fails fast at startup (except Ollama).
- Live-test fixes (see VERIFIED.md): Mistral reasoning models return
  `content` as a list of typed parts (normalized in `_message_content`);
  diagnostics go to stderr so `--json` stdout stays clean; mistral timeout
  300s for large stage-3 prompts.

## Cost shape

Large 4 tokens only for orchestration, implementation, plan synthesis.
Review volume runs on cheap Mistral flash models (hot path, local model
excluded for latency). Per council run: N stage-1 + M stage-2 (M = stage-1
successes) + 1 chairman.

## Decisions log

- **HTTP client: stay on httpx 0.28.1 for now** (decided; bundle verified live
  with it). httpx2==2.16.2 (pydantic's official continuation, rename-only
  breaking change) is the drop-in future option: one-line import swap +
  `httpx2>=2.16` + `requires-python>=3.11`.
- **Local model out of the review council** (hot path), kept in the plan
  council (cold path) — latency gates every stage via gather.
- Ollama base URL: `http://llm-server:11434/v1`, on the host and inside the
  agentsafe container alike (`qwen-lan` never resolved).

## Durability protocol (this container is throwaway)

- Bundle lives in the session scratchpad (`llm-council-service/`) AND in the
  host-side transcript (every file written is visible there).
- Scratchpad survives summarization within the session but is per-session;
  the transcript on the host is the durable copy.
- To apply: run `apply.sh /path/to/llm-council` on the host (see README.md).
