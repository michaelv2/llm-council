"""3-stage LLM Council orchestration (service mode).

Modes:
- "answer": original pipeline — parallel answers, anonymized peer ranking,
  chairman synthesis.
- "plan": divergence-first brainstorming. Each seat gets a randomly assigned
  angle (simplest-possible, most-robust, different-architecture) so the
  proposals differ by design, not just by model family; stage 2 critiques and
  ranks them; the chairman commits to a plan with rejected alternatives.
- "review": stage 1 reviewers emit structured issue lines (see REVIEW_FORMAT)
  after a CHECKED: line; stage 2 votes on every issue by id (REAL / FALSE
  POSITIVE / DUPLICATE) and may add NEW issues; the chairman gets the issues
  with vote counts (self-votes excluded) and returns "VERDICT: APPROVE" or
  "VERDICT: CHANGES NEEDED" plus a deduplicated list. No peer ranking.

Bug fixes vs. the web version:
- stage 2 only queries models that succeeded in stage 1 (previously, failed
  models were still asked to rank);
- the response labels (Response A, B, ...) are shuffled per run, so the same
  council seat isn't always shown first (position bias);
- a ranking is read only from the last "FINAL RANKING:" section, duplicates
  dropped; without that heading the evaluation counts as no vote (the old
  fallback counted every "Response X" mentioned in the prose).
"""

import asyncio
import random
import re
import sys
import time
from collections import defaultdict
from typing import Optional

from .providers import ModelEntry, ProviderRegistry

# --- prompts -------------------------------------------------------------------

REVIEW_FORMAT = """Report every issue as ONE line with six fields separated by " | ":
SEVERITY | KIND | LOCATION | description | evidence | check
- SEVERITY: blocker (wrong results, data loss or secret exposure in normal use) | major (wrong behavior in a realistic case, or a broken repo rule) | minor (edge case or misleading behavior) | nit (style, dead code)
- KIND: BUG (code that is present behaves wrongly) | MISSING (required behavior is absent, so there is no line to quote) | RULE (breaks a repo rule)
- LOCATION: file:line; for MISSING, the file where the behavior belongs
- evidence: the exact offending code, quoted; for MISSING, quote the acceptance criterion or rule that is not met
- check: a concrete input, command or test that would demonstrate the issue, or "none\""""

PLAN_ANGLES = {
    "simplest-possible": (
        "Propose the SIMPLEST approach that could work: fewest moving parts, "
        "least new code and dependencies. Say explicitly what you leave out and "
        "why that is acceptable."
    ),
    "most-robust": (
        "Propose the MOST ROBUST approach: how it behaves under failures, bad "
        "input, load, partial outages and recovery, and how problems are "
        "detected. Accept extra complexity where it buys reliability."
    ),
    "different-architecture": (
        "Propose an approach with a FUNDAMENTALLY DIFFERENT architecture from "
        "the obvious one: a different decomposition, data flow, tool or "
        "execution model. Name the obvious design and explain what yours does "
        "differently and when it wins."
    ),
}

STAGE1_SYSTEM = {
    "plan": (
        "You are a member of a planning council. Propose ONE distinct approach "
        "to the task below. Steelman it: concrete design, trade-offs, risks, and "
        "what you considered and rejected. Do NOT converge toward a consensus — "
        "diversity of approaches is the goal. Be specific and opinionated."
    ),
    "review": (
        "You are a reviewer on a code-review council. Check the change against "
        "the repo rules and acceptance criteria given in the task. Look for "
        "missing behavior as well as wrong code: error paths, empty or invalid "
        "input, and what other parts of the change rely on.\n\n"
        + REVIEW_FORMAT
        + "\n\nStart your reply with one line beginning \"CHECKED:\" that lists "
        "the areas you examined. Then the issue lines, nothing else. List only "
        "issues you still believe are real after checking: if you look into "
        "something and conclude it is fine, leave it out (no \"no issue\" or "
        "\"skip\" lines; every line you list is voted on). If you find no "
        "issues, write the CHECKED: line and then exactly: NO ISSUES"
    ),
    "answer": None,
}

_RANKING_FORMAT = """IMPORTANT: Your final ranking MUST be formatted EXACTLY as follows:
- Start with the line "FINAL RANKING:" (all caps, with colon)
- Then list the responses from best to worst as a numbered list
- Each line should be: number, period, space, then ONLY the response label (e.g., "1. Response A")
- Do not add any other text or explanations in the ranking section"""


def _stage2_prompt(mode: str, task: str, responses_text: str, issues_text: str = "") -> str:
    if mode == "review":
        return f"""You are validating issue reports from other reviewers of the following change:

Task: {task}

The anonymized reviewer reports:

{responses_text}

The issues to judge, by id:

{issues_text or "(no issues were reported)"}

For EVERY issue id above, write one line:
ID | REAL or FALSE POSITIVE or DUPLICATE of <ID> | one-line justification quoting the code or criterion
Judge each claim against the change itself, not against the reviewer's wording.

Then, if the reviewers missed a real issue, add it as a line starting with "NEW | " followed by the six-field format (only issues you believe are real):
{REVIEW_FORMAT}

Write nothing else."""
    if mode == "plan":
        return f"""You are evaluating anonymized proposals for the following task:

Task: {task}

Each proposal was written from a deliberately assigned angle (shown with it), so they are meant to differ. Proposals:

{responses_text}

For each proposal: what would make it fail in practice? What is missing? Which assumption could be wrong? Which of its ideas are worth keeping even if the proposal as a whole is not chosen? Then rank the proposals by risk-adjusted expected value.

{_RANKING_FORMAT}

Now provide your evaluation and ranking:"""
    return f"""You are evaluating different responses to the following question:

Question: {task}

Here are the responses from different models (anonymized):

{responses_text}

Your task:
1. First, evaluate each response individually. For each response, explain what it does well and what it does poorly.
2. Then, at the very end of your response, provide a final ranking.

{_RANKING_FORMAT}

Example of the correct format for your ENTIRE response:

Response A provides good detail on X but misses Y...
Response B is accurate but lacks depth on Z...
Response C offers the most comprehensive answer...

FINAL RANKING:
1. Response C
2. Response A
3. Response B

Now provide your evaluation and ranking:"""


def _stage3_prompt(
    mode: str, task: str, stage1_text: str, stage2_text: str, issues_text: str = ""
) -> str:
    base = (
        f"Original task:\n{task}\n\n"
        f"STAGE 1 - Individual responses:\n{stage1_text}\n\n"
        f"STAGE 2 - Peer evaluations:\n{stage2_text}\n\n"
    )
    if mode == "plan":
        return base + """You are the chairman of a planning council. The proposals were written from different assigned angles. Commit to ONE plan (you may combine ideas from several proposals; say which):
1. Chosen approach and why (weigh the peer critiques, not just the raw proposals)
2. Rejected alternatives and why
3. Concrete first steps
4. Risks, and how to detect early that the plan is wrong"""
    if mode == "review":
        return base + f"""ISSUES WITH PEER VOTES (votes exclude the issue's own author; NEW issues were raised in stage 2 and are unvoted):
{issues_text or "(none)"}

You are the chairman of a review council. The implementer is NOT on this council.
Start your response with exactly one line: "VERDICT: APPROVE" or "VERDICT: CHANGES NEEDED".
Then the deduplicated issue list, most severe first, in the same six-field format:
{REVIEW_FORMAT}
Keep issues that the votes support or that you can confirm from the change yourself; drop false positives and merge duplicates. You may add an issue you find yourself.
Account for EVERY issue id above: after the list, write a line "DROPPED:" followed by one line per id you did not keep:
ID | FALSE POSITIVE or DUPLICATE of <ID> or NOT CONFIRMED | one-line reason
Dropping an issue that a peer voted REAL, or one with no votes, needs a reason grounded in the change.
If the reviewers' CHECKED: lines leave an important area of the change unexamined, add one line "UNCHECKED: <areas>" at the end.
If no real issues remain, return VERDICT: APPROVE with no list (but still the DROPPED: section if any issues were reported)."""
    return base + """You are the Chairman of an LLM Council. Multiple AI models have provided responses to a user's question, and then ranked each other's responses. Synthesize all of this into a single, comprehensive, accurate answer to the original task. Consider the individual responses, the peer rankings, and any patterns of agreement or disagreement. Provide a clear, well-reasoned final answer that represents the council's collective wisdom:"""


# --- parsing -------------------------------------------------------------------

_SEVERITY = r"(blocker|major|minor|nit)"
# a leading "|" allows markdown table rows ("| major | BUG | ... |")
_ISSUE_RE = re.compile(
    r"^[ \t]*\|?[ \t]*(?:[-*]|\d+[.)])?[ \t]*\**" + _SEVERITY + r"\**[ \t]*\|(.*)$", re.I | re.M
)
_NEW_RE = re.compile(
    r"^[ \t]*\|?[ \t]*(?:[-*][ \t]*)?\**NEW\**[ \t]*\|[ \t]*" + _SEVERITY + r"[ \t]*\|(.*)$",
    re.I | re.M,
)
_VOTE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\**([A-Z]\d+|N\d+)\**\s*[|:]\s*\**(REAL|FALSE POSITIVE|DUPLICATE)",
    re.I | re.M,
)


def _split_fields(rest: str, maxsplit: int) -> list[str]:
    """Split on "|" outside `backtick` spans (evidence often quotes a shell
    pipeline); unbalanced backticks fall back to a plain split."""
    rest = rest.strip()
    if rest.endswith("|"):  # markdown table row
        rest = rest[:-1]
    if rest.count("`") % 2:
        return [p.strip() for p in rest.split("|", maxsplit)]
    parts, current, in_code = [], [], False
    for ch in rest:
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code and len(parts) < maxsplit:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    parts.append("".join(current).strip())
    return parts


def _issue_fields(severity: str, rest: str) -> dict:
    parts = _split_fields(rest, 4)
    parts += [""] * (5 - len(parts))
    kind, location, description, evidence, check = parts
    return {
        "severity": severity.lower(),
        "kind": kind.upper(),
        "location": location,
        "description": description,
        "evidence": evidence,
        "check": check,
    }


def parse_issues(text: str, prefix: str) -> list[dict]:
    """Issue lines from a stage-1 review, with ids prefix+1, prefix+2, ..."""
    if not isinstance(text, str):
        return []
    return [
        {"id": f"{prefix}{n}", **_issue_fields(m.group(1), m.group(2))}
        for n, m in enumerate(_ISSUE_RE.finditer(text), start=1)
    ]


def parse_new_issues(text: str) -> list[dict]:
    """NEW issue lines raised by a stage-2 validator (ids assigned by the caller)."""
    if not isinstance(text, str):
        return []
    return [_issue_fields(m.group(1), m.group(2)) for m in _NEW_RE.finditer(text)]


def parse_votes(text: str) -> dict[str, str]:
    """{issue id: REAL | FALSE POSITIVE | DUPLICATE}; the last vote per id wins."""
    if not isinstance(text, str):
        return {}
    return {m.group(1).upper(): m.group(2).upper() for m in _VOTE_RE.finditer(text)}


_DROPPED_LINE_RE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?\**([A-Za-z]\d+)\**[ \t]*\|(.*)$")


def parse_dropped(synthesis: str) -> list[dict]:
    """The chairman's DROPPED: section -> [{id, verdict, reason}] (reason may be empty)."""
    if not isinstance(synthesis, str) or "DROPPED:" not in synthesis:
        return []
    section = synthesis.rsplit("DROPPED:", 1)[1].split("UNCHECKED:", 1)[0]
    dropped = []
    for line in section.splitlines():  # one entry per line, never across lines
        m = _DROPPED_LINE_RE.match(line)
        if m:
            verdict, _, reason = m.group(2).partition("|")
            dropped.append({"id": m.group(1).upper(), "verdict": verdict.strip().upper(),
                            "reason": reason.strip()})
    return dropped


def parse_ranking_from_text(ranking_text: str) -> list[str]:
    """Parse the FINAL RANKING section from a model's response.

    Reads the LAST "FINAL RANKING:" section (models sometimes echo the heading
    from the instructions earlier on), prefers its numbered list, and keeps each
    label once. Without the heading it returns [] (no vote): the labels in the
    prose are discussion order, not a ranking.
    """
    if not isinstance(ranking_text, str) or "FINAL RANKING:" not in ranking_text:
        return []
    section = ranking_text.rsplit("FINAL RANKING:", 1)[1]
    labels = re.findall(r"\d+\.\s*(Response [A-Z])\b", section)
    if not labels:
        labels = re.findall(r"\bResponse [A-Z]\b", section)
    return list(dict.fromkeys(labels))


def _issue_line(issue: dict) -> str:
    return " | ".join(
        issue[k] for k in ("severity", "kind", "location", "description", "evidence", "check")
    )


def _issues_with_votes_text(issues: list[dict]) -> str:
    lines = []
    for i in issues:
        if "votes" in i:
            v = i["votes"]
            tally = (f"[votes: REAL {v['REAL']}, FALSE POSITIVE {v['FALSE POSITIVE']}, "
                     f"DUPLICATE {v['DUPLICATE']}]")
        else:
            tally = "[NEW, unvoted]"
        lines.append(f"{i['id']} {tally} {_issue_line(i)}")
    return "\n".join(lines)


# --- stages --------------------------------------------------------------------

def assign_angles(council: list[ModelEntry], rng=None) -> dict[str, str]:
    """Randomly give each seat a different plan angle (cycling if seats > angles)."""
    angles = list(PLAN_ANGLES)
    (rng or random).shuffle(angles)
    return {e.name: angles[i % len(angles)] for i, e in enumerate(council)}


async def stage1_collect_responses(
    registry: ProviderRegistry,
    council: list[ModelEntry],
    task: str,
    mode: str = "answer",
    timeout: Optional[float] = None,
    roles: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Stage 1: collect individual responses from all council models.

    roles (plan mode) maps entry name -> angle; each seat then gets its own
    system prompt.
    """
    system = STAGE1_SYSTEM.get(mode)

    def messages_for(entry: ModelEntry) -> list[dict]:
        text = system
        if roles and entry.name in roles:
            angle = roles[entry.name]
            text = f"{system}\n\nYour assigned angle: {angle}. {PLAN_ANGLES[angle]}"
        msgs = [{"role": "system", "content": text}] if text else []
        return msgs + [{"role": "user", "content": task}]

    if roles:
        results = await asyncio.gather(
            *[registry.query(e, messages_for(e), timeout) for e in council]
        )
        responses = {e.name: r for e, r in zip(council, results)}
    else:
        responses = await registry.query_parallel(council, messages_for(council[0]), timeout)

    out = []
    for name, r in responses.items():
        if r is None:
            continue
        content = r.get("content") or ""
        if not content.strip():
            print(f"[council] {name} returned an empty response; counted as failed", file=sys.stderr)
            continue
        if mode == "review" and "CHECKED:" not in content:
            # the format requires a CHECKED: line first; without it the reply
            # is not a review (and NO ISSUES from it would mean nothing)
            print(f"[council] {name} reply has no CHECKED: line; counted as failed", file=sys.stderr)
            continue
        item = {"model": name, "response": content}
        if roles and name in roles:
            item["role"] = roles[name]
        out.append(item)
    return out


async def stage2_collect_rankings(
    registry: ProviderRegistry,
    council: list[ModelEntry],
    task: str,
    stage1_results: list[dict],
    mode: str = "answer",
    timeout: Optional[float] = None,
    rng: Optional[random.Random] = None,
):
    """Stage 2: council models that succeeded in stage 1 evaluate the anonymized responses.

    The responses are shown in a shuffled order (pass rng to make it reproducible),
    so no seat systematically benefits from being read first. Returns
    (stage2_results, label_to_model, issues); issues is [] outside review mode.
    """
    shuffled = list(stage1_results)
    (rng or random).shuffle(shuffled)
    labels = [chr(65 + i) for i in range(len(shuffled))]  # A, B, C, ...
    label_to_model = {
        f"Response {label}": result["model"]
        for label, result in zip(labels, shuffled)
    }

    def header(label, result):
        role = f" (angle: {result['role']})" if result.get("role") else ""
        return f"Response {label}{role}:\n{result['response']}"

    responses_text = "\n\n".join(header(lb, r) for lb, r in zip(labels, shuffled))

    issues: list[dict] = []
    if mode == "review":
        for label, result in zip(labels, shuffled):
            for issue in parse_issues(result["response"], label):
                issues.append({**issue, "reviewer": result["model"]})
    issues_text = "\n".join(f"{i['id']}: {_issue_line(i)}" for i in issues)
    messages = [{"role": "user", "content": _stage2_prompt(mode, task, responses_text, issues_text)}]

    # Bug fix: only models that succeeded in stage 1 participate in stage 2.
    succeeded = {r["model"] for r in stage1_results}
    voters = [e for e in council if e.name in succeeded]

    # Review: votes on one's own issues are ignored, so with no issues or a
    # single reviewer there is nothing to vote on; skip the (slow) stage.
    if mode == "review" and (not issues or len(voters) < 2):
        return [], label_to_model, tally_votes(issues, [])

    responses = await registry.query_parallel(voters, messages, timeout)

    stage2_results = []
    for name, r in responses.items():
        if r is None:
            continue
        text = r.get("content") or ""
        item = {"model": name, "ranking": text}
        if mode == "review":
            item["votes"] = parse_votes(text)
            item["parsed_ranking"] = []
        else:
            item["parsed_ranking"] = parse_ranking_from_text(text)
        stage2_results.append(item)

    if mode == "review":
        issues = tally_votes(issues, stage2_results)
    return stage2_results, label_to_model, issues


def tally_votes(issues: list[dict], stage2_results: list[dict]) -> list[dict]:
    """Attach vote counts to each issue (a reviewer's votes on its own issues are
    ignored) and append the NEW issues raised in stage 2."""
    tallied = []
    for issue in issues:
        votes = {"REAL": 0, "FALSE POSITIVE": 0, "DUPLICATE": 0}
        for r in stage2_results:
            if r["model"] == issue["reviewer"]:
                continue
            verdict = r.get("votes", {}).get(issue["id"])
            if verdict in votes:
                votes[verdict] += 1
        tallied.append({**issue, "votes": votes})
    n = 0
    for r in stage2_results:
        for new in parse_new_issues(r["ranking"]):
            n += 1
            tallied.append({"id": f"N{n}", **new, "reviewer": r["model"]})
    return tallied


async def stage3_synthesize_final(
    registry: ProviderRegistry,
    chairman: ModelEntry,
    task: str,
    stage1_results: list[dict],
    stage2_results: list[dict],
    mode: str = "answer",
    timeout: Optional[float] = None,
    issues: Optional[list[dict]] = None,
) -> dict:
    """Stage 3: chairman synthesizes the final response."""
    def s1(r):
        role = f" (angle: {r['role']})" if r.get("role") else ""
        return f"Model: {r['model']}{role}\nResponse: {r['response']}"

    stage1_text = "\n\n".join(s1(r) for r in stage1_results)
    stage2_text = "\n\n".join(
        f"Model: {r['model']}\nEvaluation: {r['ranking']}" for r in stage2_results
    )
    issues_text = _issues_with_votes_text(issues or [])
    messages = [{"role": "user", "content": _stage3_prompt(
        mode, task, stage1_text, stage2_text, issues_text
    )}]

    response = await registry.query(chairman, messages, timeout)
    if response is None:
        # "failed" lets callers (cli.py) report the run as unsuccessful
        return {
            "model": chairman.name,
            "response": "Error: Unable to generate final synthesis.",
            "failed": True,
        }
    content = response.get("content") or ""
    if not content.strip():
        # seen live: a large stage-3 prompt can yield an empty text part
        return {
            "model": chairman.name,
            "response": "Error: chairman returned an empty response.",
            "failed": True,
        }
    return {"model": chairman.name, "response": content}


def calculate_aggregate_rankings(
    stage2_results: list[dict],
    label_to_model: dict[str, str],
) -> list[dict]:
    """Average rank position per model across all peer evaluations, best first."""
    model_positions = defaultdict(list)
    for ranking in stage2_results:
        for position, label in enumerate(ranking["parsed_ranking"], start=1):
            if label in label_to_model:
                model_positions[label_to_model[label]].append(position)

    aggregate = [
        {
            "model": model,
            "average_rank": round(sum(positions) / len(positions), 2),
            "rankings_count": len(positions),
        }
        for model, positions in model_positions.items()
        if positions
    ]
    aggregate.sort(key=lambda x: x["average_rank"])
    return aggregate


async def run_full_council(
    task: str,
    registry: ProviderRegistry,
    council: list[ModelEntry],
    chairman: ModelEntry,
    mode: str = "answer",
    timeout: Optional[float] = None,
    rng: Optional[random.Random] = None,
    angles: bool = True,
):
    """Run the complete 3-stage council process.

    angles: in plan mode, give each seat a random angle (PLAN_ANGLES).
    Returns (stage1_results, stage2_results, stage3_result, metadata); metadata
    has the mode, label_to_model, aggregate_rankings, issues (review), roles
    (plan) and per-stage wall-clock timings.
    """
    roles = assign_angles(council, rng) if (mode == "plan" and angles) else {}
    timings = {}
    t = time.monotonic()
    stage1_results = await stage1_collect_responses(
        registry, council, task, mode, timeout, roles or None
    )
    timings["stage1_s"] = round(time.monotonic() - t, 1)

    if not stage1_results:
        return [], [], {
            "model": "error",
            "response": "All models failed to respond. Please try again.",
            "failed": True,
        }, {"mode": mode, "label_to_model": {}, "aggregate_rankings": [],
            "issues": [], "roles": roles, "timings": timings}

    t = time.monotonic()
    stage2_results, label_to_model, issues = await stage2_collect_rankings(
        registry, council, task, stage1_results, mode, timeout, rng
    )
    timings["stage2_s"] = round(time.monotonic() - t, 1)
    aggregate_rankings = calculate_aggregate_rankings(stage2_results, label_to_model)

    t = time.monotonic()
    stage3_result = await stage3_synthesize_final(
        registry, chairman, task, stage1_results, stage2_results, mode, timeout, issues
    )
    timings["stage3_s"] = round(time.monotonic() - t, 1)

    metadata = {
        "mode": mode,
        "label_to_model": label_to_model,
        "aggregate_rankings": aggregate_rankings,
        "issues": issues,
        "roles": roles,
        "timings": timings,
    }
    if mode == "review":
        # the chairman's DROPPED: section, so a discarded true issue stays visible
        metadata["dropped"] = parse_dropped(stage3_result.get("response", ""))
    return stage1_results, stage2_results, stage3_result, metadata
