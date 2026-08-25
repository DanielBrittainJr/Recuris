# -*- coding: utf-8 -*-
"""Meta-Agent eval tool — run a Skill Memory package on tasks, print per-task pass
rate AND the structured failure detail (exactly which required actions / checks
failed) for every failing task. This is the 'answer key' for diagnosis.

Usage:
  recuris metaagent eval \
      --pkg skill_memories/<package>   (or 'bare' for the stock tau2 agent) \
      --domain retail --task-ids 5 8 9 --k 3
"""
from __future__ import annotations

import argparse
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
from recuris.metaagent.downstream import RecurisDownstream


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pkg", required=True, help="path to package, or 'bare' for raw llm_agent")
    ap.add_argument("--domain", default="retail")
    ap.add_argument("--task-ids", nargs="*", required=True)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--max-concurrency", type=int, default=8)
    ap.add_argument("--show-traj", action="store_true", help="also print the full failing transcript")
    args = ap.parse_args()

    ds = RecurisDownstream(domain=args.domain, max_concurrency=args.max_concurrency, eval_timeout=2400)
    pkg = None if args.pkg == "bare" else args.pkg
    res = ds.evaluate(pkg, args.task_ids, seeds=args.k, tag="ma_eval")

    rows = [(t, statistics.mean(res.matrix[t])) for t in sorted(res.matrix, key=lambda x: int(x))]
    print(f"=== {args.pkg} on {args.domain} tasks {args.task_ids} (k={args.k}) ===")
    print(f"overall pass = {statistics.mean([r for _, r in rows])*100:.1f}%")
    for t, r in rows:
        print(f"  task {t}: {r*100:.0f}% pass" + ("   [FAIL]" if t in res.failing else ""))
    print(f"\nfailing tasks: {res.failing}")
    for t in res.failing:
        print(f"\n----- WHAT FAILED on task {t} (diagnose from THIS) -----")
        print(res.failure_details.get(t, "(no detail)"))
        if args.show_traj:
            print("--- transcript ---")
            print((res.trajectories.get(t) or [""])[0][:6000])


if __name__ == "__main__":
    main()
