# -*- coding: utf-8 -*-
"""Meta-Agent validation gate — the ONLY way a change earns "keep it".
Evaluates BASE vs CANDIDATE on a robust held-out set (use >=12 tasks or it
overfits!) + checks the diagnosed failing task(s) actually repaired. Prints a
paired-bootstrap CI and a clear ACCEPT/REJECT.

A change is ACCEPTED only if ALL hold:
  1. repair: the diagnosed failing task's pass rate RISES,
  2. held-out net improvement > 0,
  3. regressions (held-out tasks that dropped) <= reg_cap.
And it is SIGNIFICANT only if the held-out paired CI excludes 0.

Usage:
  recuris metaagent gate \
    --base skill_memories/<baseline> --cand skill_memories/<candidate> \
    --domain retail --heldout 18 22 37 52 55 60 70 79 102 3 4 7 11 \
    --repair-tasks 5 --k 3 --reg-cap 1
"""
from __future__ import annotations

import argparse
import random
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
from recuris.metaagent.downstream import RecurisDownstream


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True, help="baseline package path (or 'bare')")
    ap.add_argument("--cand", required=True, help="candidate package path")
    ap.add_argument("--domain", default="retail")
    ap.add_argument("--heldout", nargs="*", required=True)
    ap.add_argument("--repair-tasks", nargs="*", default=[], help="the diagnosed failing task(s) that must repair")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--reg-cap", type=int, default=1)
    ap.add_argument("--max-concurrency", type=int, default=8)
    args = ap.parse_args()
    random.seed(0)

    ds = RecurisDownstream(domain=args.domain, max_concurrency=args.max_concurrency, eval_timeout=2400)
    base_pkg = None if args.base == "bare" else args.base
    b = ds.evaluate(base_pkg, args.heldout, seeds=args.k, tag="gate_base")
    c = ds.evaluate(args.cand, args.heldout, seeds=args.k, tag="gate_cand")

    tasks = sorted(set(b.matrix) & set(c.matrix), key=lambda x: int(x))
    diffs = [statistics.mean(c.matrix[t]) - statistics.mean(b.matrix[t]) for t in tasks]
    net = statistics.mean(diffs) * 100
    up = sum(1 for d in diffs if d > 1e-9)
    dn = sum(1 for d in diffs if d < -1e-9)
    boots = sorted(statistics.mean([diffs[random.randrange(len(diffs))] for _ in diffs]) * 100
                   for _ in range(10000))
    lo, hi = boots[250], boots[9750]

    print(f"=== GATE: {args.cand}  vs  {args.base}  on {len(tasks)} held-out tasks (k={args.k}) ===")
    print(f"held-out net = {net:+.1f}pp   CI[{lo:+.1f}, {hi:+.1f}]   up={up} dn={dn}")

    repaired = True
    if args.repair_tasks:
        rb = ds.evaluate(base_pkg, args.repair_tasks, seeds=args.k, tag="gate_rbase")
        rc = ds.evaluate(args.cand, args.repair_tasks, seeds=args.k, tag="gate_rcand")
        br = statistics.mean(statistics.mean(rb.matrix.get(t, [0])) for t in args.repair_tasks)
        cr = statistics.mean(statistics.mean(rc.matrix.get(t, [0])) for t in args.repair_tasks)
        repaired = cr > br + 1e-9
        print(f"repair(diagnosed {args.repair_tasks}): {br:.2f} -> {cr:.2f}  {'OK' if repaired else 'NOT repaired'}")

    accept = repaired and net > 1e-9 and dn <= args.reg_cap
    sig = lo > 0
    print(f"\nVERDICT: {'ACCEPT' if accept else 'REJECT'}"
          f"  (repair={'ok' if repaired else 'no'}, net>0={net>0}, regress {dn}<= {args.reg_cap}={dn<=args.reg_cap})")
    print(f"significant (CI excludes 0): {'YES' if sig else 'no'}")
    if accept and not sig:
        print("NOTE: accepted for the stack, but not yet significant alone — significance is judged on the FULL accumulated stack.")


if __name__ == "__main__":
    main()
