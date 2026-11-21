"""3-stage LLM Council orchestration (service mode).

Modes:
- "answer": original pipeline — parallel answers, anonymized peer ranking,
  chairman synthesis.
- "plan": divergence-first brainstorming. Stage 1 proposes distinct approaches
  (explicitly no convergence), stage 2 critiques them, the chairman commits to
  a plan with rejected alternatives.
- "review": stage 1 emits evidence-backed issue lists (file:line + quoted
  code), stage 2 filters FALSE POSITIVE / DUPLICATE, the chairman returns
  "VERDICT: APPROVE" or "VERDICT: CHANGES NEEDED" plus a deduplicated list.

Bug fix vs. the web version: stage 2 now only queries models that succeeded in
stage 1 (previously, failed models were still asked to rank).
"""

import re
from collections import defaultdict
from typing import Any, Optional

from .providers import ModelEntry, ProviderRegistry

STAGE1_SYSTEM = {
    "plan": (
        "You are a member of a planning council. Propose ONE distinct approach "
        "to the task below. Steelman it: concrete design, trade-offs, risks, and "
        "what you considered and rejected. Do NOT converge toward a consensus — "
        "diversity of approaches is the goal. Be specific and opinionated."
    ),
    "review": (
        "You are a reviewer on a code-review council. Report issues ONLY with "
        "evidence: quote the exact offending line and give file:line. Format each "
        "issue as one line: SEVERITY (blocker|major|minor|nit) | file:line | "
        "one-line description | quoted evidence. Check the repo rules and "
        "acceptance criteria given in the task. If you find no issues, reply "
        "exactly: NO ISSUES"
    ),
    "answer": None,
}

_RANKING_FORMAT = """IMPORTANT: Your final ranking MUST be formatted EXACTLY as follows:
- Start with the line "FINAL RANKING:" (all caps, with colon)
- Then list the responses from best to worst as a numbered list
- Each line should be: number, period, space, then ONLY the response label (e.g., "1. Response A")
- Do not add any other text or explanations in the ranking section"""


def _stage2_prompt(mode: str, task: str, responses_text: str) -> str:
    if mode == "review":
        return f"""You are validating issue lists from other reviewers of the following change:

Task: {task}

Here are the anonymized reviewer reports:

{responses_text}

For each reported issue, judge it: REAL, FALSE POSITIVE, or DUPLICATE — with a one-line justification that quotes the code. Then evaluate each report overall: which reviewer caught the most real issues, which reports are noise.

{_RANKING_FORMAT}

Now provide your evaluation and ranking:"""
    if mode == "plan":
        return f"""You are evaluating anonymized proposals for the following task:

Task: {task}

Proposals:

{responses_text}

For each proposal: what would make it fail in practice? What is missing? Which assumption could be wrong? Then rank the proposals by risk-adjusted expected value.

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


def _stage3_prompt(mode: str, task: str, stage1_text: str, stage2_text: str) -> str:
    base = (
        f"Original task:\n{task}\n\n"
        f"STAGE 1 - Individual responses:\n{stage1_text}\n\n"
        f"STAGE 2 - Peer evaluations:\n{stage2_text}\n\n"
    )
    if mode == "plan":
        return base + """You are the chairman of a planning council. Commit to ONE plan:
1. Chosen approach and why (weigh the peer critiques, not just the raw proposals)
2. Rejected alternatives and why
3. Concrete first steps
4. Risks, and how to detect early that the plan is wrong"""
    if mode == "review":
        return base + """You are the chairman of a review council. The implementer is NOT on this council.
Start your response with exactly one line: "VERDICT: APPROVE" or "VERDICT: CHANGES NEEDED".
Then give the deduplicated issue list, most severe first, each as one line:
SEVERITY | file:line | description | quoted evidence
Drop false positives and duplicates. If every reviewer reported NO ISSUES and you agree after checking, return VERDICT: APPROVE with no list."""
    return base + """You are the Chairman of an LLM Council. Multiple AI models have provided responses to a user's question, and then ranked each other's responses. Synthesize all of this into a single, comprehensive, accurate answer to the original task. Consider the individual responses, the peer rankings, and any patterns of agreement or disagreement. Provide a clear, well-reasoned final answer that represents the council's collective wisdom:"""


async def stage1_collect_responses(
    registry: ProviderRegistry,
    council: list[ModelEntry],
    task: str,
    mode: str = "answer",
    timeout: Optional[float] = None,
) -> list[dict]:
    """Stage 1: collect individual responses from all council models."""
    messages = []
    system = STAGE1_SYSTEM.get(mode)
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": task})

    responses = await registry.query_parallel(council, messages, timeout)

    return [
        {"model": name, "response": r.get("content") or ""}
        for name, r in responses.items()
        if r is not None
    ]


async def stage2_collect_rankings(
    registry: ProviderRegistry,
    council: list[ModelEntry],
    task: str,
    stage1_results: list[dict],
    mode: str = "answer",
    timeout: Optional[float] = None,
):
    """Stage 2: council models that succeeded in stage 1 evaluate the anonymized responses."""
    labels = [chr(65 + i) for i in range(len(stage1_results))]  # A, B, C, ...
    label_to_model = {
        f"Response {label}": result["model"]
        for label, result in zip(labels, stage1_results)
    }
    responses_text = "\n\n".join(
        f"Response {label}:\n{result['response']}"
        for label, result in zip(labels, stage1_results)
    )
    messages = [{"role": "user", "content": _stage2_prompt(mode, task, responses_text)}]

    # Bug fix: only models that succeeded in stage 1 participate in stage 2.
    succeeded = {r["model"] for r in stage1_results}
    voters = [e for e in council if e.name in succeeded]

    responses = await registry.query_parallel(voters, messages, timeout)

    stage2_results = []
    for name, r in responses.items():
        if r is not None:
            text = r.get("content") or ""
            stage2_results.append({
                "model": name,
                "ranking": text,
                "parsed_ranking": parse_ranking_from_text(text),
            })
    return stage2_results, label_to_model


async def stage3_synthesize_final(
    registry: ProviderRegistry,
    chairman: ModelEntry,
    task: str,
    stage1_results: list[dict],
    stage2_results: list[dict],
    mode: str = "answer",
    timeout: Optional[float] = None,
) -> dict:
    """Stage 3: chairman synthesizes the final response."""
    stage1_text = "\n\n".join(
        f"Model: {r['model']}\nResponse: {r['response']}" for r in stage1_results
    )
    stage2_text = "\n\n".join(
        f"Model: {r['model']}\nEvaluation: {r['ranking']}" for r in stage2_results
    )
    messages = [{"role": "user", "content": _stage3_prompt(mode, task, stage1_text, stage2_text)}]

    response = await registry.query(chairman, messages, timeout)
    if response is None:
        return {
            "model": chairman.name,
            "response": "Error: Unable to generate final synthesis.",
        }
    return {"model": chairman.name, "response": response.get("content") or ""}


def parse_ranking_from_text(ranking_text: str) -> list[str]:
    """Parse the FINAL RANKING section from a model's response."""
    if not isinstance(ranking_text, str):
        return []
    if "FINAL RANKING:" in ranking_text:
        parts = ranking_text.split("FINAL RANKING:")
        if len(parts) >= 2:
            ranking_section = parts[1]
            numbered_matches = re.findall(r"\d+\.\s*Response [A-Z]", ranking_section)
            if numbered_matches:
                return [re.search(r"Response [A-Z]", m).group() for m in numbered_matches]
            return re.findall(r"Response [A-Z]", ranking_section)
    return re.findall(r"Response [A-Z]", ranking_text)


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
):
    """Run the complete 3-stage council process.

    Returns (stage1_results, stage2_results, stage3_result, metadata).
    """
    stage1_results = await stage1_collect_responses(registry, council, task, mode, timeout)

    if not stage1_results:
        return [], [], {
            "model": "error",
            "response": "All models failed to respond. Please try again.",
        }, {"mode": mode, "label_to_model": {}, "aggregate_rankings": []}

    stage2_results, label_to_model = await stage2_collect_rankings(
        registry, council, task, stage1_results, mode, timeout
    )
    aggregate_rankings = calculate_aggregate_rankings(stage2_results, label_to_model)
    stage3_result = await stage3_synthesize_final(
        registry, chairman, task, stage1_results, stage2_results, mode, timeout
    )

    metadata = {
        "mode": mode,
        "label_to_model": label_to_model,
        "aggregate_rankings": aggregate_rankings,
    }
    return stage1_results, stage2_results, stage3_result, metadata
