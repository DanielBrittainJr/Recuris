"""Compare two runs: per-task pass rates, and a bootstrap CI of the difference.

    recuris compare --a retail_skill --b retail_bare

Each side takes one or more run names or paths. A bare name is resolved against
the tau2 simulations directory, so the names passed to `--save-to` work here
directly. Multiple paths per side are pooled.

The bootstrap resamples **tasks**, not trials. Trials within a task are
correlated, so resampling simulations would report an interval far narrower
than the evidence supports.

Two things it refuses to do, both because of the same incident. A mid-run
snapshot once produced a +18.65 that fell to +15.57 once the arm finished: a
task with one lucky trial out of an intended four reads as 100%. So pairing
stops if the two sides share too few tasks, and it stops if trial counts are
uneven within a side. `--allow-partial` overrides both, and prints what it is
overriding.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

DEFAULT_MIN_COMMON = 40


def resolve(spec: str) -> Path:
    """A path, or a run name under the tau2 simulations directory."""
    path = Path(spec)
    if path.exists():
        return path
    from recuris.paths import tau2_data_dir

    candidate = tau2_data_dir(required=False) / "simulations" / spec
    if candidate.exists():
        return candidate
    raise SystemExit(
        f"no such run: {spec!r}. Pass a path, or the name given to --save-to "
        f"(looked in {candidate.parent})."
    )


def load_sims(specs: list[str]) -> list[dict]:
    sims: list[dict] = []
    for spec in specs:
        path = resolve(spec)
        if path.is_dir():
            results = path / "results.json"
            if results.is_file():
                path = results
            else:
                candidates = sorted(path.glob("*.json"))
                if not candidates:
                    raise SystemExit(f"no json under {path}")
                path = max(candidates, key=lambda q: q.stat().st_mtime)
        data = json.loads(path.read_text(encoding="utf-8"))
        sims.extend(data.get("simulations") or [])
    return sims


def per_task_outcomes(sims: list[dict]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for s in sims:
        r = (s.get("reward_info") or {}).get("reward")
        if r is not None:
            out[str(s.get("task_id"))].append(1 if float(r) >= 1.0 else 0)
    return out


def gate_fire_rate(sims: list[dict]) -> float:
    fired = sum(
        1
        for s in sims
        if any(
            m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].startswith("[SYSTEM CHECK")
            for m in (s.get("messages") or [])
        )
    )
    return fired / max(1, len(sims))


def total(outcomes: dict[str, list[int]]) -> tuple[int, int]:
    wins = sum(sum(v) for v in outcomes.values())
    n = sum(len(v) for v in outcomes.values())
    return wins, n


def check_pairable(
    a: dict[str, list[int]],
    b: dict[str, list[int]],
    labels: tuple[str, str],
    *,
    min_common: int,
    allow_partial: bool,
) -> list[str]:
    """Reasons this pair should not be compared. Empty means go ahead."""
    problems: list[str] = []
    common = set(a) & set(b)
    if not common:
        # Not a weaker version of "too few tasks": there is nothing to compare,
        # and every statistic below would be computed over the empty set. The
        # bootstrap happily returns [+0.0%, +0.0%] for it, which reads as a
        # confident finding of parity rather than as an absent comparison, so
        # --allow-partial must not reach this case.
        return [
            f"{labels[0]} and {labels[1]} share no scored tasks. Either the "
            "runs cover different tasks, or one of them produced no gradable "
            "episode at all -- check its run output for ungraded episodes."
        ]
    if len(common) < min_common:
        problems.append(
            f"only {len(common)} tasks in common (need {min_common}). A partial "
            "arm reads as a large effect: one lucky trial out of an intended "
            "four scores 100%."
        )
    for outcomes, label in ((a, labels[0]), (b, labels[1])):
        widths = {len(outcomes[t]) for t in common if t in outcomes}
        if len(widths) > 1:
            short = sorted(t for t in common if len(outcomes.get(t, [])) < max(widths))
            problems.append(
                f"{label} has uneven trial counts {sorted(widths)}; "
                f"{len(short)} task(s) are short, e.g. {short[:5]}. "
                "An unfinished task is not a result."
            )
    if problems and allow_partial:
        for problem in problems:
            print(f"[recuris] WARNING (--allow-partial): {problem}")
        return []
    return problems


def bootstrap_diff(
    a: dict[str, list[int]], b: dict[str, list[int]], iters: int, seed: int = 7
) -> tuple[float, float]:
    """Cluster bootstrap over the COMMON task set; returns (lo, hi) of a-b."""
    tasks = sorted(set(a) & set(b))
    rng = random.Random(seed)
    diffs = []
    for _ in range(iters):
        sample = [tasks[rng.randrange(len(tasks))] for _ in tasks]
        aw = an = bw = bn = 0
        for t in sample:
            aw += sum(a[t])
            an += len(a[t])
            bw += sum(b[t])
            bn += len(b[t])
        diffs.append(aw / max(1, an) - bw / max(1, bn))
    diffs.sort()
    return diffs[int(0.025 * iters)], diffs[int(0.975 * iters)]


def main() -> None:
    ap = argparse.ArgumentParser(prog="recuris compare", description=__doc__)
    ap.add_argument("--a", nargs="+", required=True, help="run name(s) or path(s)")
    ap.add_argument("--b", nargs="+", required=True, help="run name(s) or path(s)")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument("--boot", type=int, default=10000)
    ap.add_argument(
        "--min-common",
        type=int,
        default=DEFAULT_MIN_COMMON,
        help=f"refuse to pair below this many shared tasks (default {DEFAULT_MIN_COMMON})",
    )
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="pair anyway, printing each refusal as a warning",
    )
    args = ap.parse_args()

    sims_a, sims_b = load_sims(args.a), load_sims(args.b)
    oa, ob = per_task_outcomes(sims_a), per_task_outcomes(sims_b)

    problems = check_pairable(
        oa, ob, (args.label_a, args.label_b),
        min_common=args.min_common, allow_partial=args.allow_partial,
    )
    if problems:
        print("refusing to pair these runs:")
        for problem in problems:
            print("  " + problem)
        if not (set(oa) & set(ob)):
            print("\nThere is nothing to compare here; --allow-partial does not "
                  "apply. Re-run whichever side produced no gradable episode.")
        else:
            print("\nFinish the runs, or pass --allow-partial if you know why.")
        raise SystemExit(1)

    (aw, an), (bw, bn) = total(oa), total(ob)
    print(f"{args.label_a}: {aw}/{an} = {aw / max(1, an):.1%} | gate fire {gate_fire_rate(sims_a):.0%}")
    print(f"{args.label_b}: {bw}/{bn} = {bw / max(1, bn):.1%} | gate fire {gate_fire_rate(sims_b):.0%}")
    common = sorted(set(oa) & set(ob), key=lambda x: (len(x), x))
    lo, hi = bootstrap_diff(oa, ob, args.boot)
    print(f"diff ({args.label_a}-{args.label_b}) bootstrap95 CI over {len(common)} common tasks: "
          f"[{lo:+.1%}, {hi:+.1%}]  {'(includes 0 — parity plausible)' if lo <= 0 <= hi else '(excludes 0)'}")
    print("\nper-task (common):")
    for t in common:
        pa = f"{sum(oa[t])}/{len(oa[t])}"
        pb = f"{sum(ob[t])}/{len(ob[t])}"
        flag = "" if abs(sum(oa[t]) / len(oa[t]) - sum(ob[t]) / len(ob[t])) < 0.25 else "  <-- diverges"
        print(f"  task {t}: {args.label_a} {pa} vs {args.label_b} {pb}{flag}")


if __name__ == "__main__":
    main()
