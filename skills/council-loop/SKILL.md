---
name: council-loop
description: Run the LLM council as an in-session skill loop — subagents pinned to different models act as council members, so no external service or CLI is needed. Load when you want council-plan / council-review deliberation and the council agent profiles (agents/*.toml in the llm-council repo, installed to ~/.vibe/agents/) plus config.snippet.toml are set up.
---

# council-loop

Runs the 3-stage council entirely inside this Vibe session, using subagents
as council members. No daemon, no `council` CLI invocation.

## Setup (once)

```bash
cp /path/to/llm-council/agents/*.toml ~/.vibe/agents/
# merge /path/to/llm-council/config.snippet.toml into ~/.vibe/config.toml
# (providers: mistral + ollama; model aliases: large4, deepseek-flash,
#  glm-flash, glm, qwen-local)
```

## Procedure

Stage prompts are in `prompts.md` next to this file (mirrors
`backend/council.py` in the llm-council repo).

1. **Stage 1** — spawn one subagent per council member, each with the task
   plus the mode's stage-1 system text as its first message:
   - plan mode: `council-qwen`, `council-deepseek`, `council-glm-flash`, `council-glm`
   - review mode: `council-deepseek`, `council-glm-flash` (hot path,
     flash-only; **never the implementer's model**)
   Wait for all of them; collect each response.
2. **Stage 2** — anonymize the responses as Response A, B, C... Send each
   stage-1 member the stage-2 prompt (prompts.md) containing the anonymized
   responses. Wait; collect. Parse each `FINAL RANKING:`; aggregate = average
   position per member.
3. **Stage 3** — spawn the chairman (`chairman-large4` for plan,
   `review-chairman` for review) with the task + all stage-1 responses +
   all stage-2 evaluations. Wait; its reply is the verdict/synthesis.

## Output

Return to the caller: the chairman's verdict (review: the `VERDICT:` line +
deduplicated issue list), each member's raw stage-1 response, and the
aggregate ranking. For review mode, CHANGES NEEDED feeds back as the next
task — max 2 iterations, then escalate to the user.

## Gotchas

- Everything flows through this session's context — the loop costs more
  parent-context tokens than the CLI variant. Parent context grows by roughly
  (N+1)*D + N*N*R (N = members, D = artifact tokens, R = avg response tokens),
  so the cost is quadratic in council size. Sizing rule: keep the loop for
  artifacts up to ~15-20k tokens (~60-80 KB diff) with 3-4 members; beyond
  that use the CLI variant (`council --json > out.json`, read only the
  verdict). Regardless of variant, chunk reviews per-file/per-commit once an
  artifact exceeds a single reviewer's context window (~30-50k tokens), and
  exclude generated files/lockfiles. Warning signs: context gauge past ~50-60%
  mid-review, compaction firing during the loop, reviewers reporting a
  truncated diff.
- Subagent instructions are capped at 65,536 chars; approvals apply
  session-wide.
- If a member fails or hangs, proceed with the responses you have (the
  council degrades gracefully); note the failure in the output.
