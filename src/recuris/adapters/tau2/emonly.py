"""The EM-only arm of the EM/WM coupling ablation.

This arm takes tau2's stock ``LLMAgent`` -- which has no working memory at all:
no ledger, no grounding, no truth guard, no status board, no gates -- and
statically concatenates every experiential card of a Skill Memory package into
its system prompt. It isolates "the cards, without any of the machinery that
decides when to use them".

It exists so that the EM/WM comparison cannot be answered with "you just gave
one arm more text". The Recuris kernel is not imported here, on purpose; the
only thing shared with the full arm is the card content and the treatment.

Invoked as ``recuris tau2-emonly ...``.
"""

from __future__ import annotations

import argparse
import os
import sys

from recuris.paths import PathError, resolve_skill_memory, tau2_root

from .treatment import (
    LLM_ARGS_JSON,
    PROVIDER_MODEL,
    validate_frozen_treatment,
    validate_open_downstream_agent,
)

PREAMBLE = (
    "Below are experiential skill cards distilled from prior tasks. Treat them "
    "as helpful reference for how to handle common situations and tool calls. "
    "They do not override the policy above.\n\n"
)


def load_cards(package_dir) -> tuple[str, int]:
    """Concatenate every card body in a package, frontmatter stripped."""
    files = sorted(package_dir.joinpath("em").rglob("*.md"))
    bodies = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("---", 3)
            body = text[end + 3:].strip() if end > 0 else text
        else:
            body = text.strip()
        if body:
            bodies.append(body)
    return "\n\n---\n\n".join(bodies), len(bodies)


def build_parser(prog: str = "recuris tau2-emonly") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__)
    ap.add_argument("--domain", default="airline")
    ap.add_argument("--skill-memory", default="tau2_airline")
    ap.add_argument("--save-to", required=True)
    ap.add_argument("--task-ids", nargs="*", default=None)
    ap.add_argument("--num-trials", type=int, default=4)
    ap.add_argument("--max-concurrency", type=int, default=4)
    ap.add_argument("--agent-llm", default=PROVIDER_MODEL)
    ap.add_argument("--agent-llm-args", default=LLM_ARGS_JSON)
    ap.add_argument("--user-llm", default=PROVIDER_MODEL)
    ap.add_argument("--user-llm-args", default=LLM_ARGS_JSON)
    ap.add_argument("--open-downstream", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    try:
        if args.open_downstream:
            agent_llm_args = validate_open_downstream_agent(
                agent_model=args.agent_llm, agent_args=args.agent_llm_args
            )
            _, user_llm_args = validate_frozen_treatment(
                agent_model=PROVIDER_MODEL,
                user_model=args.user_llm,
                agent_args=LLM_ARGS_JSON,
                user_args=args.user_llm_args,
            )
        else:
            agent_llm_args, user_llm_args = validate_frozen_treatment(
                agent_model=args.agent_llm,
                user_model=args.user_llm,
                agent_args=args.agent_llm_args,
                user_args=args.user_llm_args,
            )
    except ValueError as exc:
        ap.error(str(exc))

    try:
        package = resolve_skill_memory(args.skill_memory)
    except PathError as exc:
        ap.error(str(exc))

    cards, n = load_cards(package)
    print(
        f"[recuris] em-only: injecting {n} cards "
        f"({len(cards.split())} words) from {package.name}"
    )

    from tau2.agent.llm_agent import LLMAgent
    from tau2.registry import registry

    def create_em_only_agent(tools, domain_policy, **kwargs):
        augmented = (
            domain_policy
            + "\n\n<skill_memory>\n"
            + PREAMBLE
            + cards
            + "\n</skill_memory>"
        )
        return LLMAgent(
            tools=tools,
            domain_policy=augmented,
            llm=kwargs.get("llm"),
            llm_args=kwargs.get("llm_args"),
        )

    registry.register_agent_factory(create_em_only_agent, "em_only_agent")

    from tau2.data_model.simulation import TextRunConfig
    from tau2.run import run_domain

    # tau2 reads the benchmark revision from the process working directory.
    os.chdir(tau2_root())

    run_domain(
        TextRunConfig(
            domain=args.domain,
            task_ids=args.task_ids,
            num_trials=args.num_trials,
            max_concurrency=args.max_concurrency,
            agent="em_only_agent",
            llm_agent=args.agent_llm,
            llm_args_agent=agent_llm_args,
            llm_user=args.user_llm,
            llm_args_user=user_llm_args,
            save_to=args.save_to,
            auto_resume=True,
        )
    )
    print(f"[recuris] em-only run complete: save_to={args.save_to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
