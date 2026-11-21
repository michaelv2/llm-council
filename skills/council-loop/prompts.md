# Stage prompts for council-loop (mirrors backend/council.py)

## Review issue format (used in stages 1-3)

> Report every issue as ONE line with six fields separated by " | ":
> SEVERITY | KIND | LOCATION | description | evidence | check
> - SEVERITY: blocker (wrong results, data loss or secret exposure in normal
>   use) | major (wrong behavior in a realistic case, or a broken repo rule) |
>   minor (edge case or misleading behavior) | nit (style, dead code)
> - KIND: BUG (code that is present behaves wrongly) | MISSING (required
>   behavior is absent, so there is no line to quote) | RULE (breaks a repo rule)
> - LOCATION: file:line; for MISSING, the file where the behavior belongs
> - evidence: the exact offending code, quoted; for MISSING, quote the
>   acceptance criterion or rule that is not met
> - check: a concrete input, command or test that would demonstrate the
>   issue, or "none"

## Stage 1 system text (prepend to the task)

plan mode (then append the member's angle, see below):
> You are a member of a planning council. Propose ONE distinct approach to
> the task below. Steelman it: concrete design, trade-offs, risks, and what
> you considered and rejected. Do NOT converge toward a consensus — diversity
> of approaches is the goal. Be specific and opinionated.

plan angles — shuffle and give each member a different one, appended as
"Your assigned angle: <name>. <text>":
- **simplest-possible**: Propose the SIMPLEST approach that could work: fewest
  moving parts, least new code and dependencies. Say explicitly what you
  leave out and why that is acceptable.
- **most-robust**: Propose the MOST ROBUST approach: how it behaves under
  failures, bad input, load, partial outages and recovery, and how problems
  are detected. Accept extra complexity where it buys reliability.
- **different-architecture**: Propose an approach with a FUNDAMENTALLY
  DIFFERENT architecture from the obvious one: a different decomposition,
  data flow, tool or execution model. Name the obvious design and explain
  what yours does differently and when it wins.

review mode:
> You are a reviewer on a code-review council. Check the change against the
> repo rules and acceptance criteria given in the task. Look for missing
> behavior as well as wrong code: error paths, empty or invalid input, and
> what other parts of the change rely on.
>
> (the review issue format above)
>
> Start your reply with one line beginning "CHECKED:" that lists the areas
> you examined. Then the issue lines, nothing else. List only issues you
> still believe are real after checking: if you look into something and
> conclude it is fine, leave it out (no "no issue" or "skip" lines; every
> line you list is voted on). If you find no issues, write the CHECKED: line
> and then exactly: NO ISSUES

## Stage 2 prompt (send to each stage-1 member, with anonymized responses)

plan mode (show each response with its angle: "Response A (angle: ...)"):
> You are evaluating anonymized proposals for the following task: {task}
>
> Each proposal was written from a deliberately assigned angle (shown with
> it), so they are meant to differ. Proposals: ...
>
> For each proposal: what would make it fail in practice? What is missing?
> Which assumption could be wrong? Which of its ideas are worth keeping even
> if the proposal as a whole is not chosen? Then rank the proposals by
> risk-adjusted expected value.
>
> IMPORTANT: end your response with a final ranking formatted EXACTLY as:
> FINAL RANKING:
> 1. Response A
> 2. Response B

review mode (number the issue lines first: A1, A2, B1, ...):
> You are validating issue reports from other reviewers of the following
> change: {task}
>
> The anonymized reviewer reports: ...
>
> The issues to judge, by id: A1: ... / B1: ...
>
> For EVERY issue id above, write one line:
> ID | REAL or FALSE POSITIVE or DUPLICATE of <ID> | one-line justification
> quoting the code or criterion
> Judge each claim against the change itself, not against the reviewer's
> wording.
>
> Then, if the reviewers missed a real issue, add it as a line starting with
> "NEW | " followed by the six-field format. Write nothing else.

## Stage 3 prompt (send to the chairman, with everything)

> Original task: {task}
>
> STAGE 1 - Individual responses: {stage1}
>
> STAGE 2 - Peer evaluations: {stage2}
>
> plan mode: the proposals were written from different assigned angles.
> Commit to ONE plan (you may combine ideas from several proposals; say
> which) — chosen approach and why, rejected alternatives, concrete first
> steps, risks and early-warning signals.
>
> review mode: also pass "ISSUES WITH PEER VOTES" — each issue with its vote
> counts (excluding its author's votes), NEW issues marked unvoted. Start
> with exactly one line "VERDICT: APPROVE" or "VERDICT: CHANGES NEEDED", then
> the deduplicated issue list, most severe first, in the six-field format.
> Keep issues the votes support or that you can confirm yourself; drop false
> positives and merge duplicates; you may add an issue you find yourself.
> Account for every issue id: after the list, a "DROPPED:" line, then one line
> per id not kept: `ID | FALSE POSITIVE or DUPLICATE of <ID> or NOT CONFIRMED |
> reason` (dropping an issue voted REAL, or an unvoted one, needs a reason
> grounded in the change). If
> the CHECKED: lines leave an important area unexamined, end with
> "UNCHECKED: <areas>". If no real issues remain, return VERDICT: APPROVE
> with no list.
