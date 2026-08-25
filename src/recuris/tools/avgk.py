"""Validate Tau2 result matrices and report Avg@k and Pass@k.

Avg@k is the mean reward over the first ``k`` trials of every task. Pass@k is
the fraction of tasks where at least one of the first ``k`` trials succeeds.

The validator fails closed on missing/duplicate trials, infrastructure errors,
or missing rewards so partial runs cannot be mistaken for baseline scores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

OFFICIAL_TAU2_COMMIT = "fc0055dc4e0a316c3f83133267fbd6faaa770992"
DOUBAO_MODEL = "openai/doubao-seed-2-0-pro-260215"
SEED = 300
MAX_STEPS = 200
MAX_ERRORS = 10
MAX_CONCURRENCY = 3
LLM_TIMEOUT = 360
LLM_RETRIES = 2
OFFICIAL_FULL_BASE_TASK_IDS = {
    "airline": frozenset(str(task_id) for task_id in range(50)),
    "retail": frozenset(str(task_id) for task_id in range(114)),
}
_VOLATILE_PROTOCOL_KEYS = {
    "attempts",
    "completed_at",
    "configuration_hash",
    "created_at",
    "error_message",
    "error_type",
    "status",
}


class ResultValidationError(ValueError):
    """Raised when a result file is not a complete, scoreable trial matrix."""


def _load_result(path: Path) -> dict[str, Any]:
    if path.is_dir():
        path = path / "results.json"
    if not path.is_file():
        raise ResultValidationError(f"result file does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResultValidationError(f"cannot read result file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ResultValidationError("result root must be a JSON object")
    return data


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ResultValidationError(f"manifest file does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResultValidationError(f"cannot read manifest file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ResultValidationError("manifest root must be a JSON object")
    return data


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _configuration_hash(protocol: dict[str, Any]) -> str:
    configuration = {
        key: value
        for key, value in protocol.items()
        if key not in _VOLATILE_PROTOCOL_KEYS
    }
    encoded = json.dumps(
        configuration,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_positive_ks(values: Iterable[int], label: str) -> tuple[int, ...]:
    ks = tuple(sorted(set(values)))
    if not ks or any(isinstance(k, bool) or not isinstance(k, int) or k <= 0 for k in ks):
        raise ResultValidationError(f"{label} must contain positive integers")
    return ks


def _simulation_error(simulation: dict[str, Any]) -> str | None:
    if simulation.get("termination_reason") == "infrastructure_error":
        return "termination_reason: infrastructure_error"
    info = simulation.get("info")
    if isinstance(info, dict):
        for key in ("error", "infrastructure_error", "exception"):
            if info.get(key):
                return f"{key}: {info[key]}"
    for key in ("error", "infrastructure_error", "exception"):
        if simulation.get(key):
            return f"{key}: {simulation[key]}"
    return None


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResultValidationError(f"{label} must be an object")
    return value


def _require_field(mapping: dict[str, Any], key: str, label: str) -> Any:
    if key not in mapping:
        raise ResultValidationError(f"{label} is missing")
    value = mapping[key]
    if value is None:
        raise ResultValidationError(f"{label} must not be null")
    return value


def _require_match(label: str, manifest_value: Any, result_value: Any) -> None:
    if manifest_value != result_value:
        raise ResultValidationError(
            f"manifest {label} mismatch: "
            f"manifest={manifest_value!r}, result={result_value!r}"
        )


def _validate_formal_deployment(
    manifest: dict[str, Any],
    *,
    agent_model: str,
    agent_api_base: str,
) -> None:
    deployment = _require_mapping(
        manifest.get("deployment"), "manifest deployment"
    )
    if deployment.get("status") != "ready":
        raise ResultValidationError("manifest deployment must be ready")
    if not agent_model.startswith("openai/"):
        raise ResultValidationError(
            "formal agent model must use openai/<served-name>"
        )
    served_model = agent_model.removeprefix("openai/")
    _require_match(
        "deployment.served_model",
        _require_field(
            deployment,
            "served_model",
            "manifest deployment.served_model",
        ),
        served_model,
    )
    _require_match(
        "deployment.api_base",
        _require_field(
            deployment,
            "api_base",
            "manifest deployment.api_base",
        ),
        agent_api_base,
    )
    for field in (
        "config_sha256",
        "weights_fingerprint",
    ):
        value = _require_field(
            deployment,
            field,
            f"manifest deployment.{field}",
        )
        if not isinstance(value, str) or len(value) != 64:
            raise ResultValidationError(
                f"manifest deployment.{field} must be a SHA-256 digest"
            )
    runtime = _require_mapping(
        deployment.get("runtime"), "manifest deployment.runtime"
    )
    _require_field(
        runtime,
        "version",
        "manifest deployment.runtime.version",
    )
    launch = _require_mapping(
        deployment.get("launch"), "manifest deployment.launch"
    )
    argv = _require_field(
        launch,
        "argv",
        "manifest deployment.launch.argv",
    )
    if not isinstance(argv, list) or not argv:
        raise ResultValidationError(
            "manifest deployment.launch.argv must be a non-empty list"
        )
    tool_smoke = _require_mapping(
        deployment.get("tool_smoke"), "manifest deployment.tool_smoke"
    )
    if tool_smoke.get("status") != "passed":
        raise ResultValidationError(
            "manifest deployment tool smoke must be passed"
        )

    deployment_path_value = _require_field(
        manifest,
        "deployment_manifest_path",
        "manifest deployment_manifest_path",
    )
    deployment_sha256 = _require_field(
        manifest,
        "deployment_manifest_sha256",
        "manifest deployment_manifest_sha256",
    )
    if not isinstance(deployment_path_value, str):
        raise ResultValidationError(
            "manifest deployment_manifest_path must be a string"
        )
    deployment_path = Path(deployment_path_value)
    if not deployment_path.is_file():
        raise ResultValidationError(
            f"deployment manifest file does not exist: {deployment_path}"
        )
    if _sha256_file(deployment_path) != deployment_sha256:
        raise ResultValidationError("deployment manifest SHA-256 mismatch")
    try:
        external_deployment = json.loads(
            deployment_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ResultValidationError(
            f"cannot read bound deployment manifest: {exc}"
        ) from exc
    if external_deployment != deployment:
        raise ResultValidationError(
            "embedded deployment does not match its bound manifest"
        )


def _validate_manifest(
    manifest: dict[str, Any],
    info: dict[str, Any],
    *,
    result_dir: Path | None,
    formal: bool,
) -> None:
    if result_dir is None:
        raise ResultValidationError(
            "result_dir is required when validating a manifest"
        )
    if manifest.get("status") != "process_finished_unvalidated":
        raise ResultValidationError(
            "manifest status must be 'process_finished_unvalidated'"
        )

    agent_manifest = _require_mapping(manifest.get("agent"), "manifest agent")
    user_manifest = _require_mapping(
        manifest.get("user_simulator"), "manifest user_simulator"
    )
    agent_info = _require_mapping(info.get("agent_info"), "result agent_info")
    user_info = _require_mapping(info.get("user_info"), "result user_info")
    environment_info = _require_mapping(
        info.get("environment_info"), "result environment_info"
    )
    agent_args = _require_mapping(
        agent_info.get("llm_args"), "result agent_info.llm_args"
    )
    user_args = _require_mapping(
        user_info.get("llm_args"), "result user_info.llm_args"
    )

    bindings = (
        (
            "tau2_commit",
            _require_field(manifest, "tau2_commit", "manifest tau2_commit"),
            _require_field(info, "git_commit", "result git_commit"),
        ),
        (
            "domain",
            _require_field(manifest, "domain", "manifest domain"),
            _require_field(
                environment_info,
                "domain_name",
                "result environment_info.domain_name",
            ),
        ),
        (
            "trials",
            _require_field(manifest, "trials", "manifest trials"),
            _require_field(info, "num_trials", "result num_trials"),
        ),
        (
            "seed",
            _require_field(manifest, "seed", "manifest seed"),
            _require_field(info, "seed", "result seed"),
        ),
        (
            "max_steps",
            _require_field(manifest, "max_steps", "manifest max_steps"),
            _require_field(info, "max_steps", "result max_steps"),
        ),
        (
            "max_errors",
            _require_field(manifest, "max_errors", "manifest max_errors"),
            _require_field(info, "max_errors", "result max_errors"),
        ),
        (
            "agent.model",
            _require_field(agent_manifest, "model", "manifest agent.model"),
            _require_field(agent_info, "llm", "result agent_info.llm"),
        ),
        (
            "agent.api_base",
            _require_field(
                agent_manifest,
                "api_base",
                "manifest agent.api_base",
            ),
            _require_field(
                agent_args,
                "api_base",
                "result agent_info.llm_args.api_base",
            ),
        ),
        (
            "agent.temperature",
            _require_field(
                agent_manifest,
                "temperature",
                "manifest agent.temperature",
            ),
            _require_field(
                agent_args,
                "temperature",
                "result agent_info.llm_args.temperature",
            ),
        ),
        (
            "agent.timeout",
            _require_field(
                agent_manifest,
                "timeout",
                "manifest agent.timeout",
            ),
            _require_field(
                agent_args,
                "timeout",
                "result agent_info.llm_args.timeout",
            ),
        ),
        (
            "agent.num_retries",
            _require_field(
                agent_manifest,
                "num_retries",
                "manifest agent.num_retries",
            ),
            _require_field(
                agent_args,
                "num_retries",
                "result agent_info.llm_args.num_retries",
            ),
        ),
        (
            "user_simulator.model",
            _require_field(
                user_manifest,
                "model",
                "manifest user_simulator.model",
            ),
            _require_field(user_info, "llm", "result user_info.llm"),
        ),
        (
            "user_simulator.reasoning_effort",
            _require_field(
                user_manifest,
                "reasoning_effort",
                "manifest user_simulator.reasoning_effort",
            ),
            _require_field(
                user_args,
                "reasoning_effort",
                "result user_info.llm_args.reasoning_effort",
            ),
        ),
        (
            "user_simulator.temperature",
            _require_field(
                user_manifest,
                "temperature",
                "manifest user_simulator.temperature",
            ),
            _require_field(
                user_args,
                "temperature",
                "result user_info.llm_args.temperature",
            ),
        ),
        (
            "user_simulator.timeout",
            _require_field(
                user_manifest,
                "timeout",
                "manifest user_simulator.timeout",
            ),
            _require_field(
                user_args,
                "timeout",
                "result user_info.llm_args.timeout",
            ),
        ),
        (
            "user_simulator.num_retries",
            _require_field(
                user_manifest,
                "num_retries",
                "manifest user_simulator.num_retries",
            ),
            _require_field(
                user_args,
                "num_retries",
                "result user_info.llm_args.num_retries",
            ),
        ),
    )
    for label, manifest_value, result_value in bindings:
        _require_match(label, manifest_value, result_value)
    _require_match(
        "agent.extra_body",
        agent_manifest.get("extra_body"),
        agent_args.get("extra_body"),
    )

    if agent_info.get("implementation") != "llm_agent":
        raise ResultValidationError(
            "result agent implementation must be 'llm_agent'"
        )
    if user_info.get("implementation") != "user_simulator":
        raise ResultValidationError(
            "result user implementation must be 'user_simulator'"
        )
    exact_protocol = (
        ("seed", manifest.get("seed"), SEED),
        ("max_steps", manifest.get("max_steps"), MAX_STEPS),
        ("max_errors", manifest.get("max_errors"), MAX_ERRORS),
        (
            "max_concurrency",
            manifest.get("max_concurrency"),
            MAX_CONCURRENCY,
        ),
        ("agent.temperature", agent_manifest.get("temperature"), 0.0),
        ("agent.timeout", agent_manifest.get("timeout"), LLM_TIMEOUT),
        ("agent.num_retries", agent_manifest.get("num_retries"), LLM_RETRIES),
        ("user_simulator.temperature", user_manifest.get("temperature"), 0.0),
        (
            "user_simulator.timeout",
            user_manifest.get("timeout"),
            LLM_TIMEOUT,
        ),
        (
            "user_simulator.num_retries",
            user_manifest.get("num_retries"),
            LLM_RETRIES,
        ),
    )
    for label, actual, expected in exact_protocol:
        if actual != expected:
            raise ResultValidationError(
                f"manifest {label} must be {expected!r}, got {actual!r}"
            )
    if user_manifest.get("model") != DOUBAO_MODEL:
        raise ResultValidationError(
            f"manifest user_simulator.model must be {DOUBAO_MODEL!r}"
        )
    if user_manifest.get("reasoning_effort") != "medium":
        raise ResultValidationError(
            "manifest user_simulator.reasoning_effort must be 'medium'"
        )
    if manifest.get("task_split") != "base":
        raise ResultValidationError("manifest task_split must be 'base'")
    _require_match(
        "save_to",
        _require_field(manifest, "save_to", "manifest save_to"),
        result_dir.resolve().name,
    )

    if environment_info.get("domain_name") == "retail":
        judge = _require_mapping(
            manifest.get("nl_assertion_judge"),
            "manifest nl_assertion_judge",
        )
        if judge.get("model") != DOUBAO_MODEL:
            raise ResultValidationError(
                f"Retail judge model must be {DOUBAO_MODEL!r}"
            )
        if judge.get("reasoning_effort") != "medium":
            raise ResultValidationError(
                "Retail judge reasoning_effort must be 'medium'"
            )
        for field, expected in (
            ("temperature", 0.0),
            ("timeout", LLM_TIMEOUT),
            ("num_retries", LLM_RETRIES),
        ):
            if judge.get(field) != expected:
                raise ResultValidationError(
                    f"Retail judge {field} must be {expected!r}"
                )
        comparability = judge.get("comparability")
        if (
            not isinstance(comparability, str)
            or "internal" not in comparability.lower()
            or "not directly comparable" not in comparability.lower()
        ):
            raise ResultValidationError(
                "Retail judge must declare internal, not-directly-comparable "
                "scoring"
            )
        attempts = manifest.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            raise ResultValidationError(
                "Retail manifest must record at least one run attempt"
            )
        last_attempt = _require_mapping(
            attempts[-1], "manifest last attempt"
        )
        binding = _require_mapping(
            last_attempt.get("judge_binding"),
            "manifest last attempt judge_binding",
        )
        binding_args = _require_mapping(
            binding.get("args"),
            "manifest last attempt judge_binding.args",
        )
        if (
            binding.get("model") != DOUBAO_MODEL
            or binding_args.get("reasoning_effort") != "medium"
        ):
            raise ResultValidationError(
                "Retail runtime judge binding is not Doubao medium"
            )

    if formal:
        if manifest.get("task_ids") is not None:
            raise ResultValidationError(
                "formal full-base manifest task_ids must be null"
            )
        _validate_formal_deployment(
            manifest,
            agent_model=agent_info["llm"],
            agent_api_base=agent_args["api_base"],
        )
    runner_sha256 = manifest.get("runner_sha256")
    if not isinstance(runner_sha256, str) or len(runner_sha256) != 64:
        raise ResultValidationError(
            "manifest runner_sha256 must be a SHA-256 digest"
        )
    if manifest.get("configuration_hash") != _configuration_hash(manifest):
        raise ResultValidationError("manifest configuration hash is invalid")


def _validate_official_full_base(
    info: dict[str, Any],
    declared_task_ids: list[str],
    domain: str,
) -> None:
    expected_task_ids = OFFICIAL_FULL_BASE_TASK_IDS.get(domain)
    if expected_task_ids is None:
        raise ResultValidationError(
            f"unsupported official full-base domain: {domain!r}"
        )
    environment_info = _require_mapping(
        info.get("environment_info"), "result environment_info"
    )
    if info.get("git_commit") != OFFICIAL_TAU2_COMMIT:
        raise ResultValidationError(
            "official full-base git commit mismatch: "
            f"expected {OFFICIAL_TAU2_COMMIT}, got {info.get('git_commit')}"
        )
    if environment_info.get("domain_name") != domain:
        raise ResultValidationError(
            "official full-base domain mismatch: "
            f"expected {domain!r}, got {environment_info.get('domain_name')!r}"
        )
    actual_task_ids = set(declared_task_ids)
    if actual_task_ids != expected_task_ids:
        raise ResultValidationError(
            f"official {domain} full-base task set mismatch; "
            f"missing={sorted(expected_task_ids - actual_task_ids, key=int)}, "
            f"extra={sorted(actual_task_ids - expected_task_ids)}"
        )


def summarize(
    data: dict[str, Any],
    *,
    avg_ks: Iterable[int] = (4, 8),
    pass_ks: Iterable[int] = (1, 4, 8),
    expected_git_commit: str | None = None,
    manifest: dict[str, Any] | None = None,
    result_dir: Path | None = None,
    official_full_base_domain: str | None = None,
) -> dict[str, Any]:
    """Validate one Tau2 result object and compute its aggregate metrics."""
    avg_ks = _as_positive_ks(avg_ks, "avg_ks")
    pass_ks = _as_positive_ks(pass_ks, "pass_ks")
    required_trials = max(max(avg_ks), max(pass_ks))

    info = data.get("info")
    if not isinstance(info, dict):
        raise ResultValidationError("missing result info")
    if expected_git_commit is not None and info.get("git_commit") != expected_git_commit:
        raise ResultValidationError(
            "git commit mismatch: "
            f"expected {expected_git_commit}, got {info.get('git_commit')}"
        )
    if info.get("num_trials") != required_trials:
        raise ResultValidationError(
            f"result declares num_trials={info.get('num_trials')}; "
            f"expected exactly {required_trials}"
        )

    simulations = data.get("simulations")
    if not isinstance(simulations, list) or not simulations:
        raise ResultValidationError("result contains no simulations")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ResultValidationError("result contains no declared tasks")
    declared_task_ids: list[str] = []
    for index, task in enumerate(tasks):
        task_id = task.get("id") if isinstance(task, dict) else task
        if task_id is None:
            raise ResultValidationError(f"declared task {index} has no id")
        declared_task_ids.append(str(task_id))
    if len(declared_task_ids) != len(set(declared_task_ids)):
        raise ResultValidationError("declared task list contains duplicate ids")
    if official_full_base_domain is not None:
        _validate_official_full_base(
            info,
            declared_task_ids,
            official_full_base_domain,
        )
        if manifest is None:
            raise ResultValidationError(
                "official full-base scoring requires a protocol manifest"
            )
    if manifest is not None:
        _validate_manifest(
            manifest,
            info,
            result_dir=result_dir,
            formal=official_full_base_domain is not None,
        )

    matrix: dict[str, dict[int, float]] = defaultdict(dict)
    for index, simulation in enumerate(simulations):
        if not isinstance(simulation, dict):
            raise ResultValidationError(f"simulation {index} is not an object")
        error = _simulation_error(simulation)
        if error is not None:
            raise ResultValidationError(
                f"simulation {index} has an infrastructure error: {error}"
            )

        task_id = simulation.get("task_id")
        if task_id is None:
            raise ResultValidationError(f"simulation {index} has no task_id")
        task_key = str(task_id)
        trial = simulation.get("trial")
        if isinstance(trial, bool) or not isinstance(trial, int):
            raise ResultValidationError(
                f"simulation {index} has invalid trial value: {trial!r}"
            )
        if trial < 0 or trial >= required_trials:
            raise ResultValidationError(
                f"task {task_key} has out-of-range trial {trial}; "
                f"expected 0..{required_trials - 1}"
            )
        if trial in matrix[task_key]:
            raise ResultValidationError(
                f"task {task_key} has duplicate trial {trial}"
            )

        reward_info = simulation.get("reward_info")
        reward = reward_info.get("reward") if isinstance(reward_info, dict) else None
        if isinstance(reward, bool) or not isinstance(reward, (int, float)):
            raise ResultValidationError(
                f"task {task_key} trial {trial} has no numeric reward"
            )
        reward = float(reward)
        if not math.isfinite(reward):
            raise ResultValidationError(
                f"task {task_key} trial {trial} has non-finite reward"
            )
        matrix[task_key][trial] = reward

    expected_trial_set = set(range(required_trials))
    for task_id, trials in matrix.items():
        actual_trial_set = set(trials)
        if actual_trial_set != expected_trial_set:
            missing = sorted(expected_trial_set - actual_trial_set)
            extra = sorted(actual_trial_set - expected_trial_set)
            raise ResultValidationError(
                f"task {task_id} has an incomplete trial matrix; "
                f"missing={missing}, extra={extra}"
            )

    task_ids = sorted(matrix)
    declared_task_set = set(declared_task_ids)
    observed_task_set = set(task_ids)
    if observed_task_set != declared_task_set:
        raise ResultValidationError(
            "observed task set does not match declared tasks; "
            f"missing={sorted(declared_task_set - observed_task_set)}, "
            f"extra={sorted(observed_task_set - declared_task_set)}"
        )
    expected_simulations = len(task_ids) * required_trials
    if len(simulations) != expected_simulations:
        raise ResultValidationError(
            f"result has {len(simulations)} simulations; "
            f"expected {expected_simulations}"
        )
    avg_metrics = {
        f"Avg@{k}": sum(
            matrix[task_id][trial]
            for task_id in task_ids
            for trial in range(k)
        )
        / (len(task_ids) * k)
        for k in avg_ks
    }

    pass_metrics: dict[str, float] = {}
    for k in pass_ks:
        per_task = []
        for task_id in task_ids:
            per_task.append(
                1.0
                if any(
                    math.isclose(matrix[task_id][trial], 1.0)
                    for trial in range(k)
                )
                else 0.0
            )
        pass_metrics[f"Pass@{k}"] = sum(per_task) / len(per_task)

    agent_info = info.get("agent_info")
    environment_info = info.get("environment_info")
    return {
        "validated": True,
        "git_commit": info.get("git_commit"),
        "domain": (
            environment_info.get("domain_name")
            if isinstance(environment_info, dict)
            else None
        ),
        "model": agent_info.get("llm") if isinstance(agent_info, dict) else None,
        "tasks": len(task_ids),
        "trials_per_task": required_trials,
        "simulations": len(simulations),
        "manifest_validated": manifest is not None,
        "official_full_base_domain": official_full_base_domain,
        **avg_metrics,
        **pass_metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="results.json or its run directory")
    parser.add_argument("--avg-k", type=int, nargs="+", default=[4, 8])
    parser.add_argument("--pass-k", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--expected-git-commit")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="protocol manifest to bind to this result directory",
    )
    parser.add_argument(
        "--official-full-base-domain",
        choices=sorted(OFFICIAL_FULL_BASE_TASK_IDS),
        help="require the exact official base task set and v1.0.1 commit",
    )
    parser.add_argument("--json", action="store_true", help="print compact JSON")
    args = parser.parse_args()
    requested_max_k = max((*args.avg_k, *args.pass_k))
    if requested_max_k >= 4 and args.official_full_base_domain is None:
        parser.error(
            "Avg@4/8 or Pass@4/8 scoring requires "
            "--official-full-base-domain"
        )
    if args.official_full_base_domain and args.manifest is None:
        parser.error("--official-full-base-domain requires --manifest")

    try:
        result_file = args.result / "results.json" if args.result.is_dir() else args.result
        summary = summarize(
            _load_result(result_file),
            avg_ks=args.avg_k,
            pass_ks=args.pass_k,
            expected_git_commit=args.expected_git_commit,
            manifest=_load_manifest(args.manifest) if args.manifest else None,
            result_dir=result_file.parent,
            official_full_base_domain=args.official_full_base_domain,
        )
    except ResultValidationError as exc:
        parser.error(str(exc))

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    print(
        f"validated {summary['tasks']} tasks x "
        f"{summary['trials_per_task']} trials "
        f"({summary['simulations']} simulations)"
    )
    print(
        f"domain={summary['domain']} model={summary['model']} "
        f"git_commit={summary['git_commit']}"
    )
    for name, value in summary.items():
        if name.startswith("Avg@") or name.startswith("Pass@"):
            print(f"{name}: {value:.6f}")


if __name__ == "__main__":
    main()
