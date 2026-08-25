"""The ``recuris`` command line.

One entry point per capability, each a thin dispatcher onto the module that
does the work. The subcommands parse their own arguments, so ``recuris tau2
--help`` shows the tau2 arm's real options rather than a summary maintained
separately here and drifting out of date.

    recuris check-data                 preflight: is everything set up?
    recuris tau2 ...                   run a tau2-Bench arm (bare or skill)
    recuris tau2-emonly ...            the EM-only ablation arm
    recuris score ...                  Avg@k / Pass@k with validation
    recuris compare ...                paired comparison of two runs
    recuris scorecard ...              mechanism fingerprint, then the score
    recuris metaagent run|qualify|...  the recursive evolution loop
    recuris skillflow render-configs|score
    recuris tta run ...                Terminal-Bench 2.1 test-time adaptation
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

USAGE = __doc__


def _run(entry: Callable[..., object], argv: list[str], *, prog: str = "") -> int:
    """Call a module main() that parses ``sys.argv``, and normalise its exit code.

    ``prog`` sets what the subcommand's own --help calls itself; without it a
    module that builds its parser from ``sys.argv[0]`` announces the dispatcher
    rather than the command the user typed.
    """
    saved = sys.argv
    sys.argv = [prog or saved[0], *argv]
    try:
        result = entry()
    except SystemExit as exc:
        # argparse exits with an int; `raise SystemExit("message")` exits with
        # the message itself, which is how several of these modules report a
        # bad argument. Print it rather than trying to make an exit code of it.
        code = exc.code
        if isinstance(code, str):
            print(code, file=sys.stderr)
            return 1
        return int(code or 0)
    finally:
        sys.argv = saved
    return int(result) if isinstance(result, int) else 0


def check_data(argv: list[str]) -> int:
    """Preflight the things a run needs, and say exactly what is missing."""
    ap = argparse.ArgumentParser(prog="recuris check-data", description=check_data.__doc__)
    ap.add_argument("--benchmark", choices=("tau2", "skillflow", "tb21", "all"), default="all")
    args = ap.parse_args(argv)

    from recuris.paths import (
        PathError,
        external_root,
        skill_memory_root,
        splits_root,
        tau2_root,
    )

    problems: list[str] = []
    notes: list[str] = []

    packages = sorted(
        p.name for p in skill_memory_root().glob("*") if (p / "manifest.yaml").is_file()
    )
    notes.append(f"Skill Memory packages: {', '.join(packages) or '(none found)'}")

    try:
        from recuris.metaagent.integrity import verify

        result = verify()
        notes.append(f"champion integrity: OK ({result['tree_sha256'][:12]})")
    except Exception as exc:
        problems.append(f"champion integrity: {exc}")

    if args.benchmark in ("tau2", "all"):
        try:
            root = tau2_root()
            notes.append(f"tau2-Bench checkout: {root}")
            try:
                import tau2  # noqa: F401

                notes.append("tau2 package: importable")
            except ImportError:
                problems.append(
                    "tau2 package is not importable. Run `uv sync --extra tau2` and "
                    "`bash third_party/tau2/setup.sh`."
                )
            else:
                from tau2.registry import registry

                from recuris.adapters.tau2.agent import create_recuris_agent

                registry.register_agent_factory(create_recuris_agent, "recuris_agent")
                notes.append("agent factory: registers as 'recuris_agent'")
        except PathError as exc:
            problems.append(str(exc))

    if args.benchmark in ("skillflow", "all"):
        tasks = external_root() / "SkillFlow" / "test_tasks" / "test_tasks"
        if tasks.is_dir():
            notes.append(f"SkillFlow tasks: {tasks}")
        else:
            problems.append(
                f"SkillFlow tasks not found at {tasks}. "
                "Run `bash third_party/skillflow/setup.sh`."
            )

    if args.benchmark in ("tb21", "all"):
        tasks = external_root() / "terminal-bench-2.1" / "tasks"
        if tasks.is_dir():
            notes.append(f"Terminal-Bench 2.1 tasks: {tasks}")
        else:
            problems.append(
                f"Terminal-Bench 2.1 tasks not found at {tasks}. "
                "Run `bash third_party/tb21/setup.sh`."
            )

    for split_dir in sorted(splits_root().glob("*")):
        manifest = split_dir / "split_manifest.json"
        if manifest.is_file():
            notes.append(f"splits: {split_dir.name} ({manifest.name} present)")

    for line in notes:
        print("  ok    " + line)
    for line in problems:
        print("  MISSING " + line)
    if problems:
        print(f"\n{len(problems)} thing(s) to fix. The README has the setup steps.")
        return 1
    print("\nEverything this checked is in place.")
    return 0


def metaagent(argv: list[str]) -> int:
    sub = argv[0] if argv else ""
    rest = argv[1:]
    if sub == "run":
        from recuris.metaagent.driver import main as entry

        return _run(entry, rest, prog="recuris metaagent run")
    if sub == "qualify":
        from recuris.metaagent.driver import main as entry

        return _run(entry, [*rest, "--qualify-only"], prog="recuris metaagent qualify")
    if sub == "gate":
        from recuris.metaagent.gate import main as entry

        return _run(entry, rest, prog="recuris metaagent gate")
    if sub == "eval":
        from recuris.metaagent.eval_package import main as entry

        return _run(entry, rest, prog="recuris metaagent eval")
    if sub == "lint":
        from recuris.metaagent.lint import main as entry

        return _run(entry, rest, prog="recuris metaagent lint")
    if sub == "sanitize":
        from recuris.metaagent.sanitize import main as entry

        return _run(entry, rest, prog="recuris metaagent sanitize")
    if sub == "settle":
        from recuris.metaagent.settle import main as entry

        return _run(entry, rest, prog="recuris metaagent settle")
    if sub == "integrity":
        from recuris.metaagent.integrity import main as entry

        return _run(entry, rest, prog="recuris metaagent integrity")
    print(
        "usage: recuris metaagent {run,qualify,gate,eval,lint,sanitize,settle,integrity} ...",
        file=sys.stderr,
    )
    return 2


def skillflow(argv: list[str]) -> int:
    sub = argv[0] if argv else ""
    rest = argv[1:]
    if sub == "render-configs":
        from recuris.adapters.skillflow.render_configs import main as entry

        return entry(rest)
    if sub == "score":
        from recuris.adapters.skillflow.score import main as entry

        return entry(rest)
    print("usage: recuris skillflow {render-configs,score} ...", file=sys.stderr)
    return 2


def tta(argv: list[str]) -> int:
    sub = argv[0] if argv else ""
    if sub == "run":
        from recuris.tta.driver import main as entry

        return _run(entry, argv[1:], prog="recuris tta run")
    print("usage: recuris tta run ...", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("-V", "--version"):
        from recuris import __version__

        print(f"recuris {__version__}")
        return 0

    command, rest = argv[0], argv[1:]

    if command == "check-data":
        return check_data(rest)
    if command == "tau2":
        from recuris.adapters.tau2.run import main as entry

        return entry(rest)
    if command == "tau2-emonly":
        from recuris.adapters.tau2.emonly import main as entry

        return entry(rest)
    if command == "score":
        from recuris.tools.avgk import main as entry

        return _run(entry, rest, prog="recuris score")
    if command == "compare":
        from recuris.tools.compare_runs import main as entry

        return _run(entry, rest, prog="recuris compare")
    if command == "scorecard":
        from recuris.tools.scorecard import main as entry

        return _run(entry, rest, prog="recuris scorecard")
    if command == "metaagent":
        return metaagent(rest)
    if command == "skillflow":
        return skillflow(rest)
    if command == "tta":
        return tta(rest)

    print(f"unknown command: {command}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
