# Review task template

Copy this file, substitute the {{PLACEHOLDERS}}, and pass it to the council
with `--mode review --council review --task-file`.

---

You are reviewing a proposed change. Report issues ONLY with evidence.

## The change (diff / artifact)

{{DIFF}}

## Repo rules to check against

{{RULES}}

## Acceptance criteria for this task

{{CRITERIA}}

## Output format

First a line starting `CHECKED:` listing the areas you examined. Then each
issue on its own line, six fields:

SEVERITY | KIND | LOCATION | description | evidence | check

- SEVERITY: blocker | major | minor | nit
- KIND: BUG (present code is wrong) | MISSING (required behavior is absent) | RULE (breaks a repo rule)
- LOCATION: file:line (for MISSING, the file where it belongs)
- evidence: quoted code; for MISSING, the criterion or rule that is not met
- check: an input, command or test that would demonstrate it, or "none"

List only issues you still believe are real after checking; leave out
anything you looked into and found fine (every listed line is voted on).
If you find no issues, write the CHECKED: line, then exactly: NO ISSUES
