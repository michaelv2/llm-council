---
name: council-review
description: Load after implementing a change (code, config, docs) to run the multi-model review council over the diff before considering the task done. Returns VERDICT: APPROVE or CHANGES NEEDED with an evidence-backed, deduplicated issue list.
---

# council-review

Review a finished piece of work with the review council — fresh eyes,
cross-model skepticism. This institutionalizes the "second agent in a fresh
context finds the bugs" effect.

## Hard rules

- The review council must NEVER include the model that implemented the
  change — not even as chairman. `councils.review` in `council.yaml` is
  configured without the implementer; do not add it. A model grades its own
  output with less scrutiny (familiarity bias).
- Review the artifact, not the intention: pass the actual diff/output plus
  the repo rules (AGENTS.md) as a checklist.
- Reviewers must cite evidence (file:line + quoted code); the council's
  stage 2 filters FALSE POSITIVE / DUPLICATE.

## How to run

1. Build the review task from `review-prompt.md` in this directory:
   substitute `{{DIFF}}` (e.g. `git diff` output or the changed file
   contents), `{{RULES}}` (the repo's AGENTS.md rules), `{{CRITERIA}}` (the
   task's acceptance criteria). Write to `/tmp/review-task.md`.
2. Run from the llm-council repo:
   `uv run council --mode review --council review --task-file /tmp/review-task.md --json`
3. Parse the JSON:
   - `synthesis` starts with `VERDICT: APPROVE` or `VERDICT: CHANGES NEEDED`,
     followed by the deduplicated issue list (most severe first).
   - `stage1` — the raw reviewer issue lists, if you want to see what each
     model caught individually.
4. If CHANGES NEEDED: feed the issue list back as the next implementation
   task. Cap at 2 review iterations; if the council still disagrees or the
   verdict is split, escalate to the user instead of looping.

## Gotchas

- Review cost scales with artifact size — keep the diff focused; for large
  changes, review per-file or per-commit.
- If all stage-1 models fail, the CLI exits 1 and `synthesis` is an error
  string — treat as "review unavailable", not as APPROVE.
