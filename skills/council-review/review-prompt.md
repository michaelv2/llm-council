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

Each issue on its own line:

SEVERITY (blocker|major|minor|nit) | file:line | one-line description | quoted evidence

If you find no issues, reply exactly: NO ISSUES
