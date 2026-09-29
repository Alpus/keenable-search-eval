"""Small CLI for planning, running and rebuilding saved evaluation reports."""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from .core import ROOT, EvalError, atomic_json, read_config, suite_module
from .runner import checks, run


def main():
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("config", "plan", "doctor", "run", "allow"):
        child = sub.add_parser(command)
        child.add_argument("--config", type=Path, required=True)
        if command == "allow":
            child.add_argument("--existing-model-credit-usd", type=float)
            child.add_argument(
                "--confirm-existing-quota",
                action="store_true",
                required=True,
                help="Confirm service access, enough existing quota for this config, and paid overage disabled",
            )
        if command == "run":
            child.add_argument(
                "--plan-only", action="store_true", help="Save schedule without external calls"
            )
    child = sub.add_parser("report")
    child.add_argument("--run", required=True)
    child = sub.add_parser("grade")
    child.add_argument("--run", required=True)
    child = sub.add_parser("reconcile", help="Read-only recovery of confirmed GitHub reviews")
    child.add_argument("--run", required=True)
    args = parser.parse_args()
    try:
        if args.command in {"config", "plan", "doctor", "run", "allow"}:
            config = read_config(args.config)
            if args.command == "allow":
                from .core import record_operator_allowance

                path = record_operator_allowance(config, ROOT, args.existing_model_credit_usd)
                print(f"Operator quota attestation saved: {path}")
                return 0
            if args.command == "plan":
                from .core import schedule

                tasks = suite_module(config.suite).load_tasks(ROOT / config.manifest)
                result = {
                    "preview": True,
                    "config": config.model_dump(),
                    "attempts": schedule(config, tasks),
                }
                path = ROOT / "runs/plans" / f"{config.run_id}.json"
                atomic_json(path, result)
                print(
                    json.dumps(
                        {"planned": len(result["attempts"]), "plan": str(path), "live_calls": 0}
                    )
                )
                return 0
            if args.command == "config":
                from .effective_inputs import resolve_inputs

                print(json.dumps(resolve_inputs(config, ROOT), indent=2))
                return 0
            if args.command == "doctor":
                result, _ = checks(config)
                atomic_json(ROOT / "validation" / f"doctor-{config.run_id}.json", result)
                print(json.dumps(result, indent=2))
                return 0 if result["live_ready"] else 2
            result = run(config, plan_only=args.plan_only)
            if not args.plan_only:
                from .report import build_report

                run_dir = ROOT / "runs" / config.run_id
                state = json.loads((run_dir / "state.json").read_text())
                if any(a["status"] in {"pending", "running", "unknown"} for a in state["attempts"]):
                    raise EvalError(
                        "Run has unresolved attempts; reconcile saved evidence before continuing"
                    )
                live_grader = getattr(suite_module(config.suite), "grade_live", None)
                if live_grader is not None:
                    result["grading"] = live_grader(run_dir)
                result["report"] = build_report(run_dir)
        else:
            import re

            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,59}", args.run):
                raise EvalError("Invalid run ID")
            run_dir = ROOT / "runs" / args.run
            if args.command == "reconcile":
                from .runner import reconcile_reviews

                result = reconcile_reviews(run_dir)
            elif args.command == "grade":
                from .grading import grade_live

                result = grade_live(run_dir)
            else:
                from .report import build_report

                result = build_report(run_dir)
        print(json.dumps(result, indent=2))
        return 0
    except (EvalError, ValidationError, OSError, ValueError, KeyError) as exc:
        print(f"Evaluation stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
