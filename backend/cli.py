"""One-shot CLI for the LLM Council service mode.

Usage:
    council "How do I implement RCU in C?"
    council --task-file task.md --mode plan --council default
    cat review-task.md | council --mode review --council review --json

Exit code 0 on success, 1 if all stage-1 models failed.
"""

import argparse
import asyncio
import json
import sys

from .config import load_config
from .council import run_full_council
from .providers import ProviderRegistry


def _read_task(args) -> str:
    if args.task:
        return args.task
    if args.task_file:
        with open(args.task_file) as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("error: provide a task as an argument, via --task-file, or on stdin")


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

    registry = ProviderRegistry(council + [chairman])
    stage1, stage2, stage3, meta = asyncio.run(
        run_full_council(task, registry, council, chairman, mode=args.mode, timeout=args.timeout)
    )

    if args.json:
        print(json.dumps({
            "task": task,
            "mode": args.mode,
            "council": args.council,
            "chairman": stage3.get("model", ""),
            "rankings": meta.get("aggregate_rankings", []),
            "stage1": stage1,
            "stage2": stage2,
            "synthesis": stage3.get("response", ""),
        }, indent=2))
    else:
        succeeded = {r["model"] for r in stage1}
        failed = [e.name for e in council if e.name not in succeeded]
        print(_render_digest(task, args.mode, stage1, stage2, stage3, meta, failed))

    sys.exit(0 if stage1 else 1)


if __name__ == "__main__":
    main()
