# iTailor - Copyright (c) 2026 Michael Bidollahkhani. All rights reserved. MIT License.
"""
The evaluation harness (design §11.4).

    python evals/run_evals.py --list
    python evals/run_evals.py --task t01-describe-csv            # against the configured model
    python evals/run_evals.py --all --out results.json

Each task runs in a fresh copy of its fixture folder with a scripted
oversight that allows installs and commands (counting them as interrupts)
and answers consultations with the first option (counting them). Metrics
are computed from the run result and the transcript, never from what the
model said. A change to a control-plane method or constant is judged
against these numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from itailor import Agent, checks as checks_module  # noqa: E402
from itailor.oversight import CallbackOversight  # noqa: E402
from itailor.runs import RunDir  # noqa: E402
from itailor.transcript import Transcript  # noqa: E402


def load_tasks() -> dict:
    with open(os.path.join(HERE, "tasks.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_checks(specs):
    out = []
    for spec in specs:
        kind, _, arg = spec.partition(":")
        if kind == "file":
            out.append(checks_module.file_exists(arg))
        elif kind == "stdout":
            out.append(checks_module.stdout_matches(arg))
        elif kind == "csv":
            path, _, rows = arg.partition("@")
            out.append(checks_module.csv_rows(path, int(rows or 1)))
        elif kind == "json":
            out.append(checks_module.json_valid(arg))
        elif kind == "cmd":
            out.append(checks_module.command_exit_zero(arg))
    return out


def make_fixture(task: dict) -> str:
    target = tempfile.mkdtemp(prefix=f"itailor-eval-{task['id']}-")
    source = os.path.join(HERE, task.get("fixture") or "")
    if os.path.isdir(source):
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        # A minimal products.csv so the suite runs without the fixtures folder.
        with open(os.path.join(target, "products.csv"), "w", encoding="utf-8") as handle:
            handle.write("id,name,category,price,cost\n")
            rows = [(i, f"item{i}", ["tools", "toys", "food", "books"][i % 4], 10 + i * 1.5, 5 + i) for i in range(40)]
            handle.writelines(f"{r[0]},{r[1]},{r[2]},{r[3]:.2f},{r[4]}\n" for r in rows)
        os.makedirs(os.path.join(target, "data"), exist_ok=True)
        for n in range(3):
            with open(os.path.join(target, "data", f"part{n}.csv"), "w", encoding="utf-8") as handle:
                handle.write("a,b\n" + "".join(f"{i},{i * n}\n" for i in range(3)))
        with open(os.path.join(target, "notes.md"), "w", encoding="utf-8") as handle:
            handle.write("# Notes\n\nSome *notes* here.\n")
    return target


def run_task(task: dict, model: str | None = None, mode: str = "default") -> dict:
    project = make_fixture(task)
    counters = {"interrupts": 0, "consultations": 0}

    def permission(request):
        counters["interrupts"] += 1
        return "allow"

    def consult(request):
        counters["consultations"] += 1
        return (request.options or ["proceed with the obvious reading"])[0]

    agent = Agent(project, model=model, oversight=CallbackOversight(permission, consult), permissions=mode)
    started = time.time()
    result = agent.run(task["goal"], checks=parse_checks(task.get("checks") or []))
    elapsed = time.time() - started
    run_dir = RunDir.open(project, result.run_id)
    rows = Transcript.read(run_dir.transcript_path)
    decisions = [r for r in rows if r.get("type") == "decision"]
    refusals = sum(1 for d in decisions if d.get("decision") == "deny" and d.get("source") in ("hard-block", "risk", "deny-rules", "org-policy"))
    attempts = sum(int(s.get("attempts") or 0) for s in result.steps)
    wasted = sum(max(0, int(s.get("attempts") or 0) - 1) for s in result.steps if s.get("status") != "done")
    verified = result.ok and all(c.get("passed") for c in result.checks)
    expect = task.get("expect") or {}
    metrics = {
        "task": task["id"], "status": result.status, "verified_success": bool(verified),
        "repair_attempts": attempts, "wasted_attempts": wasted,
        "interrupts": counters["interrupts"], "consultations": counters["consultations"], "refusals": refusals,
        "model_calls": result.usage.get("calls", 0), "tokens": result.usage.get("totalTokens", 0),
        "wall_seconds": round(elapsed, 1), "report": result.report_path, "project": project,
        "expect": expect,
    }
    metrics["interrupts_justified"] = _within(counters["interrupts"], expect.get("interrupts"))
    metrics["consultations_as_expected"] = _within(counters["consultations"], expect.get("consultations", 0))
    metrics["refusals_as_expected"] = _within(refusals, expect.get("refusals", 0))
    if expect.get("status") == "not succeeded":
        metrics["verified_success"] = result.status != "succeeded"
    return metrics


def _within(value: int, spec) -> bool | None:
    if spec is None:
        return None
    spec = str(spec)
    if spec.startswith(">="):
        return value >= int(spec[2:])
    if spec.startswith("<="):
        return value <= int(spec[2:])
    return value == int(spec)


def summarise(results: list[dict]) -> dict:
    n = len(results) or 1
    return {
        "tasks": len(results),
        "verified_success_rate": round(sum(1 for r in results if r["verified_success"]) / n, 3),
        "mean_repair_attempts": round(sum(r["repair_attempts"] for r in results) / n, 2),
        "wasted_attempts": sum(r["wasted_attempts"] for r in results),
        "interrupts_justified": sum(1 for r in results if r["interrupts_justified"]),
        "interrupts_unjustified": sum(1 for r in results if r["interrupts_justified"] is False),
        "refusals_as_expected": sum(1 for r in results if r["refusals_as_expected"]),
        "total_tokens": sum(r["tokens"] for r in results),
        "total_wall_seconds": round(sum(r["wall_seconds"] for r in results), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--mode", default="default")
    parser.add_argument("--out")
    args = parser.parse_args()
    suite = load_tasks()
    tasks = suite["tasks"]
    if args.list or not (args.task or args.all):
        for task in tasks:
            print(f"{task['id']:28} {task['goal'][:80]}")
        return 0
    selected = [t for t in tasks if args.all or t["id"] in args.task]
    results = []
    for task in selected:
        print(f"== {task['id']}: {task['goal'][:70]}")
        try:
            metrics = run_task(task, model=args.model, mode=args.mode)
        except Exception as exc:  # noqa: BLE001
            metrics = {"task": task["id"], "status": "error", "error": str(exc), "verified_success": False, "repair_attempts": 0,
                       "wasted_attempts": 0, "interrupts_justified": None, "consultations_as_expected": None,
                       "refusals_as_expected": None, "tokens": 0, "wall_seconds": 0}
        print(f"   {metrics['status']} verified={metrics['verified_success']} interrupts={metrics.get('interrupts')} "
              f"attempts={metrics['repair_attempts']} tokens={metrics['tokens']}")
        results.append(metrics)
    summary = summarise(results)
    print("\n" + json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "results": results}, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
