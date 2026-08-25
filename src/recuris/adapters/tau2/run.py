"""Run a tau2-Bench arm, with or without a Skill Memory package.

The tau2-Bench checkout is never modified: the Recuris agent is registered with
tau2's own agent registry at run time, and everything else is stock tau2.

Both arms of a paired comparison go through this one entry point, so the only
thing that can differ between them is what the command line says differs. The
treatment validators in :mod:`recuris.adapters.tau2.treatment` reject an arm
whose model or decoding settings drift from the declared treatment.

Invoked as ``recuris tau2 ...``; the README has the full walkthrough.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from recuris.paths import PathError, resolve_skill_memory, tau2_data_dir, tau2_root

from .treatment import (
    LLM_ARGS_JSON,
    PROVIDER_MODEL,
    treatment_triple,
    validate_frozen_treatment,
    validate_open_downstream_agent,
)


def build_parser(prog: str = "recuris tau2") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__)
    ap.add_argument("--save-to", required=True, help="run name under <data>/simulations")
    ap.add_argument("--domain", default="retail")
    ap.add_argument("--task-ids", nargs="*", default=None)
    ap.add_argument("--num-trials", type=int, default=4)
    ap.add_argument("--max-concurrency", type=int, default=4)
    ap.add_argument(
        "--simulation-timeout",
        type=float,
        default=None,
        help="wall-clock seconds for one simulation; omitted means the tau2 "
        "default of no per-simulation timeout",
    )
    ap.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="whole-simulation retries after the initial attempt",
    )
    ap.add_argument("--retry-delay", type=float, default=1.0)
    ap.add_argument("--agent-llm", default=PROVIDER_MODEL)
    ap.add_argument("--user-llm", default=PROVIDER_MODEL)
    ap.add_argument("--agent-llm-args", default=LLM_ARGS_JSON)
    ap.add_argument("--user-llm-args", default=LLM_ARGS_JSON)
    ap.add_argument(
        "--agent",
        default="recuris_agent",
        help="'recuris_agent' for the skill arm, 'llm_agent' for the bare control",
    )
    ap.add_argument(
        "--skill-memory",
        default=None,
        help="Skill Memory package: a path, or a name under skill_memories/. "
        "Ignored when --agent is llm_agent.",
    )
    ap.add_argument(
        "--retrieval-config",
        default=os.environ.get("TAU2_RETRIEVAL_CONFIG"),
        help="retrieval variant for knowledge-base domains; falls back to the "
        "TAU2_RETRIEVAL_CONFIG environment variable so that subprocesses "
        "launched by the meta-agent driver inherit it",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="continue an interrupted run of the SAME code version. Without "
        "this flag a non-empty save directory is an error, so results from "
        "two code versions can never be silently mixed.",
    )
    ap.add_argument(
        "--open-downstream",
        action="store_true",
        help="the downstream agent is served elsewhere (a local open-weight "
        "endpoint, or a frontier provider) via --agent-llm/--agent-llm-args. "
        "The user simulator and the assertion judge stay frozen.",
    )
    return ap


def _resolve_treatment(ap: argparse.ArgumentParser, args) -> tuple[dict, dict]:
    if args.open_downstream:
        agent_args = validate_open_downstream_agent(
            agent_model=args.agent_llm, agent_args=args.agent_llm_args
        )
        # The user simulator stays on the frozen reference treatment.
        _, user_args = validate_frozen_treatment(
            agent_model=PROVIDER_MODEL,
            user_model=args.user_llm,
            agent_args=LLM_ARGS_JSON,
            user_args=args.user_llm_args,
        )
        return agent_args, user_args
    return validate_frozen_treatment(
        agent_model=args.agent_llm,
        user_model=args.user_llm,
        agent_args=args.agent_llm_args,
        user_args=args.user_llm_args,
    )


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.simulation_timeout is not None and args.simulation_timeout <= 0:
        ap.error("--simulation-timeout must be positive")
    if args.max_retries < 0:
        ap.error("--max-retries must be non-negative")
    if args.retry_delay < 0:
        ap.error("--retry-delay must be non-negative")

    try:
        agent_llm_args, user_llm_args = _resolve_treatment(ap, args)
    except ValueError as exc:
        ap.error(str(exc))

    try:
        root = tau2_root()
        data_dir = tau2_data_dir()
    except PathError as exc:
        ap.error(str(exc))

    if args.skill_memory:
        try:
            os.environ["RECURIS_SKILL_MEMORY"] = str(resolve_skill_memory(args.skill_memory))
        except PathError as exc:
            ap.error(str(exc))
    elif args.agent == "recuris_agent" and not os.getenv("RECURIS_SKILL_MEMORY"):
        ap.error(
            "--agent recuris_agent needs --skill-memory (or the "
            "RECURIS_SKILL_MEMORY environment variable)"
        )

    existing = data_dir / "simulations" / args.save_to / "results.json"
    if existing.exists() and existing.stat().st_size > 2 and not args.resume:
        ap.error(
            f"refusing to write into the existing run {args.save_to!r} ({existing}). "
            "Use a fresh save name, or pass --resume to continue an interrupted "
            "run of the same code version."
        )

    # Fingerprint output is append-only, so a fresh run must truncate a reused
    # path. Otherwise the previous run's lines stack on top of this one's,
    # inflating the aggregate counters and producing a false mismatch in the
    # scorecard. On --resume we keep appending, which is what continuing means.
    fp_path = os.getenv("RECURIS_FINGERPRINT")
    if fp_path and not args.resume:
        try:
            Path(fp_path).write_text("", encoding="utf-8")
        except OSError as exc:
            print(f"[recuris] could not truncate fingerprint file {fp_path}: {exc}")

    triple = treatment_triple()
    print(
        "[recuris] tau2 arm: domain=%s agent=%s trials=%d | treatment: "
        "gate_term=%s gate_term_wm=%s status_board=%s"
        % (
            args.domain,
            args.agent,
            args.num_trials,
            triple["gate_term"],
            triple["gate_term_wm"],
            triple["status_board"],
        )
    )

    from tau2.registry import registry

    from recuris.adapters.tau2.agent import create_recuris_agent

    registry.register_agent_factory(create_recuris_agent, "recuris_agent")

    from tau2.data_model.simulation import TextRunConfig
    from tau2.run import run_domain

    # tau2 records the benchmark revision by running `git rev-parse HEAD` in the
    # process working directory. Change directory only after every path above
    # has been resolved to an absolute location.
    os.chdir(root)

    config = TextRunConfig(
        domain=args.domain,
        task_ids=args.task_ids,
        num_trials=args.num_trials,
        max_concurrency=args.max_concurrency,
        timeout=args.simulation_timeout,
        max_retries=args.max_retries,
        retry_delay=args.retry_delay,
        agent=args.agent,
        llm_agent=args.agent_llm,
        llm_args_agent=agent_llm_args,
        llm_user=args.user_llm,
        llm_args_user=user_llm_args,
        save_to=args.save_to,
        # auto_resume only after the fresh-name guard above has cleared the
        # save directory as either empty or an explicit --resume.
        auto_resume=True,
        retrieval_config=args.retrieval_config,
    )
    results = run_domain(config)

    # Record the effective treatment next to the results so a published number
    # can always be traced back to the switches it ran under.
    out_dir = data_dir / "simulations" / args.save_to
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "_params.json").write_text(
            json.dumps(
                {
                    "domain": args.domain,
                    "agent": args.agent,
                    "skill_memory": os.getenv("RECURIS_SKILL_MEMORY", ""),
                    "num_trials": args.num_trials,
                    "agent_llm": args.agent_llm,
                    "agent_llm_args": agent_llm_args,
                    "user_llm": args.user_llm,
                    "user_llm_args": user_llm_args,
                    "treatment": triple,
                    "open_downstream": args.open_downstream,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[recuris] could not write _params.json: {exc}")

    print(
        f"[recuris] run complete: {getattr(results, 'timestamp', '')} "
        f"save_to={args.save_to}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
