# VERIFIED — network facts, live model ids, and end-to-end results

## How Vibe reaches Mistral "with no network"

Two layers:

1. **Vibe itself runs on the host, outside this container.** The model
   connection (the conversation you are reading) is made by the host Vibe
   process to api.mistral.ai using the key in `~/.vibe/.env` on the host.
   The agentsafe container is only the sandbox where my *tool calls*
   (bash, file ops, run_typescript) execute — inference traffic never
   passes through it. So the container's restrictions never applied to the
   model connection.
2. **The container's bash actually has internet egress.** Measured:
   - https://example.com -> 200, https://pypi.org -> 200
   - https://api.mistral.ai/v1/models -> 401 without auth (reachable)
   - `MISTRAL_API_KEY` is set in the container env (existence checked only;
     value never read or printed)
   - Local LAN is blocked: http://qwen-lan:11434 -> unreachable (000)
   - http://llm-server:11434 (Ollama) -> 200
   - The `run_typescript` sandbox is the truly network-less one (no fetch).

## Live model ids (pulled from the APIs, key used as bearer only)

Mistral API (`GET /v1/models`), relevant subset:
- `mistral-large-4` (also `mistral-large-4-0`, `mistral-large-latest`)
- `deepseek-v4-1-flash-1017`
- `zai-glm-5-3`, `zai-glm-5-3-flash`, `zai-glm-5-2`, `zai-glm-5`  ← note the `zai-` prefix
- (no unprefixed `glm-5-3` — council.yaml uses the `zai-` ids)

Ollama on llm-server (`GET /api/tags`): `qwen3.8:27b-128k`, `gemma3:27b`,
`gemma4:31b`, `muse-glimmer:30b-128k`, `phi3:3.8b`

## Bugs found by live testing (fixed in this bundle)

1. **Mistral reasoning models return `message.content` as a list of typed
   parts**, not a string (`[{"type":"thinking",...},{"type":"text",...}]`),
   which crashed `parse_ranking_from_text` with TypeError. Fixed by
   `_message_content()` in providers.py (extracts text parts, maps thinking
   blocks to `reasoning_details`); parser also guards non-str input.
   Regression tests added (15/15 pass).
2. **Diagnostics printed to stdout**, corrupting `--json` output. Now stderr.
3. **Chairman timeout**: mistral-large-4 on a large stage-3 prompt exceeded
   the 120s default (empty-message TimeoutError). Mistral timeout raised to
   300s in both yamls; `--timeout` overrides per run; error logs now include
   the exception type name.

## End-to-end live runs (all exit 0, in this container)

- `answer` mode, smoke council (ollama/phi3:3.8b + deepseek-v4-1-flash-1017
  + zai-glm-5-3-flash, chairman zai-glm-5-3-flash): all answered "4", peer
  rankings computed, synthesis OK.
- `review` mode, same council, planted bug (`return a / (b - 1)`): all three
  reviewers flagged it with file:line evidence; chairman returned
  `VERDICT: CHANGES NEEDED` with a deduplicated severity-ordered issue list.
- `plan` mode, qwen council (ollama/qwen3.8:27b-128k + zai-glm-5-3-flash,
  chairman mistral-large-4, --timeout 600): two divergent proposals
  (11.3k / 6.2k chars), rankings, and a committed chairman plan. Took ~6 min
  wall clock, dominated by the local 27B model.

## Environment used for the live runs

Container venv `/home/claude/venvs/fintools` with user-approved installs:
httpx 0.28.1, pyyaml 6.0.4, python-dotenv 1.2.4. Config: council.test.yaml
(ollama -> llm-server:11434). MISTRAL_API_KEY read from env by the code only.

## Not yet verified

- Ollama on the actual qwen-lan host (unreachable from this container).
- Anthropic / Fireworks transports (no keys here; code-reviewed only).
- The web frontend (untouched, out of scope for service mode).

## Pricing (per 1M tokens, input / output; from live pricing pages)

Mistral API (docs.mistral.ai/models/pricing):
- zai-glm-5-3:        $1.40 / $4.40   (cached $0.14)
- zai-glm-5-3-flash:  $0.15 / $0.50   (cached $0.03)
- deepseek-v4-1-flash-1017: $0.30 / $1.20 (cached $0.007)
- mistral-large-4:    $0.70 / $2.10   (cached $0.07)

Fireworks (fireworks.ai/pricing, on-demand):
- minimax-m3 (accounts/fireworks/models/minimax-m3): $0.30 / $1.20 (cached $0.06)
- GLM 5.3: $1.40 / $4.40; GLM 5.3 Flash: $0.15 / $0.50
- Qwen 3.8 27B 128K: $1.86 / $5.60; GPT OSS 120B: $0.15 / $0.60; Nemotron 3.5 Lightning 30B: $0.05 / $0.20

Estimated cost per council run (task ~2k tok, plan proposal ~4k, stage-2
input ~14k; 7 calls per plan run, 5 per review run):
- plan run, glm-5-3 member: ~$0.08; with minimax-m3: ~$0.05; with glm-flash: ~$0.04
- review run, glm-5-3 chairman: ~$0.03 (chairman ~$0.02 of it); glm-flash chairman: ~$0.012

Decision: plan council uses minimax-m3 (Fireworks) instead of zai-glm-5-3 —
same price class as deepseek-flash, different family. Default council now
requires FIREWORKS_API_KEY (fail-fast); comment out the fireworks block +
minimax entry if no key yet.
