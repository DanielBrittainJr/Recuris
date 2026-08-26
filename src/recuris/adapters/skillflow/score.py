"""Score SkillFlow harbor jobs, and compare two arms task by task.

A harbor job directory holds one directory per task family, each holding one
directory per trial, each holding ``result.json`` with the verifier's reward.
This walks that tree, aggregates per task, and -- given two arms -- reports the
paired difference with a task-clustered bootstrap interval.

Pairing matters more than the aggregate here. SkillFlow families differ widely
in difficulty, so an unpaired comparison of two arms that happened to attempt
slightly different task sets can move several points for no reason at all.

Invoked as ``recuris skillflow score``.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260825


def _reward(result: Path) -> float | None:
    try:
        doc = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rewards = (doc.get("verifier_result") or {}).get("rewards") or {}
    value = rewards.get("reward")
    return float(value) if isinstance(value, (int, float)) else None


def _family(doc: dict, result: Path) -> str:
    """The task's family, taken from where the task lives in the task set.

    Deliberately not from the job directory. harbor names that after the job,
    and render-configs names the job after the arm, so a family read from it
    carries "bare" or "skill" inside it and the two arms can never share a key.
    That made the paired comparison, which is the whole point of this command,
    report "the two arms share no tasks" on every run.
    """
    path = str((doc.get("task_id") or {}).get("path") or "").replace("\\", "/")
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2]
    group = result.parent.parent.name
    return group.split("__")[-1] if "__" in group else group


def collect(jobs_dir: Path) -> dict[str, list[float]]:
    """Map ``family/task`` to the list of rewards observed for it."""
    per_task: dict[str, list[float]] = defaultdict(list)
    for result in sorted(jobs_dir.rglob("result.json")):
        value = _reward(result)
        if value is None:
            continue
        doc = json.loads(result.read_text(encoding="utf-8"))
        task = str(doc.get("task_name") or result.parent.name)
        per_task[f"{_family(doc, result)}/{task}"].append(value)
    return dict(per_task)


def summarise(per_task: dict[str, list[float]]) -> dict:
    means = {task: sum(v) / len(v) for task, v in per_task.items()}
    trials = sum(len(v) for v in per_task.values())
    return {
        "tasks": len(means),
        "trials": trials,
        "mean": (sum(means.values()) / len(means)) if means else 0.0,
        "per_task": means,
    }


def paired(base: dict[str, float], skill: dict[str, float]) -> dict | None:
    """Task-clustered paired bootstrap over the tasks both arms attempted."""
    common = sorted(set(base) & set(skill))
    if not common:
        return None
    diffs = {task: skill[task] - base[task] for task in common}
    delta = sum(diffs.values()) / len(common)
    rng = random.Random(BOOTSTRAP_SEED)
    draws = sorted(
        sum(diffs[rng.choice(common)] for _ in common) / len(common)
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    lo = draws[int(0.025 * BOOTSTRAP_RESAMPLES)]
    hi = draws[int(0.975 * BOOTSTRAP_RESAMPLES) - 1]
    return {
        "n_tasks": len(common),
        "delta_pp": delta * 100,
        "ci95_pp": [lo * 100, hi * 100],
        "excludes_zero": lo > 0 or hi < 0,
        "improved": sum(1 for d in diffs.values() if d > 1e-9),
        "regressed": sum(1 for d in diffs.values() if d < -1e-9),
        "base_mean": sum(base[t] for t in common) / len(common) * 100,
        "skill_mean": sum(skill[t] for t in common) / len(common) * 100,
    }


def build_parser(prog: str = "recuris skillflow score") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__)
    ap.add_argument("--bare", type=Path, help="jobs directory of the bare arm")
    ap.add_argument("--skill", type=Path, help="jobs directory of the skill arm")
    ap.add_argument("--jobs-dir", type=Path, help="score a single jobs directory")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.jobs_dir:
        result = summarise(collect(args.jobs_dir))
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(
                f"{args.jobs_dir}: {result['mean'] * 100:.2f}% "
                f"over {result['tasks']} tasks / {result['trials']} trials"
            )
        return 0

    if not (args.bare and args.skill):
        ap.error("pass --jobs-dir, or both --bare and --skill")

    bare = summarise(collect(args.bare))
    skill = summarise(collect(args.skill))
    contrast = paired(bare["per_task"], skill["per_task"])
    if contrast is None:
        ap.error("the two arms share no tasks; nothing to pair")

    if args.json:
        print(json.dumps({"bare": bare, "skill": skill, "paired": contrast},
                         indent=2, sort_keys=True))
        return 0

    print(f"bare   {bare['mean'] * 100:6.2f}%  ({bare['tasks']} tasks, {bare['trials']} trials)")
    print(f"skill  {skill['mean'] * 100:6.2f}%  ({skill['tasks']} tasks, {skill['trials']} trials)")
    print()
    print(f"paired over {contrast['n_tasks']} shared tasks:")
    print(
        f"  {contrast['base_mean']:.2f} -> {contrast['skill_mean']:.2f}  "
        f"delta = {contrast['delta_pp']:+.2f} pp  "
        f"95% CI [{contrast['ci95_pp'][0]:+.2f}, {contrast['ci95_pp'][1]:+.2f}]"
        f"{'  (excludes 0)' if contrast['excludes_zero'] else ''}"
    )
    print(f"  improved {contrast['improved']}, regressed {contrast['regressed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
