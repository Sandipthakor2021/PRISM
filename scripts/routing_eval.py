#!/usr/bin/env python3
"""One-command routing eval: python3 scripts/routing_eval.py [--json]

Runs every prompt in data/routing_eval.jsonl through the `auto` classifier (classification
only, no upstream call) and reports accuracy, per-case expected vs actual with the routing
reason, and the length-only baseline for comparison. Labels are used here only for scoring.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from prism.classifier import classify, length_only_baseline  # noqa: E402


def run_eval(path=None):
    path = Path(path or ROOT / "data" / "routing_eval.jsonl")
    cases, correct, base_correct = [], 0, 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        tier, score, reason = classify(row["prompt"])
        ok = tier == row["expected_tier"]
        base = length_only_baseline(row["prompt"])
        correct += ok
        base_correct += base == row["expected_tier"]
        cases.append({"id": row["id"], "expected": row["expected_tier"], "actual": tier, "correct": ok,
                      "baseline": base, "reason": reason, "note": row["note"]})
    total = len(cases)
    return {"total": total, "correct": correct, "accuracy": round(correct / total, 4),
            "length_only_baseline_accuracy": round(base_correct / total, 4), "cases": cases}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="print the full JSON report")
    args = ap.parse_args()
    rep = run_eval()
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        for c in rep["cases"]:
            print(f"{'OK  ' if c['correct'] else 'MISS'} {c['id']}  expected={c['expected']:5} actual={c['actual']:5}  {c['reason'][:95]}")
        print(f"\nAccuracy: {rep['correct']}/{rep['total']} = {rep['accuracy']:.0%}   "
              f"(length-only baseline: {rep['length_only_baseline_accuracy']:.0%})")
    sys.exit(0)
