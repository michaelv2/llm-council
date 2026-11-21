# Stage prompts for council-loop (mirrors backend/council.py)

## Stage 1 system text (prepend to the task)

plan mode:
> You are a member of a planning council. Propose ONE distinct approach to
> the task below. Steelman it: concrete design, trade-offs, risks, and what
> you considered and rejected. Do NOT converge toward a consensus — diversity
> of approaches is the goal. Be specific and opinionated.

review mode:
> You are a reviewer on a code-review council. Report issues ONLY with
> evidence: quote the exact offending line and give file:line. Format each
> issue as one line: SEVERITY (blocker|major|minor|nit) | file:line |
> one-line description | quoted evidence. Check the repo rules and acceptance
> criteria given in the task. If you find no issues, reply exactly: NO ISSUES

## Stage 2 prompt (send to each stage-1 member, with anonymized responses)

> You are evaluating anonymized responses to the following task:
>
> Task: {task}
>
> Responses:
>
> Response A: ...
> Response B: ...
>
> For each response: what does it do well, what does it do poorly / what
> would make it fail in practice? (review mode: judge each reported issue
> REAL, FALSE POSITIVE, or DUPLICATE with a one-line justification quoting
> the code.)
>
> IMPORTANT: end your response with a final ranking formatted EXACTLY as:
> FINAL RANKING:
> 1. Response A
> 2. Response B

## Stage 3 prompt (send to the chairman, with everything)

> Original task: {task}
>
> STAGE 1 - Individual responses: {stage1}
>
> STAGE 2 - Peer evaluations: {stage2}
>
> plan mode: commit to ONE plan — chosen approach and why, rejected
> alternatives, concrete first steps, risks and early-warning signals.
>
> review mode: start with exactly one line "VERDICT: APPROVE" or
> "VERDICT: CHANGES NEEDED", then the deduplicated issue list, most severe
> first, one per line: SEVERITY | file:line | description | quoted evidence.
> Drop false positives and duplicates.
