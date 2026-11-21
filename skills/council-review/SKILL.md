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
- Reviewers must cite evidence (quoted code, or for a MISSING issue the
  criterion it fails) and say how to check the claim; stage 2 votes on every
  issue (REAL / FALSE POSITIVE / DUPLICATE) and can add NEW ones.
- `council.yaml` lists the implementer under `implementers`; the CLI refuses
  to run a review with it on the council or as chairman.

## How to run

1. Build the review task from `review-prompt.md` in this directory:
   substitute `{{DIFF}}` (e.g. `git diff` output or the changed file
   contents), `{{RULES}}` (the repo's AGENTS.md rules), `{{CRITERIA}}` (the
   task's acceptance criteria). Write to `/tmp/review-task.md`.
2. Run the CLI from the llm-council repo (set `COUNCIL_HOME` once, e.g. in
   `~/.vibe/.env` or your shell profile; default `/path/to/llm-council`):
   `cd "${COUNCIL_HOME:-/path/to/llm-council}" && uv run python -m backend.cli --mode review --council review --task-file /tmp/review-task.md --json > /tmp/review-out.json`
   (`uv run council ...` also works once `uv sync` has installed the script;
   `python -m backend.cli` works regardless of packaging)
   (redirect full JSON to a file; read only the verdict into context — see the
   sizing rule in `council-loop`)
3. Parse the JSON:
   - `synthesis` starts with `VERDICT: APPROVE` or `VERDICT: CHANGES NEEDED`,
     followed by the deduplicated issue list (most severe first) as
     `SEVERITY | KIND | LOCATION | description | evidence | check`, and maybe
     an `UNCHECKED:` line naming areas no reviewer examined.
   - `issues` — every stage-1 issue with its id, fields and peer `votes`
     (self-votes excluded), plus `NEW` issues from stage 2 (no votes).
   - `dropped` — the issues the chairman discarded, each with a verdict and
     reason. Skim it: in testing, a real bug was dropped on one peer's wrong
     vote. An issue with REAL votes dropped as NOT CONFIRMED is worth a look.
   - `stage1` — the raw reviewer issue lists, if you want to see what each
     model caught individually.
   - `failures` — models that failed; a review with a missing seat had fewer
     eyes on it, so say so when reporting the verdict.
4. If CHANGES NEEDED: before fixing, try each issue's `check` where it is
   cheap (run the command or input it names) and drop issues that don't
   reproduce. Feed the confirmed list back as the next implementation task. Cap at 2 review iterations; if the council still disagrees or the
   verdict is split, escalate to the user instead of looping.

## Gotchas

- Results vary between runs (in one test, both reviewers answered NO ISSUES
  on a diff with real bugs). For important changes, run the review twice and
  combine the issue lists.
- Review cost scales with artifact size — keep the diff focused; for large
  changes, review per-file or per-commit.
- If all stage-1 models fail or the chairman fails, the CLI exits 1 and
  `synthesis` is an error string — treat as "review unavailable", not as
  APPROVE.
