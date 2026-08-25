"""The frozen tau2-Bench treatment, and the validators that enforce it.

This module performs validation only. It never imports tau2-Bench and never
calls a model, so importing it is free and it can be unit-tested offline.

Why validators rather than defaults
-----------------------------------
Every comparison in the paper is a *paired* contrast: two arms that differ in
exactly one thing, the Skill Memory. That only holds if the model id, the
decoding settings, the timeout and the retry policy are identical on both
sides. A typo in one launcher silently produces an inadmissible arm whose
number still looks plausible, and we have been bitten by exactly that. These
functions therefore fail closed: an arm either matches the declared treatment
or it does not start.

Two treatments exist:

``validate_frozen_treatment``
    Both the downstream agent and the user simulator are the frozen reference
    model. This is the configuration used for the meta-agent campaigns.

``validate_open_downstream_agent``
    The downstream agent is some other model reached over an OpenAI-compatible
    endpoint (a locally served open-weight model, or a frontier provider),
    while the user simulator and the assertion judge stay frozen. This is the
    configuration used for every transfer arm.
"""

from __future__ import annotations

import json
import os

# The frozen reference model. Both the user simulator and the NL-assertion
# judge stay pinned to this snapshot for every arm in the repository, including
# the arms whose downstream agent is a different model: the simulator and the
# judge are part of the evaluation protocol, not of the system under test.
DEFAULT_MODEL_ID = "doubao-seed-2-0-pro-260215"
MODEL_ID = os.getenv("RECURIS_TAU2_REFERENCE_MODEL", DEFAULT_MODEL_ID)
PROVIDER_MODEL = f"openai/{MODEL_ID}"

REASONING_EFFORT = "medium"
TEMPERATURE = 0.0
TIMEOUT = 360
NUM_RETRIES = 2
ALLOWED_OPENAI_PARAMS = ["reasoning_effort"]

LLM_ARG_KEYS = {
    "temperature",
    "timeout",
    "num_retries",
    "reasoning_effort",
    "allowed_openai_params",
}
LLM_ARGS = {
    "temperature": TEMPERATURE,
    "timeout": TIMEOUT,
    "num_retries": NUM_RETRIES,
    "reasoning_effort": REASONING_EFFORT,
    "allowed_openai_params": ALLOWED_OPENAI_PARAMS,
}
LLM_ARGS_JSON = json.dumps(LLM_ARGS, separators=(",", ":"))


def _parse_args(raw: str, label: str) -> dict:
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"{label} llm args are invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} llm args must be a JSON object")
    if set(parsed) != LLM_ARG_KEYS:
        raise ValueError(
            f"{label} llm args keys must exactly equal {sorted(LLM_ARG_KEYS)}"
        )
    if (
        isinstance(parsed["temperature"], bool)
        or not isinstance(parsed["temperature"], (int, float))
        or float(parsed["temperature"]) != TEMPERATURE
    ):
        raise ValueError(f"{label} temperature is frozen at {TEMPERATURE}")
    if parsed["reasoning_effort"] != REASONING_EFFORT:
        raise ValueError(f"{label} reasoning_effort is frozen at {REASONING_EFFORT!r}")
    if parsed["allowed_openai_params"] != ALLOWED_OPENAI_PARAMS:
        raise ValueError(f"{label} must allow only reasoning_effort through LiteLLM")
    if (
        isinstance(parsed["timeout"], bool)
        or not isinstance(parsed["timeout"], int)
        or parsed["timeout"] != TIMEOUT
    ):
        raise ValueError(f"{label} timeout is frozen at {TIMEOUT}")
    if (
        isinstance(parsed["num_retries"], bool)
        or not isinstance(parsed["num_retries"], int)
        or parsed["num_retries"] != NUM_RETRIES
    ):
        raise ValueError(f"{label} num_retries is frozen at {NUM_RETRIES}")
    return parsed


def validate_frozen_treatment(
    *, agent_model: str, user_model: str, agent_args: str, user_args: str
) -> tuple[dict, dict]:
    """Both sides on the frozen reference model. Returns the parsed arg dicts."""
    for label, model in (("agent", agent_model), ("user simulator", user_model)):
        if model != PROVIDER_MODEL:
            raise ValueError(f"{label} model is frozen at {PROVIDER_MODEL!r}")
    return _parse_args(agent_args, "agent"), _parse_args(user_args, "user simulator")


# Transfer arms: the downstream agent is served elsewhere. ``api_base`` and the
# sampling settings are required so that a bare and a skill arm cannot silently
# differ; provider-specific extras (stop tokens, reasoning effort, a key) are
# optional but must be named, because an unknown key in this dict is dropped
# silently by the client library and would produce an untraceable treatment.
OPEN_AGENT_REQUIRED_KEYS = {"api_base", "temperature", "timeout", "num_retries"}
OPEN_AGENT_OPTIONAL_KEYS = {
    "extra_body",
    "stop_token_ids",
    "max_tokens",
    "api_key",
    "reasoning_effort",
    "allowed_openai_params",
}


def validate_open_downstream_agent(*, agent_model: str, agent_args: str) -> dict:
    """Validate a transfer arm's downstream agent. Returns the parsed arg dict."""
    if agent_model == PROVIDER_MODEL:
        raise ValueError(
            "--open-downstream requires an agent other than the frozen reference "
            "model; drop the flag to run the frozen treatment"
        )
    if not agent_model.startswith("openai/"):
        raise ValueError(
            "the open downstream agent must be named 'openai/<served-model>' so "
            "that the OpenAI-compatible client is selected"
        )
    try:
        parsed = json.loads(agent_args)
    except Exception as exc:
        raise ValueError(f"open agent llm args are invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("open agent llm args must be a JSON object")
    missing = OPEN_AGENT_REQUIRED_KEYS - set(parsed)
    unknown = set(parsed) - OPEN_AGENT_REQUIRED_KEYS - OPEN_AGENT_OPTIONAL_KEYS
    if missing:
        raise ValueError(f"open agent llm args missing keys: {sorted(missing)}")
    if unknown:
        raise ValueError(f"open agent llm args unknown keys: {sorted(unknown)}")
    if float(parsed["temperature"]) != TEMPERATURE:
        raise ValueError(f"open agent temperature is frozen at {TEMPERATURE}")
    if parsed["timeout"] != TIMEOUT or parsed["num_retries"] != NUM_RETRIES:
        raise ValueError(
            f"open agent timeout/num_retries are frozen at {TIMEOUT}/{NUM_RETRIES}"
        )
    base = str(parsed["api_base"])
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"open agent api_base must be an http(s) URL, got {base!r}")
    return parsed


def treatment_triple() -> dict[str, bool]:
    """The three harness switches that define the effective tau2 treatment.

    These are read by the tau2 fork's orchestrator, not by the Recuris kernel,
    so they are easy to leave unset by accident -- which produces two arms that
    look identical on the command line and are not. Every shipped config
    declares all three explicitly, the run logs them, and they are recorded in
    each run's ``_params.json``.

    ``gate_term``
        terminal gate: refuse to end the episode while verified-pending work
        remains.
    ``gate_term_wm``
        the terminal gate consults the working-memory ledger rather than
        surface text.
    ``status_board``
        render the customer-visible status board on the user channel.
    """
    def flag(name: str) -> bool:
        return os.getenv(name, "") not in ("", "0", "false", "False")

    return {
        "gate_term": flag("TAU2_GATE_TERM"),
        "gate_term_wm": flag("TAU2_GATE_TERM_WM"),
        "status_board": flag("TAU2_STATUS_BOARD"),
    }


__all__ = [
    "ALLOWED_OPENAI_PARAMS",
    "LLM_ARGS",
    "LLM_ARGS_JSON",
    "MODEL_ID",
    "NUM_RETRIES",
    "OPEN_AGENT_OPTIONAL_KEYS",
    "OPEN_AGENT_REQUIRED_KEYS",
    "PROVIDER_MODEL",
    "REASONING_EFFORT",
    "TEMPERATURE",
    "TIMEOUT",
    "treatment_triple",
    "validate_frozen_treatment",
    "validate_open_downstream_agent",
]
