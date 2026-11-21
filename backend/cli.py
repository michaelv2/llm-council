"""One-shot CLI for the LLM Council service mode.

Usage:
    council "How do I implement RCU in C?"
    council --task-file task.md --mode plan --council default
    cat review-task.md | council --mode review --council review --json

Exit code 0 on success, 1 if all stage-1 models failed or the chairman failed
(the synthesis is then an error string, not a verdict). Failed models are
listed under "failures" in --json output and FAILURES in the digest.

Review mode refuses any council seat or chairman listed under `implementers`
in council.yaml. Plan mode gives each seat a random angle (--no-angles to
turn that off). --json also reports per-stage and per-call timings.
"""

import argparse
import asyncio
import json
import sys

from .config import check_keys, load_config
from .council import run_full_council
from .providers import ProviderRegistry


def _read_task(args) -> str:
    if args.task:
        task = args.task
    elif args.task_file:
        with open(args.task_file) as f:
            task = f.read()
    elif not sys.stdin.isatty():
        task = sys.stdin.read()
    else:
        raise SystemExit("error: provide a task as an argument, via --task-file, or on stdin")
    # an empty task would still be sent to every model (and billed)
    if not task.strip():
        raise SystemExit("error: the task is empty")
    return task


def _render_digest(task, mode, stage1, stage2, stage3, meta, failed) -> str:
    lines = [f"COUNCIL VERDICT ({mode}) — {task[:80]}", ""]
    agg = meta.get("aggregate_rankings", [])
    if agg:
        lines.append("Aggregate peer rankings (avg position, #votes):")
        for i, a in enumerate(agg, 1):
            marker = "   <- RECOMMENDED" if i == 1 else ""
            lines.append(
                f"  {i}. {a['model']}   avg {a['average_rank']}   "
                f"({a['rankings_count']} votes){marker}"
            )
        lines.append("")
    roles = meta.get("roles") or {}
    if roles:
        lines.append("Assigned angles:")
        lines.extend(f"  {model}: {angle}" for model, angle in roles.items())
        lines.append("")
    issues = meta.get("issues") or []
    if issues:
        lines.append("Issues with peer votes (REAL/FALSE POSITIVE/DUPLICATE):")
        for i in issues:
            v = i.get("votes")
            tally = f"{v['REAL']}/{v['FALSE POSITIVE']}/{v['DUPLICATE']}" if v else "new"
            lines.append(f"  {i['id']} [{tally}] {i['severity']} {i['kind']} {i['location']}: {i['description']}")
        lines.append("")
    heading = "REVIEW CHAIRMAN VERDICT:" if mode == "review" else "CHAIRMAN SYNTHESIS:"
    lines.append(heading)
    lines.append(stage3.get("response", ""))
    if failed:
        lines.append("")
        lines.append(f"FAILURES: {', '.join(failed)}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(prog="council", description=__doc__)
    p.add_argument("task", nargs="?", help="task as a string argument")
    p.add_argument("--task-file", help="read task from file")
    p.add_argument("--mode", choices=["answer", "plan", "review"], default="answer")
    p.add_argument("--council", default="default", help="named council from council.yaml")
    p.add_argument(
        "--chairman",
        default=None,
        help="named chairman (default: the council's own name if it exists, else 'default')",
    )
    p.add_argument("--config", default=None, help="path to council.yaml")
    p.add_argument("--timeout", type=float, default=None, help="per-request timeout override (s)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--no-angles", action="store_true",
                   help="plan mode: don't assign each seat an angle")
    args = p.parse_args()

    task = _read_task(args)
    cfg = load_config(args.config)
    if args.council not in cfg.councils:
        raise SystemExit(f"error: no council named {args.council!r} in config")
    council = cfg.councils[args.council]
    chairman_name = args.chairman or (args.council if args.council in cfg.chairmen else "default")
    if chairman_name not in cfg.chairmen:
        raise SystemExit(f"error: no chairman named {chairman_name!r} in config")
    chairman = cfg.chairmen[chairman_name]
    if args.mode == "review":
        # hard rule: a model never reviews its own work, not even as chairman
        for seat in [*council, chairman]:
            if seat.name in cfg.implementers:
                role = "chairman" if seat is chairman else f"a seat on council {args.council!r}"
                raise SystemExit(
                    f"error: {seat.name} is listed under implementers in the config and "
                    f"can't review as {role}; pick another with --council / --chairman"
                )
    try:
        check_keys([*council, chairman])
    except ValueError as e:
        raise SystemExit(f"error: {e}")

    registry = ProviderRegistry(council + [chairman])
    stage1, stage2, stage3, meta = asyncio.run(run_full_council(
        task, registry, council, chairman, mode=args.mode, timeout=args.timeout,
        angles=not args.no_angles,
    ))

    succeeded = {r["model"] for r in stage1}
    failed = [e.name for e in council if e.name not in succeeded]
    if stage3.get("failed") and stage1:
        failed.append(f"{chairman.name} (chairman)")

    if args.json:
        print(json.dumps({
            "task": task,
            "mode": args.mode,
            "council": args.council,
            "chairman": stage3.get("model", ""),
            "rankings": meta.get("aggregate_rankings", []),
            "roles": meta.get("roles", {}),
            "issues": meta.get("issues", []),
            "dropped": meta.get("dropped", []),
            "stage1": stage1,
            "stage2": stage2,
            "synthesis": stage3.get("response", ""),
            "failures": failed,
            "timings": {**meta.get("timings", {}), "calls": getattr(registry, "calls", [])},
        }, indent=2))
    else:
        print(_render_digest(task, args.mode, stage1, stage2, stage3, meta, failed))

    sys.exit(1 if stage3.get("failed") else 0)


if __name__ == "__main__":
    main()
