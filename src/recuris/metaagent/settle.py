"""Fresh, fail-closed evaluation-only A/B for an already frozen Meta-Agent candidate.

The orchestrator intentionally does not expose any editing or Meta-Agent tools.
It evaluates immutable package bytes in preregistered task shards, persists each
completed shard independently, and only reports a gate after exact coverage and
pairing checks pass.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import re
import statistics
import time
import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from recuris.metaagent.downstream import (
    EMBEDDED_FINGERPRINT_KEY,
    FORMAL_ALLOWED_OPENAI_PARAMS,
    FORMAL_DOWNSTREAM_MODEL,
    FORMAL_DOWNSTREAM_REASONING,
    FORMAL_DOWNSTREAM_TEMPERATURE,
    RecurisDownstream,
    _package_tree_sha256,
)
from recuris.metaagent.driver import Driver
from recuris.metaagent.eval_run_guard import (
    EvaluationRunLockError,
    InflightJournalError,
    RunFileLock,
    begin_inflight,
    clear_inflight,
    make_inflight_record,
    reconcile_inflight,
)
from recuris.metaagent.gates import held_out_paired_gate

SCHEMA_VERSION = "recuris-eval-only-ab-v1"
ARMS = ("base", "candidate")
PHASES = ("dev", "repair")
TAU2_BASE_SEED = 300

V2_GATE_MODE = "short-repair-v2"
V2_FIXED_TASKS = {
    "repair": ["38"],
    "guard": ["50", "33", "36", "86"],
}
V2_FIXED_TRIALS = {
    "repair": {
        "num_trials": 6,
        "expected_trial_seeds": [
            626729, 373753, 361454, 1567, 514337, 363271,
        ],
    },
    "guard": {
        "num_trials": 2,
        "expected_trial_seeds": [626729, 373753],
    },
}
V2_FIXED_GATE = {
    "mode": V2_GATE_MODE,
    "repair_block_min_net_fixes": 2,
    "safety_task_id": "50",
    "safety_new_failure_cap": 0,
    "other_guard_task_ids": ["33", "36", "86"],
    "other_guard_new_failure_cap": 1,
    "require_overall_net_positive": True,
    "mechanism_min_causal_cells_per_repair_block": 1,
}
V2_ALLOWED_META_MODELS = {
    "doubao-seed-evolving-latest-version",
    "doubao-seed-2-1-pro-260628",
}
V2_COMPARISON_EVALUATOR = "recuris.metaagent.settle"
V2_OTHER_GENERATION_OUTCOMES = {
    "harness-failure",
    "invalid-plan",
    "all-ceiling",
    "patch-failed",
    "upstream-model-failure",
    "profile-ineligible",
}
V2_REPAIR_BLOCKS = ("repair_forward", "repair_reverse")
V2_GUARD_ROLE_BY_TASK = {
    "50": "safety",
    "33": "fallback",
    "36": "near_neighbor",
    "86": "neutral",
}


class EvaluationProtocolError(RuntimeError):
    """The preregistration, immutable inputs, or completed evidence is invalid."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _load_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise EvaluationProtocolError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluationProtocolError(f"{label} must contain a JSON object")
    return payload


def _safe_label(value: object, label: str) -> str:
    text = str(value)
    if not text or any(not (char.isalnum() or char in "._-") for char in text):
        raise EvaluationProtocolError(f"{label} is not a safe path label")
    return text


def _task_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise EvaluationProtocolError(f"{label} must be a list")
    tasks = [str(task) for task in value]
    if (not tasks or len(tasks) != len(set(tasks)) or
            any(not task.isdigit() for task in tasks)):
        raise EvaluationProtocolError(
            f"{label} must contain unique numeric task ids"
        )
    return tasks


def _valid_sha256(value: object) -> bool:
    text = str(value or "").lower()
    return (
        len(text) == 64 and
        all(char in "0123456789abcdef" for char in text)
    )


def _phase_trial_settings(protocol: dict, phase: str) -> tuple[int, list[int]]:
    execution = protocol.get("execution") or {}
    phase_trials = execution.get("phase_trials")
    settings = (
        phase_trials.get(phase)
        if isinstance(phase_trials, dict) else None
    )
    if settings is None:
        settings = execution
    if not isinstance(settings, dict):
        raise EvaluationProtocolError(
            f"execution settings for phase {phase!r} are missing"
        )
    num_trials = settings.get("num_trials")
    expected_seeds = settings.get("expected_trial_seeds")
    if (not isinstance(num_trials, int) or isinstance(num_trials, bool) or
            num_trials < 1):
        raise EvaluationProtocolError(
            f"execution settings for phase {phase!r} have invalid num_trials"
        )
    if (not isinstance(expected_seeds, list) or
            len(expected_seeds) != num_trials or
            any(not isinstance(seed, int) or isinstance(seed, bool)
                for seed in expected_seeds)):
        raise EvaluationProtocolError(
            f"execution settings for phase {phase!r} have invalid trial seeds"
        )
    return num_trials, list(expected_seeds)


def _tau2_trial_seeds(num_trials: int) -> list[int]:
    generator = random.Random(TAU2_BASE_SEED)
    return [
        generator.randint(0, 1_000_000)
        for _ in range(num_trials)
    ]


def _resolve_bound_path(
        raw_path: object, *, base_dir: Path, label: str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise EvaluationProtocolError(f"{label}.path is missing")
    path = Path(raw_path)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    else:
        path = path.resolve()
    return path


def _expected_generation_binding(
        expected: object, record: dict, *,
        tasks: dict[str, list[str]], guard_phase: str,
) -> dict:
    required_keys = {
        "run_id", "round", "profile_id", "raw_spec_sha256",
        "substantive_input_sha256", "treatment_sha256",
        "meta_model", "meta_reasoning_effort", "meta_temperature",
    }
    if (not isinstance(expected, dict) or
            not required_keys <= set(expected) or
            set(expected) - required_keys - {"task_aliases"}):
        raise EvaluationProtocolError(
            "expected_generation has invalid fields"
        )
    run_id = _safe_label(expected.get("run_id"), "expected_generation.run_id")
    round_number = expected.get("round")
    profile_id = _safe_label(
        expected.get("profile_id"), "expected_generation.profile_id",
    )
    if round_number != 1:
        raise EvaluationProtocolError(
            "expected_generation.round must be 1"
        )
    for field in (
            "raw_spec_sha256", "substantive_input_sha256",
            "treatment_sha256"):
        if not _valid_sha256(expected.get(field)):
            raise EvaluationProtocolError(
                f"expected_generation.{field} is invalid"
            )
    meta_model = str(expected.get("meta_model") or "")
    if (not meta_model or
            expected.get("meta_reasoning_effort") != "high" or
            expected.get("meta_temperature") != 0):
        raise EvaluationProtocolError(
            "expected_generation meta treatment must be exact model/high/0"
        )
    aliases = expected.get("task_aliases")
    if (not isinstance(aliases, dict) or not aliases or
            any(not isinstance(key, str) or key.isdigit() or
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", key) or
                not str(value).isdigit()
                for key, value in aliases.items()) or
            len({str(value) for value in aliases.values()}) != len(aliases)):
        raise EvaluationProtocolError(
            "expected_generation.task_aliases is invalid"
        )
    aliases = {str(key): str(value) for key, value in aliases.items()}

    if (record.get("run_id") != run_id or
            record.get("round") != round_number):
        raise EvaluationProtocolError(
            "candidate freeze generation run_id/round mismatch"
        )
    meta_agent = record.get("meta_agent")
    if (not isinstance(meta_agent, dict) or
            meta_agent.get("model") != meta_model or
            meta_agent.get("reasoning_effort") != "high" or
            meta_agent.get("temperature") != 0):
        raise EvaluationProtocolError(
            "candidate freeze meta treatment mismatch"
        )
    sessions = meta_agent.get("sessions")
    if not isinstance(sessions, list):
        raise EvaluationProtocolError(
            "candidate freeze lacks generation sessions"
        )
    phase_sessions = {"d": [], "p": []}
    for session in sessions:
        if not isinstance(session, dict):
            raise EvaluationProtocolError(
                "candidate freeze contains an invalid generation session"
            )
        name = str(session.get("name") or "")
        match = re.fullmatch(rf"r{round_number}_([dp])\d+", name)
        if not match:
            continue
        phase_sessions[match.group(1)].append(session)
        upstream_models = session.get("upstream_models")
        if (
                session.get("requested_model") != meta_model or
                session.get("requested_reasoning_effort") != "high" or
                session.get("requested_temperature") != 0 or
                upstream_models not in ([], [meta_model]) or
                (upstream_models and
                 session.get("upstream_model_verified") is not True) or
                session.get("read_scope_violations") != [] or
                session.get("edit_scope_violations") != [] or
                session.get("stray_writes") != [] or
                session.get("profile_tool_surface_verified") is not True or
                not isinstance(session.get("tool_surface"), list) or
                len(session["tool_surface"]) != 2 or
                set(session["tool_surface"]) != {"Read", "Edit"}
        ):
            raise EvaluationProtocolError(
                f"candidate freeze generation session {name!r} is inadmissible"
            )
        if (session.get("ok") is True and
                (upstream_models != [meta_model] or
                 session.get("upstream_model_verified") is not True)):
            raise EvaluationProtocolError(
                f"successful generation session {name!r} lacks exact "
                "upstream-model proof"
            )
    if (not any(session.get("ok") is True
                for session in phase_sessions["d"]) or
            not any(session.get("ok") is True
                    for session in phase_sessions["p"])):
        raise EvaluationProtocolError(
            "candidate freeze must contain a successful admissible D and P session"
        )

    profile = record.get("generation_profile")
    if not isinstance(profile, dict):
        raise EvaluationProtocolError(
            "candidate freeze lacks locked generation profile"
        )
    provenance = profile.get("provenance")
    manifest = profile.get("input_manifest")
    if not isinstance(provenance, dict) or not isinstance(manifest, dict):
        raise EvaluationProtocolError(
            "candidate freeze generation profile is incomplete"
        )
    expected_hashes = {
        "raw": str(expected["raw_spec_sha256"]).lower(),
        "substantive": str(expected["substantive_input_sha256"]).lower(),
        "treatment": str(expected["treatment_sha256"]).lower(),
    }
    if (
            provenance.get("profile_id") != profile_id or
            manifest.get("profile_id") != profile_id or
            provenance.get("expected_raw_spec_sha256") !=
            expected_hashes["raw"] or
            provenance.get("materialized_spec_sha256") !=
            expected_hashes["raw"] or
            manifest.get("raw_spec_sha256") != expected_hashes["raw"] or
            provenance.get("substantive_input_sha256") !=
            expected_hashes["substantive"] or
            manifest.get("substantive_input_sha256") !=
            expected_hashes["substantive"] or
            provenance.get("treatment_sha256") !=
            expected_hashes["treatment"] or
            manifest.get("treatment_sha256") != expected_hashes["treatment"]
    ):
        raise EvaluationProtocolError(
            "candidate freeze generation profile/treatment mismatch"
        )
    candidate = record.get("candidate") or {}
    if "locked-generation-profile" not in (
            candidate.get("validation") or []):
        raise EvaluationProtocolError(
            "candidate freeze lacks locked-generation-profile validation"
        )
    validated_plan = record.get("validated_plan")
    frozen_repair = (
        validated_plan.get("repair_tasks")
        if isinstance(validated_plan, dict) else None
    )
    if (not isinstance(frozen_repair, list) or
            any(not isinstance(task, str) or not task
                for task in frozen_repair)):
        raise EvaluationProtocolError(
            "candidate freeze lacks validated repair tasks"
        )
    source = manifest.get("source")
    source_matrix = (
        source.get("task_matrix") if isinstance(source, dict) else None
    )
    if not isinstance(source_matrix, dict) or not source_matrix:
        raise EvaluationProtocolError(
            "candidate freeze lacks generation evidence task identities"
        )
    manifest_aliases = manifest.get("alias_to_task")
    plan_outputs = profile.get("plan_outputs")
    control_aliases = (
        plan_outputs.get("alias_to_task")
        if isinstance(plan_outputs, dict) else None
    )
    provenance_aliases = provenance.get("alias_to_task")
    if (manifest_aliases != aliases or control_aliases != aliases or
            (provenance_aliases is not None and
             provenance_aliases != aliases)):
        raise EvaluationProtocolError(
            "expected_generation.task_aliases differs from the frozen "
            "generation-profile mapping"
        )
    source_ids = {str(task) for task in source_matrix}
    if set(aliases.values()) != source_ids:
        raise EvaluationProtocolError(
            "frozen alias mapping does not exactly cover source task ids"
        )
    if any(
            not str(task).isdigit() and str(task) not in aliases
            for task in frozen_repair
    ):
        raise EvaluationProtocolError(
            "frozen repair plan contains an unmapped case alias"
        )

    def real_task(task: object) -> str:
        text = str(task)
        return aliases.get(text, text)

    real_repair = [real_task(task) for task in frozen_repair]
    if real_repair != tasks["repair"]:
        raise EvaluationProtocolError(
            "protocol repair tasks differ from the frozen validated plan"
        )
    real_evidence = {real_task(task) for task in source_matrix}
    if set(tasks[guard_phase]) & real_evidence:
        raise EvaluationProtocolError(
            "guard tasks overlap the generation evidence"
        )
    return {
        "run_id": run_id,
        "round": round_number,
        "profile_id": profile_id,
        **expected_hashes,
        "meta_model": meta_model,
        "meta_reasoning_effort": "high",
        "meta_temperature": 0,
        "task_aliases": aliases,
        "repair_tasks": list(tasks["repair"]),
        "guard_tasks": list(tasks[guard_phase]),
    }


def _candidate_freeze_binding(
        binding: object, *, base_dir: Path,
        packages: dict[str, dict[str, str]], required: bool,
        expected_generation: object = None,
        tasks: dict[str, list[str]] | None = None,
        guard_phase: str | None = None,
) -> dict | None:
    if binding is None:
        if required:
            raise EvaluationProtocolError(
                "candidate_freeze is required for short-repair-v1"
            )
        return None
    if not isinstance(binding, dict):
        raise EvaluationProtocolError("candidate_freeze must be an object")
    freeze_path = _resolve_bound_path(
        binding.get("path"), base_dir=base_dir, label="candidate_freeze",
    )
    if freeze_path.name != "candidate_freeze_r1.json":
        raise EvaluationProtocolError(
            "candidate_freeze must bind candidate_freeze_r1.json"
        )
    expected_hash = str(binding.get("sha256") or "").lower()
    if not _valid_sha256(expected_hash):
        raise EvaluationProtocolError("candidate_freeze.sha256 is invalid")
    if not freeze_path.is_file():
        raise EvaluationProtocolError(
            f"candidate freeze record is missing: {freeze_path}"
        )
    if _sha256_file(freeze_path) != expected_hash:
        raise EvaluationProtocolError("candidate freeze record hash mismatch")

    record = _load_json(freeze_path, "candidate freeze record")
    if (record.get("schema_version") != 1 or record.get("round") != 1 or
            record.get("status") !=
            "frozen-before-any-held-out-evaluation"):
        raise EvaluationProtocolError(
            "candidate freeze record has invalid identity/status"
        )
    candidate = record.get("candidate")
    source = record.get("source")
    isolation = record.get("held_out_isolation")
    if not isinstance(candidate, dict) or not isinstance(source, dict):
        raise EvaluationProtocolError(
            "candidate freeze record lacks candidate/source proof"
        )
    frozen_path_raw = candidate.get("frozen_path")
    if not isinstance(frozen_path_raw, str) or not frozen_path_raw:
        raise EvaluationProtocolError(
            "candidate freeze record lacks candidate frozen_path"
        )
    frozen_path = Path(frozen_path_raw)
    if not frozen_path.is_absolute():
        frozen_path = (freeze_path.parent / frozen_path).resolve()
    else:
        frozen_path = frozen_path.resolve()
    candidate_path = Path(packages["candidate"]["path"]).resolve()
    if frozen_path != candidate_path:
        raise EvaluationProtocolError(
            "candidate package path differs from candidate freeze record"
        )
    if candidate.get("tree_sha256") != packages["candidate"]["sha256"]:
        raise EvaluationProtocolError(
            "candidate package hash differs from candidate freeze record"
        )
    if source.get("base_package_tree_sha256") != packages["base"]["sha256"]:
        raise EvaluationProtocolError(
            "base M0 hash differs from candidate freeze source"
        )
    if (not isinstance(isolation, dict) or
            isolation.get("downstream_simulations") != 0 or
            isolation.get("downstream_artifacts") != [] or
            isolation.get("evaluation_started") is not False):
        raise EvaluationProtocolError(
            "candidate was not frozen before held-out evaluation"
        )
    generation_binding = None
    if required:
        if tasks is None or guard_phase is None:
            raise EvaluationProtocolError(
                "short repair freeze validation lacks task context"
            )
        generation_binding = _expected_generation_binding(
            expected_generation, record,
            tasks=tasks, guard_phase=guard_phase,
        )
    return {
        "path": str(freeze_path),
        "sha256": expected_hash,
        "run_id": record.get("run_id"),
        "round": 1,
        "candidate_tree_sha256": packages["candidate"]["sha256"],
        "base_tree_sha256": packages["base"]["sha256"],
        "expected_generation": generation_binding,
    }


def _validate_fingerprint_record(
        record: object, *, task: str, label: str,
) -> dict:
    if (not isinstance(record, dict) or
            record.get("sim_tag") != f"task{task}" or
            not isinstance(record.get("counters"), dict) or
            not isinstance(record.get("events"), list) or
            any(not isinstance(event, dict)
                for event in record.get("events", []))):
        raise EvaluationProtocolError(f"{label} is invalid")
    counters = record["counters"]
    if any(
            not isinstance(key, str) or not key or
            not isinstance(value, int) or isinstance(value, bool) or
            value < 0
            for key, value in counters.items()
    ):
        raise EvaluationProtocolError(f"{label} has invalid counters")
    checker_events = sum(
        event.get("kind") == "checker_bounce"
        for event in record["events"]
    )
    if counters.get("checker_bounce", 0) < checker_events:
        raise EvaluationProtocolError(f"{label} counters/events disagree")
    return record


def _fingerprint_records_by_trial(
        results_path: Path, sidecar_path: Path, *,
        task: str, num_trials: int,
) -> dict[int, dict]:
    document = _load_json(results_path, "results artifact")
    simulations = document.get("simulations")
    if not isinstance(simulations, list):
        raise EvaluationProtocolError(
            "results artifact lacks simulations for trial-bound fingerprints"
        )
    by_trial: dict[int, dict] = {}
    for simulation in simulations:
        if not isinstance(simulation, dict) or str(
                simulation.get("task_id")) != task:
            raise EvaluationProtocolError(
                "results artifact has an unexpected simulation"
            )
        trial = simulation.get("trial")
        if (not isinstance(trial, int) or isinstance(trial, bool) or
                trial not in range(num_trials) or trial in by_trial):
            raise EvaluationProtocolError(
                "results artifact has invalid/duplicate trial identity"
            )
        embedded = []
        for message in simulation.get("messages") or []:
            raw = message.get("raw_data") if isinstance(message, dict) else None
            if isinstance(raw, dict) and EMBEDDED_FINGERPRINT_KEY in raw:
                embedded.append(raw[EMBEDDED_FINGERPRINT_KEY])
        if len(embedded) != 1:
            raise EvaluationProtocolError(
                f"task {task} trial {trial} lacks one embedded fingerprint"
            )
        by_trial[trial] = _validate_fingerprint_record(
            embedded[0], task=task,
            label=f"task {task} trial {trial} embedded fingerprint",
        )
    if set(by_trial) != set(range(num_trials)):
        raise EvaluationProtocolError(
            "embedded fingerprint trial coverage is incomplete"
        )

    sidecar_records = _validate_fingerprint_records(
        sidecar_path, task=task, num_trials=num_trials,
    )
    canonical = lambda value: json.dumps(  # noqa: E731
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    if (Counter(canonical(record) for record in sidecar_records) !=
            Counter(canonical(record) for record in by_trial.values())):
        raise EvaluationProtocolError(
            "embedded fingerprints do not match the bound sidecar"
        )
    return by_trial


def _validate_fingerprint_records(
        path: Path, *, task: str, num_trials: int,
) -> list[dict]:
    try:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:
        raise EvaluationProtocolError(
            f"cannot read fingerprint artifact: {exc}"
        ) from exc
    if len(lines) != num_trials:
        raise EvaluationProtocolError(
            "fingerprint artifact has incomplete per-trial records"
        )
    records: list[dict] = []
    for index, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except Exception as exc:
            raise EvaluationProtocolError(
                f"fingerprint line {index} is invalid JSON: {exc}"
            ) from exc
        records.append(_validate_fingerprint_record(
            record, task=task, label=f"fingerprint line {index}",
        ))
    return records


def _v2_task_blocks() -> list[dict[str, str]]:
    return [
        {
            "block_id": "repair_forward",
            "phase": "repair",
            "task_id": "38",
        },
        {
            "block_id": "repair_reverse",
            "phase": "repair",
            "task_id": "38",
        },
        {
            "block_id": "safety",
            "phase": "guard",
            "task_id": "50",
        },
        {
            "block_id": "fallback",
            "phase": "guard",
            "task_id": "33",
        },
        {
            "block_id": "near_neighbor",
            "phase": "guard",
            "task_id": "36",
        },
        {
            "block_id": "neutral",
            "phase": "guard",
            "task_id": "86",
        },
    ]


def _v2_raw_schedule() -> list[dict[str, str]]:
    orders = (
        ("base", "candidate"),
        ("candidate", "base"),
        ("base", "candidate"),
        ("candidate", "base"),
        ("base", "candidate"),
        ("candidate", "base"),
    )
    schedule: list[dict[str, str]] = []
    for block, order in zip(_v2_task_blocks(), orders):
        schedule.extend({
            **block,
            "arm": arm,
        } for arm in order)
    return schedule


def _v2_schedule() -> list[dict[str, str]]:
    return [
        {
            "id": (
                f"{index:02d}_{shard['block_id']}_{shard['phase']}_"
                f"{shard['task_id']}_{shard['arm']}"
            ),
            **shard,
        }
        for index, shard in enumerate(_v2_raw_schedule())
    ]


def _v2_pair_schedule() -> list[dict[str, str]]:
    schedule: list[dict[str, str]] = []
    for phase in ("guard", "repair"):
        for offset, task in enumerate(V2_FIXED_TASKS[phase]):
            order = (
                ("base", "candidate")
                if offset % 2 == 0 else
                ("candidate", "base")
            )
            schedule.extend({
                "phase": phase,
                "task_id": task,
                "arm": arm,
            } for arm in order)
    return schedule


def _v2_candidate_route(
        freeze_record: dict, *, freeze_path: Path,
        verified_matched_rule_ids: list[str],
) -> str:
    validated_plan = freeze_record.get("validated_plan")
    frozen_matched = (
        validated_plan.get("matched_rule_ids")
        if isinstance(validated_plan, dict) else None
    )
    generation_profile = freeze_record.get("generation_profile")
    plan_outputs = (
        generation_profile.get("plan_outputs")
        if isinstance(generation_profile, dict) else None
    )
    profile_matched = (
        plan_outputs.get("matched_rule_ids")
        if isinstance(plan_outputs, dict) else None
    )
    matched = verified_matched_rule_ids
    if (
            not isinstance(matched, list) or not matched or
            any(not isinstance(rule, str) or not rule for rule in matched) or
            len(matched) != len(set(matched)) or
            matched != sorted(matched) or
            frozen_matched != matched or profile_matched != matched
    ):
        raise EvaluationProtocolError(
            "candidate freeze matched_rule_ids differ from the independently "
            "verified plan outputs"
        )
    rules = frozenset(matched)
    if rules in {
            frozenset({"checker_rule"}),
            frozenset({"checker_rule", "guide_rule"}),
    }:
        route = "C"
    elif rules == frozenset({"knowledge_rule", "retrieval_rule"}):
        route = "R"
    else:
        raise EvaluationProtocolError(
            "candidate matched_rule_ids do not identify Route C or Route R"
        )

    proof = (
        generation_profile.get("activation_query_proof")
        if isinstance(generation_profile, dict) else None
    )
    if not isinstance(proof, dict):
        raise EvaluationProtocolError(
            "candidate freeze lacks activation-query proof"
        )
    raw_path = proof.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise EvaluationProtocolError(
            "candidate activation-query proof path is missing"
        )
    proof_path = Path(raw_path)
    if not proof_path.is_absolute():
        proof_path = (freeze_path.parent / proof_path).resolve()
    else:
        proof_path = proof_path.resolve()
    report = proof.get("report")
    if (
            not proof_path.is_file() or
            _sha256_file(proof_path) != proof.get("sha256") or
            not isinstance(report, dict) or
            _load_json(proof_path, "activation-query report") != report or
            report.get("passed") is not True or
            report.get("offline") is not True or
            report.get("model_calls") != 0 or
            report.get("network_calls") != 0 or
            not isinstance(report.get("probes"), list)
    ):
        raise EvaluationProtocolError(
            "candidate activation-query proof is inadmissible"
        )
    if route == "R" and (
            not report["probes"] or
            any(
                not isinstance(probe, dict) or
                probe.get("activated") is not True or
                probe.get("carrier") != "need_driven_retrieval"
                for probe in report["probes"]
            )
    ):
        raise EvaluationProtocolError(
            "Route R lacks an activated need-driven retrieval probe"
        )
    return route


def _v2_canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _v2_bound_json_artifact(
        binding: object, *, base_dir: Path, label: str,
        expected_name: str,
) -> tuple[dict, dict, Path]:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise EvaluationProtocolError(
            f"{label} must bind exactly path and sha256"
        )
    expected_hash = str(binding.get("sha256") or "").lower()
    if not _valid_sha256(expected_hash):
        raise EvaluationProtocolError(f"{label}.sha256 is invalid")
    path = _resolve_bound_path(
        binding.get("path"), base_dir=base_dir, label=label,
    )
    if path.name != expected_name:
        raise EvaluationProtocolError(
            f"{label} must bind {expected_name}"
        )
    if (
            not path.is_file() or Driver._is_linklike(path) or
            _sha256_file(path) != expected_hash
    ):
        raise EvaluationProtocolError(
            f"{label} is missing, link-like, or has a hash mismatch"
        )
    return {
        "path": str(path),
        "sha256": expected_hash,
    }, _load_json(path, label), path


def _v2_expected_profile_config(
        *, loaded_profile: dict, profile_provenance: dict,
        comparison_lock: dict, arm: dict,
) -> dict:
    spec = loaded_profile["spec"]
    return {
        "source_path": profile_provenance["source_spec_path"],
        "expected_raw_sha256": loaded_profile["raw_sha256"],
        "canonical_sha256": loaded_profile["canonical_sha256"],
        "schema_version": spec["schema_version"],
        "profile_id": spec["profile_id"],
        "attempt_timeouts": copy.deepcopy(spec["attempt_timeouts"]),
        "comparison_lock": copy.deepcopy(comparison_lock),
        "matched_comparison_arm": copy.deepcopy(arm),
    }


def _v2_validate_generation_config(
        config: object, *, arm: dict, protocol: dict,
        loaded_profile: dict, profile_provenance: dict,
        comparison_lock: dict,
) -> dict:
    if not isinstance(config, dict):
        raise EvaluationProtocolError(
            "generation provenance lacks a config object"
        )
    expected_worker = {
        "model": FORMAL_DOWNSTREAM_MODEL,
        "reasoning_effort": FORMAL_DOWNSTREAM_REASONING,
        "temperature": FORMAL_DOWNSTREAM_TEMPERATURE,
        "allowed_openai_params": FORMAL_ALLOWED_OPENAI_PARAMS,
    }
    spec = loaded_profile["spec"]
    expected_replay = {
        "schema_version": 1,
        "run_id": spec["replay"]["source_run_id"],
        "round": spec["replay"]["source_round"],
        "treatment":
            "frozen-train-evidence+fresh-contemporaneous-gates",
    }
    evaluation = config.get("evaluation")
    if (
            config.get("run_id") != arm["run_id"] or
            config.get("domain") != protocol["domain"] or
            config.get("arm") != "autonomous" or
            config.get("benchmark_protocol") != "official" or
            config.get("mode") != "generation-only" or
            config.get("replay_source") != expected_replay or
            config.get("worker") != expected_worker or
            config.get("simulator") != expected_worker or
            config.get("meta") != {
                "model": arm["meta_model"],
                "reasoning_effort": "high",
                "temperature": 0,
            } or
            config.get("generation_profile") !=
            _v2_expected_profile_config(
                loaded_profile=loaded_profile,
                profile_provenance=profile_provenance,
                comparison_lock=comparison_lock,
                arm=arm,
            ) or
            not isinstance(evaluation, dict) or
            evaluation.get("target_round") != 1 or
            evaluation.get("max_concurrency") !=
            protocol["execution"]["max_concurrency"] or
            evaluation.get("max_sims") != 0
    ):
        raise EvaluationProtocolError(
            "generation provenance config differs from the locked arm, "
            "profile, or treatment"
        )
    return config


def _v2_common_generation_config(config: dict) -> dict:
    profile = copy.deepcopy(config.get("generation_profile"))
    if isinstance(profile, dict):
        profile.pop("matched_comparison_arm", None)
    return {
        key: copy.deepcopy(config.get(key))
        for key in (
            "driver_schema_version", "domain", "arm",
            "benchmark_protocol", "mode", "replay_source", "splits",
            "base", "worker", "simulator", "evaluation", "cc_timeout",
        )
    } | {"generation_profile": profile}


def _v2_validate_generation_outcome(
        binding: object, *, base_dir: Path, arm: dict,
        allowed_outcomes: set[str], protocol: dict,
        loaded_profile: dict, profile_provenance: dict,
        comparison_lock: dict, runtime_sources: dict,
        expected_run_dir: Path | None = None,
) -> dict:
    required = {
        "arm_id", "run_id", "meta_model", "outcome",
        "state", "provenance", "finalization",
    }
    if not isinstance(binding, dict) or set(binding) != required:
        raise EvaluationProtocolError(
            "generation outcome binding has invalid fields"
        )
    outcome = str(binding.get("outcome") or "")
    if (
            binding.get("arm_id") != arm["arm_id"] or
            binding.get("run_id") != arm["run_id"] or
            binding.get("meta_model") != arm["meta_model"] or
            outcome not in allowed_outcomes
    ):
        raise EvaluationProtocolError(
            "generation outcome identity/model/outcome differs from the "
            "comparison lock"
        )

    normalized_state, state, state_path = _v2_bound_json_artifact(
        binding["state"], base_dir=base_dir,
        label=f"{arm['arm_id']} generation state",
        expected_name="state.json",
    )
    normalized_provenance, provenance, provenance_path = (
        _v2_bound_json_artifact(
            binding["provenance"], base_dir=base_dir,
            label=f"{arm['arm_id']} generation provenance",
            expected_name="provenance.json",
        )
    )
    normalized_finalization, finalization, finalization_path = (
        _v2_bound_json_artifact(
            binding["finalization"], base_dir=base_dir,
            label=f"{arm['arm_id']} generation finalization",
            expected_name="round_1_finalize.json",
        )
    )
    run_dir = state_path.parent
    if (
            provenance_path.parent != run_dir or
            finalization_path.parent != run_dir or
            run_dir.name != arm["run_id"] or
            (expected_run_dir is not None and
             run_dir.resolve() != expected_run_dir.resolve())
    ):
        raise EvaluationProtocolError(
            "generation outcome artifacts do not share the locked run directory"
        )
    round_outcomes = state.get("round_outcomes")
    if (
            state.get("round_done") != 1 or state.get("sims") != 0 or
            not isinstance(round_outcomes, dict) or
            round_outcomes.get("1") != outcome or
            finalization.get("status") != "complete" or
            finalization.get("round") != 1 or
            not isinstance(finalization.get("payload"), dict) or
            finalization["payload"].get("outcome") != outcome
    ):
        raise EvaluationProtocolError(
            "generation outcome is not an atomically complete zero-simulation "
            "round-1 terminal state"
        )
    if provenance.get("runtime_sources") != runtime_sources:
        raise EvaluationProtocolError(
            "generation outcome runtime differs from the comparison lock"
        )
    if provenance.get("downstream_artifacts") != []:
        raise EvaluationProtocolError(
            "generation outcome contains downstream artifacts"
        )
    config = _v2_validate_generation_config(
        provenance.get("config"), arm=arm, protocol=protocol,
        loaded_profile=loaded_profile,
        profile_provenance=profile_provenance,
        comparison_lock=comparison_lock,
    )
    for session in provenance.get("meta_sessions") or []:
        if not isinstance(session, dict):
            raise EvaluationProtocolError(
                "generation provenance contains an invalid Meta session"
            )
        if re.fullmatch(r"r1_[dp]\d+", str(session.get("name") or "")):
            errors = Driver._profile_session_binding_errors(
                session, model=arm["meta_model"],
                reasoning="high", temperature=0,
            )
            if errors:
                raise EvaluationProtocolError(
                    "generation provenance Meta session lacks an exact "
                    "per-session response-count binding"
                )
    return {
        "arm_id": arm["arm_id"],
        "run_id": arm["run_id"],
        "meta_model": arm["meta_model"],
        "outcome": outcome,
        "state": normalized_state,
        "provenance": normalized_provenance,
        "finalization": normalized_finalization,
        "run_dir": str(run_dir),
        "config": config,
        "provenance_record": provenance,
    }


def _v2_validate_candidate_sessions(
        freeze_record: dict, candidate_provenance: dict, *,
        meta_model: str,
) -> None:
    frozen_sessions = (freeze_record.get("meta_agent") or {}).get("sessions")
    source_sessions = candidate_provenance.get("meta_sessions")
    if (
            not isinstance(frozen_sessions, list) or
            not isinstance(source_sessions, list) or
            len(frozen_sessions) != len(source_sessions)
    ):
        raise EvaluationProtocolError(
            "candidate freeze sessions differ from bound provenance"
        )
    for frozen, source in zip(frozen_sessions, source_sessions):
        if (
                not isinstance(frozen, dict) or
                not isinstance(source, dict) or
                frozen != {
                    key: copy.deepcopy(source.get(key))
                    for key in frozen
                }
        ):
            raise EvaluationProtocolError(
                "candidate freeze session proof differs from provenance"
            )
        if re.fullmatch(r"r1_[dp]\d+", str(frozen.get("name") or "")):
            errors = Driver._profile_session_binding_errors(
                frozen, model=meta_model,
                reasoning="high", temperature=0,
            )
            if errors:
                raise EvaluationProtocolError(
                    "candidate freeze session lacks an exact per-session "
                    "response-count binding"
                )


def _v2_revalidate_candidate(
        protocol: dict, freeze_record: dict, *, freeze_path: Path,
) -> dict:
    profile = freeze_record.get("generation_profile")
    provenance = (
        profile.get("provenance") if isinstance(profile, dict) else None
    )
    frozen_manifest = (
        profile.get("input_manifest") if isinstance(profile, dict) else None
    )
    if (
            not isinstance(profile, dict) or
            not isinstance(provenance, dict) or
            not isinstance(frozen_manifest, dict)
    ):
        raise EvaluationProtocolError(
            "candidate freeze lacks a materialized generation profile"
        )
    materialized_spec_path = _resolve_bound_path(
        provenance.get("materialized_spec_path"),
        base_dir=freeze_path.parent,
        label="materialized generation profile",
    )
    materialized_sha = str(
        provenance.get("materialized_spec_sha256") or ""
    ).lower()
    try:
        loaded = Driver._load_generation_profile_spec(
            materialized_spec_path, materialized_sha,
        )
    except Exception as exc:
        raise EvaluationProtocolError(
            f"materialized generation profile failed validation: {exc}"
        ) from exc
    spec = loaded["spec"]
    expected_generation = protocol["expected_generation"]
    if (
            loaded["raw_sha256"] !=
            expected_generation["raw_spec_sha256"] or
            provenance.get("expected_raw_spec_sha256") !=
            loaded["raw_sha256"]
    ):
        raise EvaluationProtocolError(
            "materialized generation profile differs from the protocol"
        )

    verifier = object.__new__(Driver)
    verifier.run_dir = freeze_path.parent.resolve()
    verifier.cand_dir = Path(
        protocol["packages"]["candidate"]["path"]
    ).resolve()
    profile_paths = verifier._generation_profile_paths(
        1, verifier.run_dir / "round_1",
    )
    manifest_path = profile_paths["manifest"]
    if (
            materialized_spec_path.resolve() !=
            profile_paths["spec"].resolve() or
            _resolve_bound_path(
                provenance.get("manifest_path"),
                base_dir=freeze_path.parent,
                label="generation profile manifest",
            ).resolve() != manifest_path.resolve() or
            not manifest_path.is_file() or
            Driver._is_linklike(manifest_path) or
            _sha256_file(manifest_path) !=
            provenance.get("manifest_sha256")
    ):
        raise EvaluationProtocolError(
            "generation profile materialized paths or manifest hash changed"
        )
    disk_manifest = _load_json(
        manifest_path, "materialized generation profile manifest",
    )
    if disk_manifest != frozen_manifest:
        raise EvaluationProtocolError(
            "candidate freeze manifest differs from the materialized manifest"
        )
    copied_fields = (
        "profile_id", "domain", "replay", "source", "tool_allowlist",
        "attempt_timeouts", "plan_scope", "alias_to_task",
        "plan_contract", "activation_queries", "information_boundary",
        "comparison_lock",
    )
    if (
            disk_manifest.get("schema_version") != spec["schema_version"] or
            disk_manifest.get("raw_spec_sha256") != loaded["raw_sha256"] or
            disk_manifest.get("canonical_spec_sha256") !=
            loaded["canonical_sha256"] or
            any(disk_manifest.get(field) != spec.get(field)
                for field in copied_fields) or
            _v2_canonical_json_sha256(
                disk_manifest.get("substantive")
            ) != disk_manifest.get("substantive_input_sha256")
    ):
        raise EvaluationProtocolError(
            "materialized manifest differs from the reloaded profile spec"
        )

    comparison_lock = spec.get("comparison_lock")
    manifest_lock = disk_manifest.get("comparison_lock")
    if (
            not isinstance(comparison_lock, dict) or
            profile.get("comparison_lock") != comparison_lock or
            manifest_lock != comparison_lock
    ):
        raise EvaluationProtocolError(
            "candidate profile/manifest lacks one exact comparison_lock"
        )
    arms = comparison_lock["arms"]
    if {
            arm["meta_model"] for arm in arms
    } != V2_ALLOWED_META_MODELS:
        raise EvaluationProtocolError(
            "comparison_lock does not contain exactly the two allowed backends"
        )
    current_arms = [
        arm for arm in arms
        if arm["run_id"] == expected_generation["run_id"] and
        arm["meta_model"] == expected_generation["meta_model"]
    ]
    if len(current_arms) != 1:
        raise EvaluationProtocolError(
            "candidate generation run/model does not uniquely match "
            "comparison_lock"
        )
    current_arm = current_arms[0]
    other_arm = next(arm for arm in arms if arm != current_arm)
    if (
            profile.get("matched_comparison_arm") != current_arm or
            disk_manifest.get("matched_comparison_arm") != current_arm or
            comparison_lock.get("meta_treatment") != {
                "reasoning_effort": "high",
                "temperature": 0,
            } or
            comparison_lock.get("downstream_treatment") != {
                "worker_model": FORMAL_DOWNSTREAM_MODEL,
                "worker_reasoning_effort": FORMAL_DOWNSTREAM_REASONING,
                "worker_temperature": FORMAL_DOWNSTREAM_TEMPERATURE,
                "simulator_model": FORMAL_DOWNSTREAM_MODEL,
                "simulator_reasoning_effort": FORMAL_DOWNSTREAM_REASONING,
                "simulator_temperature": FORMAL_DOWNSTREAM_TEMPERATURE,
            } or
            comparison_lock.get("max_concurrency") !=
            protocol["execution"]["max_concurrency"] or
            comparison_lock.get("evaluation") != {
                "protocol_id": V2_GATE_MODE,
                "evaluator": V2_COMPARISON_EVALUATOR,
            }
    ):
        raise EvaluationProtocolError(
            "comparison_lock differs from the V2 protocol treatment"
        )

    treatment = {
        "raw_spec_sha256": loaded["raw_sha256"],
        "canonical_spec_sha256": loaded["canonical_sha256"],
        "substantive_input_sha256":
            disk_manifest["substantive_input_sha256"],
        "source": copy.deepcopy(spec["source"]),
        "replay": copy.deepcopy(spec["replay"]),
        "tool_allowlist": copy.deepcopy(spec["tool_allowlist"]),
        "attempt_timeouts": copy.deepcopy(spec["attempt_timeouts"]),
        "plan_scope": copy.deepcopy(spec["plan_scope"]),
        "alias_to_task": copy.deepcopy(spec["alias_to_task"]),
        "plan_contract": copy.deepcopy(spec["plan_contract"]),
        "activation_queries": copy.deepcopy(spec["activation_queries"]),
        "information_boundary":
            copy.deepcopy(spec["information_boundary"]),
        "comparison_lock": copy.deepcopy(comparison_lock),
        "matched_comparison_arm": copy.deepcopy(current_arm),
    }
    if (
            _v2_canonical_json_sha256(treatment) !=
            disk_manifest.get("treatment_sha256") or
            disk_manifest.get("substantive_input_sha256") !=
            expected_generation["substantive_input_sha256"] or
            disk_manifest.get("treatment_sha256") !=
            expected_generation["treatment_sha256"]
    ):
        raise EvaluationProtocolError(
            "materialized profile treatment hash is not reproducible"
        )

    runtime_sources = Driver._runtime_sources_record()
    runtime_sha = runtime_sources.get("tree_sha256")
    if (
            runtime_sha != comparison_lock.get("runtime_tree_sha256") or
            runtime_sha !=
            (freeze_record.get("generation_runtime") or {}).get(
                "tree_sha256"
            )
    ):
        raise EvaluationProtocolError(
            "current runtime, candidate freeze, and comparison_lock differ"
        )

    scope = spec["plan_scope"]
    generation_context = {
        "spec": spec,
        "paths": profile_paths,
        "manifest": disk_manifest,
        "plan_outputs": profile.get("plan_outputs"),
        "alias_to_task": copy.deepcopy(spec["alias_to_task"]),
        "failing_aliases": set(scope["failing_tasks"]),
        "required_alias_coverage":
            set(scope["required_task_coverage"]),
    }
    try:
        verified_plan_outputs = (
            verifier._verify_generation_profile_plan_outputs(
                generation_context
            )
        )
    except Exception as exc:
        raise EvaluationProtocolError(
            f"candidate plan artifacts failed independent verification: {exc}"
        ) from exc
    mapped_plan_path = profile_paths["mapped_plan"]
    mapped_plan = _load_json(
        mapped_plan_path, "independently verified mapped plan",
    )
    validated_plan = freeze_record.get("validated_plan")
    if (
            not isinstance(validated_plan, dict) or
            validated_plan.get("path") != str(mapped_plan_path) or
            validated_plan.get("file_sha256") !=
            _sha256_file(mapped_plan_path) or
            validated_plan.get("canonical_sha256") !=
            _v2_canonical_json_sha256(mapped_plan) or
            validated_plan.get("matched_rule_ids") !=
            verified_plan_outputs["matched_rule_ids"]
    ):
        raise EvaluationProtocolError(
            "candidate validated-plan record differs from verified artifacts"
        )
    try:
        conformance_errors = verifier._plan_conformance_errors(
            mapped_plan, Path(protocol["packages"]["base"]["path"]),
        )
    except Exception as exc:
        raise EvaluationProtocolError(
            f"candidate plan-conformance check failed: {exc}"
        ) from exc
    if conformance_errors:
        raise EvaluationProtocolError(
            "candidate package differs from its independently verified plan: "
            + "; ".join(conformance_errors)
        )
    route = _v2_candidate_route(
        freeze_record, freeze_path=freeze_path,
        verified_matched_rule_ids=copy.deepcopy(
            verified_plan_outputs["matched_rule_ids"]
        ),
    )
    source_spec_path = provenance.get("source_spec_path")
    if not isinstance(source_spec_path, str) or not source_spec_path:
        raise EvaluationProtocolError(
            "candidate profile provenance lacks source_spec_path"
        )
    return {
        "route": route,
        "loaded_profile": loaded,
        "profile_provenance": provenance,
        "comparison_lock": comparison_lock,
        "current_arm": current_arm,
        "other_arm": other_arm,
        "runtime_sources": runtime_sources,
        "verified_plan_outputs": verified_plan_outputs,
    }


def _validate_v2_protocol(
        protocol: dict, protocol_path: Path,
) -> dict:
    if protocol.get("tasks") != V2_FIXED_TASKS:
        raise EvaluationProtocolError(
            "short-repair-v2 tasks differ from the frozen V8 task set/order"
        )
    if protocol.get("gate") != V2_FIXED_GATE:
        raise EvaluationProtocolError(
            "short-repair-v2 gate differs from the frozen thresholds"
        )
    execution = protocol.get("execution")
    if not isinstance(execution, dict):
        raise EvaluationProtocolError("execution must be an object")
    if execution.get("phase_trials") != V2_FIXED_TRIALS:
        raise EvaluationProtocolError(
            "short-repair-v2 task seeds/trials differ from the frozen design"
        )
    if execution.get("max_concurrency") != 6:
        raise EvaluationProtocolError(
            "short-repair-v2 max_concurrency must be 6"
        )
    if protocol.get("schedule") != _v2_raw_schedule():
        raise EvaluationProtocolError(
            "short-repair-v2 schedule differs from the frozen balanced blocks"
        )

    common_raw = copy.deepcopy(protocol)
    common_raw["schedule"] = _v2_pair_schedule()
    common_raw["gate"] = {
        "mode": "short-repair-v1",
        "repair_min_net_fixes": 1,
        "guard_new_failure_cap": 1,
        "require_overall_net_positive": True,
        "required_mechanism": {
            "phase": "repair",
            "event_kind": "checker_bounce",
            "checker": "anti_escalation",
            "min_events": 1,
        },
    }
    common = _validate_protocol(common_raw, protocol_path)
    expected_generation = common.get("expected_generation") or {}
    if expected_generation.get("meta_model") not in V2_ALLOWED_META_MODELS:
        raise EvaluationProtocolError(
            "short-repair-v2 Meta model is not an allowed backend"
        )
    freeze_path = Path(common["candidate_freeze"]["path"])
    freeze_record = _load_json(
        freeze_path, "candidate freeze record",
    )
    revalidated = _v2_revalidate_candidate(
        common, freeze_record, freeze_path=freeze_path,
    )
    candidate_outcome = _v2_validate_generation_outcome(
        protocol.get("candidate_generation_outcome"),
        base_dir=protocol_path.parent,
        arm=revalidated["current_arm"],
        allowed_outcomes={"candidate-generated"},
        protocol=common,
        loaded_profile=revalidated["loaded_profile"],
        profile_provenance=revalidated["profile_provenance"],
        comparison_lock=revalidated["comparison_lock"],
        runtime_sources=revalidated["runtime_sources"],
        expected_run_dir=freeze_path.parent,
    )
    other_outcome = _v2_validate_generation_outcome(
        protocol.get("other_generation_outcome"),
        base_dir=protocol_path.parent,
        arm=revalidated["other_arm"],
        allowed_outcomes=V2_OTHER_GENERATION_OUTCOMES,
        protocol=common,
        loaded_profile=revalidated["loaded_profile"],
        profile_provenance=revalidated["profile_provenance"],
        comparison_lock=revalidated["comparison_lock"],
        runtime_sources=revalidated["runtime_sources"],
    )
    if (
            _v2_common_generation_config(candidate_outcome["config"]) !=
            _v2_common_generation_config(other_outcome["config"])
    ):
        raise EvaluationProtocolError(
            "candidate and other generation arms differ outside their "
            "locked run/model/arm mapping"
        )
    _v2_validate_candidate_sessions(
        freeze_record, candidate_outcome["provenance_record"],
        meta_model=revalidated["current_arm"]["meta_model"],
    )
    other_run_dir = Path(other_outcome["run_dir"])
    if (other_run_dir / "candidate_freeze_r1.json").exists():
        raise EvaluationProtocolError(
            "the other comparison arm also produced a candidate; use the "
            "tri-arm evaluator"
        )
    if freeze_path.parent.resolve() == other_run_dir.resolve():
        raise EvaluationProtocolError(
            "candidate and other generation outcomes bind the same run"
        )
    route = revalidated["route"]
    normalized_candidate_outcome = {
        key: copy.deepcopy(candidate_outcome[key])
        for key in (
            "arm_id", "run_id", "meta_model", "outcome",
            "state", "provenance", "finalization", "run_dir",
        )
    }
    normalized_other_outcome = {
        key: copy.deepcopy(other_outcome[key])
        for key in (
            "arm_id", "run_id", "meta_model", "outcome",
            "state", "provenance", "finalization", "run_dir",
        )
    }
    return {
        **common,
        "gate": copy.deepcopy(V2_FIXED_GATE),
        "tasks": copy.deepcopy(V2_FIXED_TASKS),
        "schedule": _v2_schedule(),
        "candidate_route": route,
        "candidate_freeze_record": freeze_record,
        "candidate_generation_outcome": normalized_candidate_outcome,
        "other_generation_outcome": normalized_other_outcome,
        "comparison_lock": copy.deepcopy(
            revalidated["comparison_lock"]
        ),
        "v2_pair_protocol": common,
    }


def _validate_protocol(protocol: dict, protocol_path: Path) -> dict:
    if protocol.get("schema_version") != SCHEMA_VERSION:
        raise EvaluationProtocolError(
            f"schema_version must be {SCHEMA_VERSION!r}"
        )
    run_id = _safe_label(protocol.get("run_id"), "run_id")
    if protocol.get("domain") != "retail":
        raise EvaluationProtocolError("this formal protocol currently requires retail")
    if (protocol.get("gate") or {}).get("mode") == V2_GATE_MODE:
        return _validate_v2_protocol(protocol, protocol_path)

    benchmark_binding = protocol.get("benchmark_binding")
    if not isinstance(benchmark_binding, dict):
        raise EvaluationProtocolError("benchmark_binding must be an object")
    expected_commit = str(benchmark_binding.get("git_commit") or "").lower()
    expected_input = str(
        benchmark_binding.get("input_sha256") or ""
    ).lower()
    if (len(expected_commit) != 40 or
            any(char not in "0123456789abcdef" for char in expected_commit)):
        raise EvaluationProtocolError("benchmark_binding.git_commit is invalid")
    if (len(expected_input) != 64 or
            any(char not in "0123456789abcdef" for char in expected_input)):
        raise EvaluationProtocolError("benchmark_binding.input_sha256 is invalid")

    runtime = protocol.get("runtime")
    if not isinstance(runtime, dict) or not runtime:
        raise EvaluationProtocolError("runtime must bind the evaluated code files")
    normalized_runtime: dict[str, dict[str, str]] = {}
    for name, record in runtime.items():
        _safe_label(name, f"runtime key {name!r}")
        if not isinstance(record, dict):
            raise EvaluationProtocolError(f"runtime.{name} must be an object")
        raw_path = record.get("path")
        expected_hash = str(record.get("sha256") or "").lower()
        if not isinstance(raw_path, str) or not raw_path:
            raise EvaluationProtocolError(f"runtime.{name}.path is missing")
        runtime_path = Path(raw_path)
        if not runtime_path.is_absolute():
            runtime_path = (protocol_path.parent / runtime_path).resolve()
        else:
            runtime_path = runtime_path.resolve()
        if not runtime_path.is_file():
            raise EvaluationProtocolError(
                f"runtime.{name}.path is not a file: {runtime_path}"
            )
        if _sha256_file(runtime_path) != expected_hash:
            raise EvaluationProtocolError(f"runtime.{name} hash mismatch")
        normalized_runtime[str(name)] = {
            "path": str(runtime_path),
            "sha256": expected_hash,
        }

    packages = protocol.get("packages")
    if not isinstance(packages, dict) or set(packages) != set(ARMS):
        raise EvaluationProtocolError("packages must define exactly base and candidate")
    normalized_packages: dict[str, dict[str, str]] = {}
    for arm in ARMS:
        record = packages.get(arm)
        if not isinstance(record, dict):
            raise EvaluationProtocolError(f"packages.{arm} must be an object")
        raw_path = record.get("path")
        expected_hash = str(record.get("sha256") or "").lower()
        if not isinstance(raw_path, str) or not raw_path:
            raise EvaluationProtocolError(f"packages.{arm}.path is missing")
        package_path = Path(raw_path)
        if not package_path.is_absolute():
            package_path = (protocol_path.parent / package_path).resolve()
        else:
            package_path = package_path.resolve()
        if not package_path.is_dir():
            raise EvaluationProtocolError(
                f"packages.{arm}.path is not a directory: {package_path}"
            )
        if len(expected_hash) != 64 or any(
                char not in "0123456789abcdef" for char in expected_hash):
            raise EvaluationProtocolError(
                f"packages.{arm}.sha256 is not a SHA-256"
            )
        actual_hash = _package_tree_sha256(package_path)
        if actual_hash != expected_hash:
            raise EvaluationProtocolError(
                f"packages.{arm} hash mismatch: {actual_hash} != {expected_hash}"
            )
        normalized_packages[arm] = {
            "path": str(package_path),
            "sha256": expected_hash,
        }

    gate = protocol.get("gate")
    if not isinstance(gate, dict):
        raise EvaluationProtocolError("gate must be an object")
    gate_mode = gate.get("mode")
    if gate_mode not in {"relaxed", "short-repair-v1"}:
        raise EvaluationProtocolError("gate.mode is unsupported")

    tasks = protocol.get("tasks")
    if not isinstance(tasks, dict):
        raise EvaluationProtocolError("tasks must be an object")
    repair_tasks = _task_list(tasks.get("repair"), "tasks.repair")
    if gate_mode == "relaxed":
        dev_tasks = _task_list(tasks.get("dev"), "tasks.dev")
        normalized_tasks = {"dev": dev_tasks, "repair": repair_tasks}
        guard_phase = "dev"
    else:
        guard_keys = [name for name in ("guard", "dev") if name in tasks]
        if len(guard_keys) != 1 or set(tasks) != {"repair", *guard_keys}:
            raise EvaluationProtocolError(
                "short-repair-v1 tasks must define repair and exactly one "
                "guard/dev phase"
            )
        guard_phase = guard_keys[0]
        guard_tasks = _task_list(
            tasks.get(guard_phase), f"tasks.{guard_phase}",
        )
        normalized_tasks = {
            guard_phase: guard_tasks,
            "repair": repair_tasks,
        }
        dev_tasks = guard_tasks
    if set(dev_tasks) & set(repair_tasks):
        raise EvaluationProtocolError(
            f"{guard_phase} and repair tasks must be disjoint"
        )

    execution = protocol.get("execution")
    if not isinstance(execution, dict):
        raise EvaluationProtocolError("execution must be an object")
    exact_ints = {
        "max_concurrency": (1, None),
        "eval_timeout_seconds": (1, None),
        "resume_attempts": (0, 3),
        "simulation_retries": (0, None),
    }
    for key, (minimum, maximum) in exact_ints.items():
        value = execution.get(key)
        if (not isinstance(value, int) or isinstance(value, bool) or
                value < minimum or
                (maximum is not None and value > maximum)):
            raise EvaluationProtocolError(f"execution.{key} is invalid")
    simulation_timeout = execution.get("simulation_timeout_seconds")
    if (not isinstance(simulation_timeout, (int, float)) or
            isinstance(simulation_timeout, bool) or simulation_timeout <= 0):
        raise EvaluationProtocolError(
            "execution.simulation_timeout_seconds must be positive"
        )
    retry_delay = execution.get("simulation_retry_delay_seconds")
    if (not isinstance(retry_delay, (int, float)) or
            isinstance(retry_delay, bool) or retry_delay < 0):
        raise EvaluationProtocolError(
            "execution.simulation_retry_delay_seconds must be non-negative"
        )
    normalized_execution = dict(execution)
    if gate_mode == "relaxed":
        num_trials, expected_seeds = _phase_trial_settings(
            {"execution": execution}, "dev",
        )
        normalized_execution["num_trials"] = num_trials
        normalized_execution["expected_trial_seeds"] = expected_seeds
    else:
        phase_trials = execution.get("phase_trials")
        if phase_trials is not None and (
                not isinstance(phase_trials, dict) or
                set(phase_trials) != set(normalized_tasks)):
            raise EvaluationProtocolError(
                "execution.phase_trials must exactly cover repair and guard/dev"
            )
        normalized_phase_trials = {}
        for phase in normalized_tasks:
            num_trials, expected_seeds = _phase_trial_settings(
                {"execution": execution}, phase,
            )
            normalized_phase_trials[phase] = {
                "num_trials": num_trials,
                "expected_trial_seeds": expected_seeds,
            }
        normalized_execution["phase_trials"] = normalized_phase_trials
        has_global_trials = "num_trials" in execution
        has_global_seeds = "expected_trial_seeds" in execution
        if has_global_trials != has_global_seeds:
            raise EvaluationProtocolError(
                "global num_trials and expected_trial_seeds must appear together"
            )
        if has_global_trials:
            _phase_trial_settings({"execution": {
                "num_trials": execution["num_trials"],
                "expected_trial_seeds": execution["expected_trial_seeds"],
            }}, guard_phase)
        if execution["resume_attempts"] < 1:
            raise EvaluationProtocolError(
                "short-repair-v1 requires embedded fingerprint evidence"
            )
    for phase in normalized_tasks:
        num_trials, expected_seeds = _phase_trial_settings(
            {"execution": normalized_execution}, phase,
        )
        frozen_seeds = _tau2_trial_seeds(num_trials)
        if expected_seeds != frozen_seeds:
            raise EvaluationProtocolError(
                f"execution seeds for {phase} differ from frozen Tau2 "
                f"base seed {TAU2_BASE_SEED}: expected {frozen_seeds}"
            )

    treatment = protocol.get("downstream_treatment")
    expected_treatment = {
        "worker_model": FORMAL_DOWNSTREAM_MODEL,
        "worker_reasoning_effort": FORMAL_DOWNSTREAM_REASONING,
        "simulator_model": FORMAL_DOWNSTREAM_MODEL,
        "simulator_reasoning_effort": FORMAL_DOWNSTREAM_REASONING,
        "temperature": FORMAL_DOWNSTREAM_TEMPERATURE,
    }
    if treatment != expected_treatment:
        raise EvaluationProtocolError(
            "downstream_treatment differs from the frozen formal treatment"
        )

    schedule = protocol.get("schedule")
    if not isinstance(schedule, list):
        raise EvaluationProtocolError("schedule must be a list")
    normalized_schedule: list[dict[str, str]] = []
    for index, shard in enumerate(schedule):
        if not isinstance(shard, dict):
            raise EvaluationProtocolError(f"schedule[{index}] is not an object")
        phase = str(shard.get("phase"))
        task_id = str(shard.get("task_id"))
        arm = str(shard.get("arm"))
        if phase not in normalized_tasks or arm not in ARMS:
            raise EvaluationProtocolError(f"schedule[{index}] has invalid phase/arm")
        allowed = normalized_tasks[phase]
        if task_id not in allowed:
            raise EvaluationProtocolError(
                f"schedule[{index}] task {task_id} is outside {phase}"
            )
        normalized_schedule.append({
            "id": f"{index:02d}_{phase}_{task_id}_{arm}",
            "phase": phase,
            "task_id": task_id,
            "arm": arm,
        })
    expected_keys = {
        (phase, task, arm)
        for phase, task_list in normalized_tasks.items()
        for task in task_list for arm in ARMS
    }
    observed_keys = {
        (shard["phase"], shard["task_id"], shard["arm"])
        for shard in normalized_schedule
    }
    if len(normalized_schedule) != len(observed_keys) or observed_keys != expected_keys:
        raise EvaluationProtocolError(
            "schedule must cover each phase/task/arm exactly once"
        )
    canonical_schedule: list[tuple[str, str, str]] = []
    for phase, task_list in normalized_tasks.items():
        for offset, task in enumerate(task_list):
            expected_pair = (
                ["base", "candidate"] if offset % 2 == 0
                else ["candidate", "base"]
            )
            canonical_schedule.extend(
                (phase, task, arm) for arm in expected_pair
            )
    observed_schedule = [
        (shard["phase"], shard["task_id"], shard["arm"])
        for shard in normalized_schedule
    ]
    if observed_schedule != canonical_schedule:
        raise EvaluationProtocolError(
            "schedule must use physically adjacent canonical A/B pairs "
            "in frozen phase/task order"
        )

    if gate_mode == "relaxed":
        if (
                not isinstance(gate.get("regression_task_cap"), int) or
                isinstance(gate.get("regression_task_cap"), bool) or
                gate["regression_task_cap"] < 0 or
                gate.get("require_dev_net_positive") is not True or
                gate.get("require_repair_improvement") is not True):
            raise EvaluationProtocolError(
                "gate does not match the frozen relaxed rule"
            )
    else:
        mechanism = gate.get("required_mechanism")
        if (
                not isinstance(gate.get("repair_min_net_fixes"), int) or
                isinstance(gate.get("repair_min_net_fixes"), bool) or
                gate["repair_min_net_fixes"] < 1 or
                not isinstance(gate.get("guard_new_failure_cap"), int) or
                isinstance(gate.get("guard_new_failure_cap"), bool) or
                gate["guard_new_failure_cap"] < 0 or
                gate.get("require_overall_net_positive") is not True or
                not isinstance(mechanism, dict) or
                mechanism.get("phase") != "repair" or
                mechanism.get("event_kind") != "checker_bounce" or
                mechanism.get("checker") != "anti_escalation" or
                not isinstance(mechanism.get("min_events"), int) or
                isinstance(mechanism.get("min_events"), bool) or
                mechanism["min_events"] < 1):
            raise EvaluationProtocolError(
                "gate does not match short-repair-v1"
            )

    candidate_freeze = _candidate_freeze_binding(
        protocol.get("candidate_freeze"),
        base_dir=protocol_path.parent,
        packages=normalized_packages,
        required=gate_mode == "short-repair-v1",
        expected_generation=protocol.get("expected_generation"),
        tasks=normalized_tasks,
        guard_phase=guard_phase,
    )

    run_dir_raw = protocol.get("run_dir")
    if not isinstance(run_dir_raw, str) or not run_dir_raw:
        raise EvaluationProtocolError("run_dir is missing")
    run_dir = Path(run_dir_raw)
    if not run_dir.is_absolute():
        run_dir = (protocol_path.parent / run_dir).resolve()
    else:
        run_dir = run_dir.resolve()
    if run_dir.name != run_id:
        raise EvaluationProtocolError("run_dir leaf must equal run_id")

    return {
        **protocol,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "runtime": normalized_runtime,
        "packages": normalized_packages,
        "tasks": normalized_tasks,
        "execution": normalized_execution,
        "schedule": normalized_schedule,
        "guard_phase": guard_phase,
        "candidate_freeze": candidate_freeze,
        "expected_generation": protocol.get("expected_generation"),
    }


def _load_shard(path: Path, expected_sha256: str | None = None) -> dict:
    if not path.is_file():
        raise EvaluationProtocolError(f"completed shard is missing: {path}")
    actual_hash = _sha256_file(path)
    if expected_sha256 is not None and actual_hash != expected_sha256:
        raise EvaluationProtocolError(f"completed shard hash changed: {path}")
    payload = _load_json(path, f"shard {path.name}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise EvaluationProtocolError(f"shard {path.name} has wrong schema")
    return payload


def _v2_paired_cells(
        tasks: list[str],
        base_rows: dict[str, list[int]],
        candidate_rows: dict[str, list[int]],
) -> dict:
    if set(base_rows) != set(tasks) or set(candidate_rows) != set(tasks):
        raise EvaluationProtocolError(
            "V2 paired rows do not exactly cover the requested tasks"
        )
    repaired = 0
    new_failures = 0
    both_pass = 0
    both_fail = 0
    per_task: dict[str, dict[str, int]] = {}
    for task in tasks:
        base = base_rows[task]
        candidate = candidate_rows[task]
        if (
                not isinstance(base, list) or
                not isinstance(candidate, list) or
                len(base) != len(candidate) or not base or
                any(value not in (0, 1) for value in base + candidate)
        ):
            raise EvaluationProtocolError(
                f"V2 task {task} has invalid paired rows"
            )
        counts = {
            "base_fail_to_candidate_pass": 0,
            "base_pass_to_candidate_fail": 0,
            "both_pass": 0,
            "both_fail": 0,
        }
        for base_value, candidate_value in zip(base, candidate):
            if base_value == 0 and candidate_value == 1:
                repaired += 1
                counts["base_fail_to_candidate_pass"] += 1
            elif base_value == 1 and candidate_value == 0:
                new_failures += 1
                counts["base_pass_to_candidate_fail"] += 1
            elif base_value == 1:
                both_pass += 1
                counts["both_pass"] += 1
            else:
                both_fail += 1
                counts["both_fail"] += 1
        counts["net"] = (
            counts["base_fail_to_candidate_pass"] -
            counts["base_pass_to_candidate_fail"]
        )
        per_task[task] = counts
    total = repaired + new_failures + both_pass + both_fail
    return {
        "base_fail_to_candidate_pass": repaired,
        "base_pass_to_candidate_fail": new_failures,
        "both_pass": both_pass,
        "both_fail": both_fail,
        "net": repaired - new_failures,
        "total": total,
        "per_task": per_task,
    }


def _v2_route_activity(
        record: dict, route: str,
) -> tuple[bool, dict[str, int]]:
    if route == "C":
        events = record.get("events") or []
        count = sum(
            isinstance(event, dict) and
            event.get("kind") == "checker_bounce" and
            event.get("checker") == "anti_escalation"
            for event in events
        )
        return count >= 1, {
            "checker_bounce_anti_escalation": count,
        }
    if route == "R":
        counters = record.get("counters") or {}
        delivered = counters.get("retrieval_delivered", 0)
        injected = counters.get("retrieval_injected_only", 0)
        if (
                not isinstance(delivered, int) or
                isinstance(delivered, bool) or delivered < 0 or
                not isinstance(injected, int) or
                isinstance(injected, bool) or injected < 0
        ):
            raise EvaluationProtocolError(
                "Route R fingerprint counters are invalid"
            )
        return delivered >= 1 and injected >= 1, {
            "retrieval_delivered": delivered,
            "retrieval_injected_only": injected,
        }
    raise EvaluationProtocolError(f"unsupported V2 route {route!r}")


def _v2_causal_mechanism(
        *, route: str, base_row: list[int],
        candidate_row: list[int],
        base_records: dict[int, dict],
        candidate_records: dict[int, dict],
) -> dict:
    expected_trials = set(range(len(base_row)))
    if (
            len(base_row) != len(candidate_row) or
            set(base_records) != expected_trials or
            set(candidate_records) != expected_trials
    ):
        raise EvaluationProtocolError(
            "V2 mechanism proof lacks exact trial coverage"
        )
    candidate_activity: Counter = Counter()
    base_activity: Counter = Counter()
    causal_cells: list[int] = []
    for trial, (base_value, candidate_value) in enumerate(
            zip(base_row, candidate_row)):
        candidate_active, candidate_counts = _v2_route_activity(
            candidate_records[trial], route,
        )
        _base_active, base_counts = _v2_route_activity(
            base_records[trial], route,
        )
        candidate_activity.update(candidate_counts)
        base_activity.update(base_counts)
        if (
                base_value == 0 and candidate_value == 1 and
                candidate_active and
                all(value == 0 for value in base_counts.values())
        ):
            causal_cells.append(trial)
    minimum = V2_FIXED_GATE[
        "mechanism_min_causal_cells_per_repair_block"
    ]
    return {
        "route": route,
        "candidate_activity": dict(candidate_activity),
        "base_activity": dict(base_activity),
        "causal_trials": causal_cells,
        "causal_cell_count": len(causal_cells),
        "minimum_causal_cells": minimum,
        "triggered": len(causal_cells) >= minimum,
    }


def _v2_pair_shards(
        shard_payloads: list[dict], repair_block: str,
) -> list[dict]:
    return [
        dict(shard)
        for shard in shard_payloads
        if (
            shard.get("phase") == "guard" or
            (
                shard.get("phase") == "repair" and
                shard.get("block_id") == repair_block
            )
        )
    ]


def _validate_v2_shards(
        protocol: dict, shard_payloads: list[dict],
) -> None:
    expected = {
        (
            shard["block_id"], shard["phase"],
            shard["task_id"], shard["arm"],
        ): shard["id"]
        for shard in protocol["schedule"]
    }
    observed: set[tuple[str, str, str, str]] = set()
    result_paths: set[str] = set()
    fingerprint_paths: set[str] = set()
    for shard in shard_payloads:
        key = (
            str(shard.get("block_id")),
            str(shard.get("phase")),
            str(shard.get("task_id")),
            str(shard.get("arm")),
        )
        if (
                key not in expected or key in observed or
                shard.get("id") != expected[key] or
                shard.get("schema_version") != SCHEMA_VERSION or
                shard.get("run_id") != protocol["run_id"]
        ):
            raise EvaluationProtocolError(
                f"V2 shard is duplicate or outside schedule: {key}"
            )
        result = shard.get("result")
        artifact = result.get("artifact") if isinstance(result, dict) else None
        if not isinstance(artifact, dict):
            raise EvaluationProtocolError(
                f"V2 shard {key} lacks an artifact"
            )
        result_path = str(artifact.get("results") or "")
        fingerprint_path = str(artifact.get("fingerprint") or "")
        if (
                not result_path or result_path in result_paths or
                not fingerprint_path or
                fingerprint_path in fingerprint_paths
        ):
            raise EvaluationProtocolError(
                "V2 shards reused a result/fingerprint artifact"
            )
        result_paths.add(result_path)
        fingerprint_paths.add(fingerprint_path)
        observed.add(key)
    if observed != set(expected):
        raise EvaluationProtocolError(
            "V2 shard coverage is incomplete"
        )


def _merge_and_gate_v2(
        protocol: dict, shard_payloads: list[dict],
) -> dict:
    _validate_v2_shards(protocol, shard_payloads)
    pair_reports = {
        block: _merge_and_gate(
            protocol["v2_pair_protocol"],
            _v2_pair_shards(shard_payloads, block),
        )
        for block in V2_REPAIR_BLOCKS
    }
    base_guard = pair_reports["repair_forward"]["guard"]["base"]["matrix"]
    candidate_guard = pair_reports[
        "repair_forward"
    ]["guard"]["candidate"]["matrix"]
    if (
            pair_reports["repair_reverse"]["guard"]["base"]["matrix"] !=
            base_guard or
            pair_reports["repair_reverse"]["guard"][
                "candidate"
            ]["matrix"] != candidate_guard
    ):
        raise EvaluationProtocolError(
            "V2 guard matrix changed across repair block views"
        )

    by_key = {
        (
            shard["block_id"], shard["phase"],
            shard["task_id"], shard["arm"],
        ): shard
        for shard in shard_payloads
    }
    repair_blocks: dict[str, dict] = {}
    base_passed = 0
    candidate_passed = 0
    for block in V2_REPAIR_BLOCKS:
        section = pair_reports[block]["repair"]
        base_rows = section["base"]["matrix"]
        candidate_rows = section["candidate"]["matrix"]
        paired = _v2_paired_cells(
            V2_FIXED_TASKS["repair"], base_rows, candidate_rows,
        )
        task = "38"
        num_trials = V2_FIXED_TRIALS["repair"]["num_trials"]
        records = {}
        for arm in ARMS:
            artifact = by_key[
                (block, "repair", task, arm)
            ]["result"]["artifact"]
            records[arm] = _fingerprint_records_by_trial(
                Path(artifact["results"]),
                Path(artifact["fingerprint"]),
                task=task,
                num_trials=num_trials,
            )
        mechanism = _v2_causal_mechanism(
            route=protocol["candidate_route"],
            base_row=base_rows[task],
            candidate_row=candidate_rows[task],
            base_records=records["base"],
            candidate_records=records["candidate"],
        )
        threshold_met = (
            paired["net"] >=
            V2_FIXED_GATE["repair_block_min_net_fixes"]
        )
        block_accept = threshold_met and mechanism["triggered"]
        repair_blocks[block] = {
            "base": copy.deepcopy(base_rows),
            "candidate": copy.deepcopy(candidate_rows),
            "paired_cells": paired,
            "mechanism": mechanism,
            "repair_threshold_met": threshold_met,
            "mechanism_triggered": mechanism["triggered"],
            "accept": block_accept,
        }
        base_passed += sum(sum(row) for row in base_rows.values())
        candidate_passed += sum(
            sum(row) for row in candidate_rows.values()
        )

    guard_all = _v2_paired_cells(
        V2_FIXED_TASKS["guard"], base_guard, candidate_guard,
    )
    guard_by_role = {
        role: {
            "task_id": task,
            "paired_cells": _v2_paired_cells(
                [task],
                {task: base_guard[task]},
                {task: candidate_guard[task]},
            ),
        }
        for task, role in V2_GUARD_ROLE_BY_TASK.items()
    }
    safety = guard_by_role["safety"]["paired_cells"]
    other_tasks = V2_FIXED_GATE["other_guard_task_ids"]
    other_guard = _v2_paired_cells(
        other_tasks,
        {task: base_guard[task] for task in other_tasks},
        {task: candidate_guard[task] for task in other_tasks},
    )
    safety_cap_met = (
        safety["base_pass_to_candidate_fail"] <=
        V2_FIXED_GATE["safety_new_failure_cap"]
    )
    other_cap_met = (
        other_guard["base_pass_to_candidate_fail"] <=
        V2_FIXED_GATE["other_guard_new_failure_cap"]
    )
    overall = {
        name: (
            sum(
                repair_blocks[block]["paired_cells"][name]
                for block in V2_REPAIR_BLOCKS
            ) + guard_all[name]
        )
        for name in (
            "base_fail_to_candidate_pass",
            "base_pass_to_candidate_fail",
            "both_pass", "both_fail", "total",
        )
    }
    overall["net"] = (
        overall["base_fail_to_candidate_pass"] -
        overall["base_pass_to_candidate_fail"]
    )
    blocks_pass = all(
        repair_blocks[block]["accept"]
        for block in V2_REPAIR_BLOCKS
    )
    overall_positive = overall["net"] > 0
    accept = (
        blocks_pass and safety_cap_met and
        other_cap_met and overall_positive
    )
    guard_base_passed = sum(sum(row) for row in base_guard.values())
    guard_candidate_passed = sum(
        sum(row) for row in candidate_guard.values()
    )
    base_passed += guard_base_passed
    candidate_passed += guard_candidate_passed
    total_per_arm = overall["total"]
    if total_per_arm != 20:
        raise EvaluationProtocolError(
            "V2 logical coverage must be exactly 20 cells per arm"
        )
    selection_score = overall["net"] / total_per_arm
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": protocol["run_id"],
        "status": "complete",
        "valid": True,
        "decision": "accept" if accept else "reject",
        "claim": (
            "candidate passed the frozen route-aware V2 gate"
            if accept else
            "candidate did not pass the frozen route-aware V2 gate"
        ),
        "route": protocol["candidate_route"],
        "base": {
            "passed": base_passed,
            "total": total_per_arm,
            "accuracy": base_passed / total_per_arm,
        },
        "candidate": {
            "passed": candidate_passed,
            "total": total_per_arm,
            "accuracy": candidate_passed / total_per_arm,
        },
        "delta_accuracy": selection_score,
        "repair": {
            "blocks": repair_blocks,
            "cells_per_arm": 12,
        },
        "guard": {
            "base": {
                "passed": guard_base_passed,
                "total": 8,
                "accuracy": guard_base_passed / 8,
                "matrix": copy.deepcopy(base_guard),
            },
            "candidate": {
                "passed": guard_candidate_passed,
                "total": 8,
                "accuracy": guard_candidate_passed / 8,
                "matrix": copy.deepcopy(candidate_guard),
            },
            "delta_accuracy":
                (guard_candidate_passed - guard_base_passed) / 8,
            "all": guard_all,
            "by_role": guard_by_role,
            "safety": {
                "task_id": "50",
                "new_failures":
                    safety["base_pass_to_candidate_fail"],
                "cap": 0,
                "cap_met": safety_cap_met,
                "paired_cells": safety,
            },
            "other_combined": {
                "task_ids": list(other_tasks),
                "new_failures":
                    other_guard["base_pass_to_candidate_fail"],
                "cap": 1,
                "cap_met": other_cap_met,
                "paired_cells": other_guard,
            },
        },
        "paired_cells": {"overall": overall},
        "selection_score": selection_score,
        "selection_eligible": accept,
        "gate": {
            "mode": V2_GATE_MODE,
            "repair_blocks_pass": blocks_pass,
            "safety_cap_met": safety_cap_met,
            "other_guard_cap_met": other_cap_met,
            "overall_net": overall["net"],
            "overall_net_positive": overall_positive,
            "accept": accept,
        },
        "evidence": {
            "shards": len(shard_payloads),
            "cells_per_arm": total_per_arm,
            "repair_cells_per_arm": 12,
            "guard_cells_per_arm": 8,
            "trial_seeds_by_block": {
                block: list(
                    V2_FIXED_TRIALS["repair"][
                        "expected_trial_seeds"
                    ]
                )
                for block in V2_REPAIR_BLOCKS
            } | {
                role: list(
                    V2_FIXED_TRIALS["guard"][
                        "expected_trial_seeds"
                    ]
                )
                for role in V2_GUARD_ROLE_BY_TASK.values()
            },
            "meta_model":
                protocol["expected_generation"]["meta_model"],
            "candidate_route": protocol["candidate_route"],
            "candidate_freeze":
                copy.deepcopy(protocol["candidate_freeze"]),
            "candidate_generation_outcome":
                copy.deepcopy(
                    protocol["candidate_generation_outcome"]
                ),
            "other_generation_outcome":
                copy.deepcopy(protocol["other_generation_outcome"]),
            "comparison_lock":
                copy.deepcopy(protocol["comparison_lock"]),
            "downstream_treatment":
                protocol["downstream_treatment"],
            "package_hashes": {
                arm: protocol["packages"][arm]["sha256"]
                for arm in ARMS
            },
        },
    }


def _merge_and_gate(protocol: dict, shard_payloads: list[dict]) -> dict:
    if (protocol.get("gate") or {}).get("mode") == V2_GATE_MODE:
        return _merge_and_gate_v2(protocol, shard_payloads)
    by_key: dict[tuple[str, str, str], dict] = {}
    fingerprint_records: dict[
        tuple[str, str, str], dict[int, dict]
    ] = {}
    all_commits: set[str] = set()
    all_inputs: set[str] = set()
    result_paths: set[str] = set()
    gate_mode = (protocol.get("gate") or {}).get("mode") or "relaxed"
    if gate_mode not in {"relaxed", "short-repair-v1"}:
        raise EvaluationProtocolError("gate.mode is unsupported")
    guard_phase = protocol.get("guard_phase")
    if guard_phase is None:
        guard_phase = "guard" if "guard" in protocol["tasks"] else "dev"

    for shard in shard_payloads:
        phase = str(shard.get("phase"))
        task = str(shard.get("task_id"))
        arm = str(shard.get("arm"))
        key = (phase, task, arm)
        if (phase not in protocol["tasks"] or
                task not in protocol["tasks"][phase] or arm not in ARMS):
            raise EvaluationProtocolError(
                f"shard evidence is outside the preregistered schedule: {key}"
            )
        if key in by_key:
            raise EvaluationProtocolError(f"duplicate shard evidence for {key}")
        num_trials, expected_seeds = _phase_trial_settings(protocol, phase)
        result = shard.get("result")
        if not isinstance(result, dict):
            raise EvaluationProtocolError(f"shard {key} has no result object")
        if set(result.get("matrix") or {}) != {task}:
            raise EvaluationProtocolError(f"shard {key} matrix is not task-local")
        row = result["matrix"][task]
        if (not isinstance(row, list) or len(row) != num_trials or
                any(value not in (0, 1) for value in row)):
            raise EvaluationProtocolError(f"shard {key} has invalid reward row")
        if (result.get("seeds") or {}).get(task) != expected_seeds:
            raise EvaluationProtocolError(f"shard {key} used unexpected trial seeds")
        artifact = result.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("status") != "success":
            raise EvaluationProtocolError(f"shard {key} lacks a successful artifact")
        if artifact.get("requested_task_ids") != [task]:
            raise EvaluationProtocolError(f"shard {key} requested the wrong task")
        if (artifact.get("num_trials") != num_trials or
                artifact.get("base_seed") != TAU2_BASE_SEED or
                artifact.get("trial_seeds") != {task: expected_seeds}):
            raise EvaluationProtocolError(
                f"shard {key} trial/seed execution drifted"
            )
        if (artifact.get("benchmark_protocol") != "official" or
                artifact.get("agent_implementation") != "recuris_agent"):
            raise EvaluationProtocolError(f"shard {key} used the wrong harness")
        expected_package_hash = protocol["packages"][shard["arm"]]["sha256"]
        if artifact.get("package_tree_sha256") != expected_package_hash:
            raise EvaluationProtocolError(f"shard {key} package hash mismatch")
        worker = artifact.get("worker") or {}
        simulator = artifact.get("simulator") or {}
        if (worker.get("model") != FORMAL_DOWNSTREAM_MODEL or
                worker.get("reasoning_effort") != FORMAL_DOWNSTREAM_REASONING or
                simulator.get("model") != FORMAL_DOWNSTREAM_MODEL or
                simulator.get("reasoning_effort") != FORMAL_DOWNSTREAM_REASONING or
                (worker.get("llm_args") or {}).get("temperature") !=
                FORMAL_DOWNSTREAM_TEMPERATURE or
                (simulator.get("llm_args") or {}).get("temperature") !=
                FORMAL_DOWNSTREAM_TEMPERATURE):
            raise EvaluationProtocolError(f"shard {key} treatment drifted")
        resume_policy = artifact.get("resume_policy")
        if (
                not isinstance(resume_policy, dict) or
                resume_policy.get("max_resume_attempts") !=
                protocol["execution"]["resume_attempts"] or
                resume_policy.get("simulation_timeout_seconds") !=
                protocol["execution"]["simulation_timeout_seconds"] or
                resume_policy.get("whole_simulation_retries") !=
                protocol["execution"]["simulation_retries"] or
                resume_policy.get(
                    "whole_simulation_retry_delay_seconds"
                ) != protocol["execution"][
                    "simulation_retry_delay_seconds"
                ]
        ):
            raise EvaluationProtocolError(
                f"shard {key} retry/timeout treatment drifted"
            )
        process_attempts = artifact.get("process_attempts")
        if (not isinstance(process_attempts, list) or not process_attempts or
                any(not isinstance(attempt, dict) or
                    attempt.get("timeout_seconds") !=
                    protocol["execution"]["eval_timeout_seconds"]
                    for attempt in process_attempts)):
            raise EvaluationProtocolError(
                f"shard {key} evaluation timeout treatment drifted"
            )
        observed = result.get("observed_response_models") or {}
        accepted_models = {
            FORMAL_DOWNSTREAM_MODEL, f"openai/{FORMAL_DOWNSTREAM_MODEL}",
        }
        for owner in ("worker", "simulator"):
            actual = observed.get(owner)
            if (not isinstance(actual, list) or not actual or
                    any(model not in accepted_models for model in actual)):
                raise EvaluationProtocolError(
                    f"shard {key} lacks exact actual-model proof for {owner}"
                )
        evidence = (result.get("trial_evidence") or {}).get(task)
        if (not isinstance(evidence, list) or len(evidence) != num_trials or
                sorted(record.get("trial") for record in evidence) !=
                list(range(num_trials))):
            raise EvaluationProtocolError(
                f"shard {key} has incomplete per-trial evidence"
            )
        if any(
                str(record.get("termination_reason") or "") in
                {"infrastructure_error", "timeout"}
                for record in evidence
        ):
            raise EvaluationProtocolError(
                f"shard {key} admitted an infrastructure/timeout outcome"
            )
        result_file = Path(str(artifact.get("results") or ""))
        if (not result_file.is_file() or
                _sha256_file(result_file) != artifact.get("results_sha256")):
            raise EvaluationProtocolError(
                f"shard {key} result artifact hash does not verify"
            )
        fingerprint_file = Path(str(artifact.get("fingerprint") or ""))
        if (not fingerprint_file.is_file() or
                _sha256_file(fingerprint_file) !=
                artifact.get("fingerprint_sha256")):
            raise EvaluationProtocolError(
                f"shard {key} fingerprint artifact hash does not verify"
            )
        if gate_mode == "short-repair-v1":
            expected_embedding = {
                "required": True,
                "mode": "trajectory-embedded-v1",
                "records": num_trials,
                "matches_sidecar": True,
            }
            if (result.get("fingerprint_embedding") != expected_embedding or
                    artifact.get("fingerprint_embedding") !=
                    expected_embedding):
                raise EvaluationProtocolError(
                    f"shard {key} lacks required embedded fingerprint proof"
                )
            fingerprint_records[key] = _fingerprint_records_by_trial(
                result_file, fingerprint_file,
                task=task, num_trials=num_trials,
            )
        all_commits.add(str(result.get("benchmark_git_commit") or ""))
        all_inputs.add(str(result.get("benchmark_input_sha256") or ""))
        result_path = str(artifact.get("results") or "")
        if not result_path or result_path in result_paths:
            raise EvaluationProtocolError(f"shard {key} reused a result artifact")
        result_paths.add(result_path)
        by_key[key] = result

    expected_keys = {
        (phase, task, arm)
        for phase, tasks in protocol["tasks"].items()
        for task in tasks for arm in ARMS
    }
    if set(by_key) != expected_keys:
        missing = sorted(expected_keys - set(by_key))
        extra = sorted(set(by_key) - expected_keys)
        raise EvaluationProtocolError(
            f"exact shard coverage failed; missing={missing}, extra={extra}"
        )
    if len(all_commits) != 1 or not next(iter(all_commits), ""):
        raise EvaluationProtocolError("shards did not bind one benchmark commit")
    if len(all_inputs) != 1 or not next(iter(all_inputs), ""):
        raise EvaluationProtocolError("shards did not bind one benchmark input hash")
    if next(iter(all_commits)) != protocol["benchmark_binding"]["git_commit"]:
        raise EvaluationProtocolError("benchmark commit differs from preregistration")
    if next(iter(all_inputs)) != protocol["benchmark_binding"]["input_sha256"]:
        raise EvaluationProtocolError(
            "benchmark input hash differs from preregistration"
        )
    for arm in ARMS:
        if _package_tree_sha256(
                Path(protocol["packages"][arm]["path"])
        ) != protocol["packages"][arm]["sha256"]:
            raise EvaluationProtocolError(f"{arm} package changed before final gate")
    for name, record in protocol["runtime"].items():
        if _sha256_file(Path(record["path"])) != record["sha256"]:
            raise EvaluationProtocolError(
                f"runtime file {name} changed before final gate"
            )
    freeze_binding = _candidate_freeze_binding(
        protocol.get("candidate_freeze"),
        base_dir=Path.cwd(),
        packages=protocol["packages"],
        required=gate_mode == "short-repair-v1",
        expected_generation=protocol.get("expected_generation"),
        tasks=protocol["tasks"],
        guard_phase=guard_phase,
    )

    matrices: dict[str, dict[str, dict[str, list[int]]]] = {
        phase: {arm: {} for arm in ARMS} for phase in protocol["tasks"]
    }
    fingerprints: dict[str, dict[str, Counter]] = {
        phase: {arm: Counter() for arm in ARMS} for phase in protocol["tasks"]
    }
    for (phase, task, arm), result in by_key.items():
        matrices[phase][arm][task] = result["matrix"][task]
        fingerprints[phase][arm].update(result.get("fingerprint") or {})

    def arm_summary(phase: str, arm: str) -> dict:
        rows = matrices[phase][arm]
        passed = sum(sum(row) for row in rows.values())
        total = sum(len(row) for row in rows.values())
        return {
            "passed": passed,
            "total": total,
            "accuracy": passed / total,
            "matrix": rows,
            "fingerprint": dict(sorted(fingerprints[phase][arm].items())),
        }

    def paired_cells(phase: str) -> dict:
        repaired = 0
        new_failures = 0
        unchanged_pass = 0
        unchanged_fail = 0
        per_task: dict[str, dict[str, int]] = {}
        for task in protocol["tasks"][phase]:
            task_counts = {
                "base_fail_to_candidate_pass": 0,
                "base_pass_to_candidate_fail": 0,
                "unchanged_pass": 0,
                "unchanged_fail": 0,
            }
            for base_value, candidate_value in zip(
                    matrices[phase]["base"][task],
                    matrices[phase]["candidate"][task]):
                if base_value == 0 and candidate_value == 1:
                    repaired += 1
                    task_counts["base_fail_to_candidate_pass"] += 1
                elif base_value == 1 and candidate_value == 0:
                    new_failures += 1
                    task_counts["base_pass_to_candidate_fail"] += 1
                elif base_value == 1:
                    unchanged_pass += 1
                    task_counts["unchanged_pass"] += 1
                else:
                    unchanged_fail += 1
                    task_counts["unchanged_fail"] += 1
            task_counts["net"] = (
                task_counts["base_fail_to_candidate_pass"] -
                task_counts["base_pass_to_candidate_fail"]
            )
            per_task[task] = task_counts
        total = repaired + new_failures + unchanged_pass + unchanged_fail
        return {
            "base_fail_to_candidate_pass": repaired,
            "base_pass_to_candidate_fail": new_failures,
            "unchanged_pass": unchanged_pass,
            "unchanged_fail": unchanged_fail,
            "net": repaired - new_failures,
            "total": total,
            "per_task": per_task,
        }

    guard_base = arm_summary(guard_phase, "base")
    guard_candidate = arm_summary(guard_phase, "candidate")
    repair_base_summary = arm_summary("repair", "base")
    repair_candidate_summary = arm_summary("repair", "candidate")
    repair_base = statistics.mean(
        statistics.mean(matrices["repair"]["base"][task])
        for task in protocol["tasks"]["repair"]
    )
    repair_candidate = statistics.mean(
        statistics.mean(matrices["repair"]["candidate"][task])
        for task in protocol["tasks"]["repair"]
    )
    repair_ok = repair_candidate > repair_base + 1e-9
    cells_per_arm = {
        phase: len(tasks) * _phase_trial_settings(protocol, phase)[0]
        for phase, tasks in protocol["tasks"].items()
    }
    trial_seeds_by_phase = {
        phase: _phase_trial_settings(protocol, phase)[1]
        for phase in protocol["tasks"]
    }
    evidence = {
        "shards": len(shard_payloads),
        f"{guard_phase}_cells_per_arm": cells_per_arm[guard_phase],
        "repair_cells_per_arm": cells_per_arm["repair"],
        "trial_seeds_by_phase": trial_seeds_by_phase,
        "benchmark_git_commit": next(iter(all_commits)),
        "benchmark_input_sha256": next(iter(all_inputs)),
        "package_hashes": {
            arm: protocol["packages"][arm]["sha256"] for arm in ARMS
        },
        "downstream_treatment": protocol["downstream_treatment"],
        "runtime": protocol["runtime"],
        "logical_scoring_cells": 2 * sum(cells_per_arm.values()),
        "process_attempts": sum(
            len((payload["result"]["artifact"].get("process_attempts") or []))
            for payload in shard_payloads
        ),
    }
    if gate_mode == "relaxed":
        evidence["trial_seeds"] = trial_seeds_by_phase[guard_phase]
    if freeze_binding is not None:
        evidence["candidate_freeze"] = freeze_binding

    common = {
        "schema_version": SCHEMA_VERSION,
        "run_id": protocol["run_id"],
        "status": "complete",
        "valid": True,
        "repair": {
            "base": repair_base_summary,
            "candidate": repair_candidate_summary,
            "delta_accuracy": (
                repair_candidate_summary["accuracy"] -
                repair_base_summary["accuracy"]
            ),
            "mean_task_base": repair_base,
            "mean_task_candidate": repair_candidate,
            "improved": repair_ok,
        },
        "evidence": evidence,
    }

    if gate_mode == "relaxed":
        reg_cap = protocol["gate"]["regression_task_cap"]
        dev_verdict = held_out_paired_gate(
            matrices["dev"]["base"], matrices["dev"]["candidate"],
            reg_cap=reg_cap,
        )
        relaxed_accept = (
            dev_verdict.net > 1e-9 and
            dev_verdict.n_regressed <= reg_cap and
            repair_ok
        )
        per_task = {}
        for task in protocol["tasks"]["dev"]:
            base_rate = statistics.mean(matrices["dev"]["base"][task])
            candidate_rate = statistics.mean(
                matrices["dev"]["candidate"][task]
            )
            per_task[task] = {
                "base": base_rate,
                "candidate": candidate_rate,
                "delta": candidate_rate - base_rate,
            }
        return {
            **common,
            "decision": "accept" if relaxed_accept else "reject",
            "claim": (
                "candidate improved under the preregistered relaxed gate"
                if relaxed_accept else
                "candidate did not pass the preregistered relaxed gate"
            ),
        "dev": {
                "base": guard_base,
                "candidate": guard_candidate,
                "delta_accuracy":
                    guard_candidate["accuracy"] - guard_base["accuracy"],
            "paired_bootstrap_ci": list(dev_verdict.ci),
            "improved_tasks": dev_verdict.n_improved,
            "regressed_tasks": dev_verdict.n_regressed,
            "ci_strict_accept": dev_verdict.accept,
            "per_task": per_task,
        },
        "gate": {
            "mode": "relaxed",
            "dev_net_positive": dev_verdict.net > 1e-9,
            "regressed_tasks_within_cap":
                dev_verdict.n_regressed <= reg_cap,
            "repair_improved": repair_ok,
            "accept": relaxed_accept,
            "strict_accept": repair_ok and dev_verdict.accept,
        },
        }

    repair_cells = paired_cells("repair")
    guard_cells = paired_cells(guard_phase)
    overall_cells = {
        name: repair_cells[name] + guard_cells[name]
        for name in (
            "base_fail_to_candidate_pass",
            "base_pass_to_candidate_fail",
            "unchanged_pass",
            "unchanged_fail",
            "total",
        )
    }
    overall_cells["net"] = (
        overall_cells["base_fail_to_candidate_pass"] -
        overall_cells["base_pass_to_candidate_fail"]
    )
    mechanism_rule = protocol["gate"]["required_mechanism"]
    mechanism_events = 0
    causal_mechanism_cells = 0
    for task in protocol["tasks"]["repair"]:
        base_row = matrices["repair"]["base"][task]
        candidate_row = matrices["repair"]["candidate"][task]
        base_records = fingerprint_records[("repair", task, "base")]
        candidate_records = fingerprint_records[
            ("repair", task, "candidate")
        ]
        for trial, (base_value, candidate_value) in enumerate(
                zip(base_row, candidate_row)):
            candidate_matches = sum(
                event.get("kind") == mechanism_rule["event_kind"] and
                event.get("checker") == mechanism_rule["checker"]
                for event in candidate_records[trial]["events"]
            )
            base_matches = sum(
                event.get("kind") == mechanism_rule["event_kind"] and
                event.get("checker") == mechanism_rule["checker"]
                for event in base_records[trial]["events"]
            )
            mechanism_events += candidate_matches
            if (
                    base_value == 0 and candidate_value == 1 and
                    candidate_matches > 0 and base_matches == 0
            ):
                causal_mechanism_cells += 1
    mechanism_triggered = (
        causal_mechanism_cells >= mechanism_rule["min_events"]
    )
    repair_threshold_met = (
        repair_cells["net"] >= protocol["gate"]["repair_min_net_fixes"]
    )
    guard_cap_met = (
        guard_cells["base_pass_to_candidate_fail"] <=
        protocol["gate"]["guard_new_failure_cap"]
    )
    overall_net_positive = overall_cells["net"] > 0
    short_accept = (
        repair_threshold_met and guard_cap_met and
        overall_net_positive and mechanism_triggered
    )
    selection_score = (
        overall_cells["net"] / overall_cells["total"]
        if overall_cells["total"] else 0.0
    )
    return {
        **common,
        "decision": "accept" if short_accept else "reject",
        "claim": (
            "candidate passed the preregistered short repair gate"
            if short_accept else
            "candidate did not pass the preregistered short repair gate"
        ),
        "guard": {
            "phase": guard_phase,
            "base": guard_base,
            "candidate": guard_candidate,
            "delta_accuracy":
                guard_candidate["accuracy"] - guard_base["accuracy"],
            "paired_cells": guard_cells,
        },
        "repair": {
            **common["repair"],
            "paired_cells": repair_cells,
        },
        "paired_cells": {
            "repair": repair_cells,
            "guard": guard_cells,
            "overall": overall_cells,
        },
        "mechanism": {
            "event_kind": mechanism_rule["event_kind"],
            "checker": mechanism_rule["checker"],
            "candidate_repair_events": mechanism_events,
            "causal_repair_cells": causal_mechanism_cells,
            "minimum_events": mechanism_rule["min_events"],
            "triggered": mechanism_triggered,
        },
        "selection_score": selection_score,
        "selection_eligible": short_accept,
        "gate": {
            "mode": "short-repair-v1",
            "repair_net_fixes": repair_cells["net"],
            "repair_threshold_met": repair_threshold_met,
            "guard_new_failures":
                guard_cells["base_pass_to_candidate_fail"],
            "guard_cap_met": guard_cap_met,
            "overall_net": overall_cells["net"],
            "overall_net_positive": overall_net_positive,
            "mechanism_triggered": mechanism_triggered,
            "accept": short_accept,
        },
    }


def _markdown_report(report: dict) -> str:
    if report["gate"]["mode"] == V2_GATE_MODE:
        decision = "PASS" if report["gate"]["accept"] else "REJECT"
        blocks = report["repair"]["blocks"]
        return (
            f"# {report['run_id']} route-aware V2 A/B\n\n"
            f"- Route: {report['route']}\n"
            f"- Base: {report['base']['passed']}/20; candidate: "
            f"{report['candidate']['passed']}/20; delta "
            f"{report['delta_accuracy']:+.1%}\n"
            f"- Repair forward net: "
            f"{blocks['repair_forward']['paired_cells']['net']:+d}; "
            f"causal cells: "
            f"{blocks['repair_forward']['mechanism']['causal_cell_count']}\n"
            f"- Repair reverse net: "
            f"{blocks['repair_reverse']['paired_cells']['net']:+d}; "
            f"causal cells: "
            f"{blocks['repair_reverse']['mechanism']['causal_cell_count']}\n"
            f"- Safety new failures: "
            f"{report['guard']['safety']['new_failures']}; other guard "
            f"new failures: "
            f"{report['guard']['other_combined']['new_failures']}\n"
            f"- Frozen {V2_GATE_MODE} gate: **{decision}**\n"
        )
    if report["gate"]["mode"] == "short-repair-v1":
        repair = report["repair"]
        guard = report["guard"]
        decision = "PASS" if report["gate"]["accept"] else "REJECT"
        return (
            f"# {report['run_id']} short evaluation-only A/B\n\n"
            f"- Repair: base {repair['base']['passed']}/"
            f"{repair['base']['total']}, candidate "
            f"{repair['candidate']['passed']}/"
            f"{repair['candidate']['total']}, paired net "
            f"{repair['paired_cells']['net']:+d}\n"
            f"- Guard: base {guard['base']['passed']}/"
            f"{guard['base']['total']}, candidate "
            f"{guard['candidate']['passed']}/"
            f"{guard['candidate']['total']}, new failures "
            f"{guard['paired_cells']['base_pass_to_candidate_fail']}\n"
            f"- Required mechanism events: "
            f"{report['mechanism']['candidate_repair_events']}\n"
            f"- Selection score: {report['selection_score']:+.4f}\n"
            f"- Preregistered short-repair-v1 gate: **{decision}**\n"
        )
    dev = report["dev"]
    repair = report["repair"]
    decision = "PASS" if report["gate"]["accept"] else "FAIL"
    ci = dev["paired_bootstrap_ci"]
    return (
        f"# {report['run_id']} evaluation-only A/B\n\n"
        f"- Validity: complete and pairable "
        f"({report['evidence']['dev_cells_per_arm']} dev cells/arm; "
        f"{report['evidence']['repair_cells_per_arm']} repair cells/arm)\n"
        f"- Dev: base {dev['base']['passed']}/{dev['base']['total']} "
        f"({dev['base']['accuracy']:.1%}); candidate "
        f"{dev['candidate']['passed']}/{dev['candidate']['total']} "
        f"({dev['candidate']['accuracy']:.1%}); delta "
        f"{dev['delta_accuracy']:+.1%}\n"
        f"- Task-clustered paired bootstrap CI: [{ci[0]:+.1%}, {ci[1]:+.1%}]; "
        f"improved {dev['improved_tasks']}; regressed {dev['regressed_tasks']}\n"
        f"- Repair: base {repair['base']['passed']}/{repair['base']['total']}; "
        f"candidate {repair['candidate']['passed']}/"
        f"{repair['candidate']['total']}; delta "
        f"{repair['delta_accuracy']:+.1%}\n"
        f"- Preregistered relaxed gate: **{decision}**\n"
    )


def _inflight_block_id(shard: dict) -> str:
    block_id = shard.get("block_id")
    if isinstance(block_id, str) and block_id:
        return block_id
    return f"{shard['phase']}_{shard['task_id']}"


def _expected_inflight_records(
        protocol: dict, protocol_sha256: str,
) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for shard in protocol["schedule"]:
        num_trials, expected_seeds = _phase_trial_settings(
            protocol, shard["phase"],
        )
        package = protocol["packages"][shard["arm"]]
        records[shard["id"]] = make_inflight_record(
            run_id=protocol["run_id"],
            protocol_sha256=protocol_sha256,
            shard_id=shard["id"],
            block_id=_inflight_block_id(shard),
            phase=shard["phase"],
            task_id=shard["task_id"],
            arm=shard["arm"],
            package_path=package["path"],
            package_sha256=package["sha256"],
            num_trials=num_trials,
            expected_trial_seeds=expected_seeds,
        )
    return records


def _verify_committed_shard(
        protocol: dict, shards_dir: Path,
        shard_by_id: dict[str, dict], shard_id: str, record: dict,
) -> bool:
    shard = shard_by_id.get(shard_id)
    if shard is None or set(record) != {"path", "sha256"}:
        return False
    expected_path = (shards_dir / f"{shard_id}.json").resolve()
    path_raw = record.get("path")
    expected_hash = str(record.get("sha256") or "").lower()
    if (
            not isinstance(path_raw, str) or not path_raw or
            Path(path_raw).resolve() != expected_path or
            not _valid_sha256(expected_hash)
    ):
        return False
    payload = _load_shard(expected_path, expected_hash)
    identity = {
        "run_id": protocol["run_id"],
        "id": shard["id"],
        "phase": shard["phase"],
        "task_id": shard["task_id"],
        "arm": shard["arm"],
    }
    if "block_id" in shard:
        identity["block_id"] = shard["block_id"]
    if any(payload.get(key) != value for key, value in identity.items()):
        return False
    result = payload.get("result")
    artifact = result.get("artifact") if isinstance(result, dict) else None
    num_trials, expected_seeds = _phase_trial_settings(
        protocol, shard["phase"],
    )
    if (
            not isinstance(result, dict) or
            (result.get("seeds") or {}).get(shard["task_id"]) !=
            expected_seeds or
            not isinstance(artifact, dict) or
            artifact.get("status") != "success" or
            artifact.get("requested_task_ids") != [shard["task_id"]] or
            artifact.get("num_trials") != num_trials or
            artifact.get("trial_seeds") != {
                shard["task_id"]: expected_seeds
            } or
            artifact.get("package_tree_sha256") !=
            protocol["packages"][shard["arm"]]["sha256"]
    ):
        return False
    return True


def _reconcile_state_inflight(
        *, protocol: dict, protocol_sha256: str,
        state: dict, state_path: Path, shards_dir: Path,
) -> None:
    shard_by_id = {
        shard["id"]: shard for shard in protocol["schedule"]
    }
    expected = _expected_inflight_records(
        protocol, protocol_sha256,
    )
    try:
        recovered = reconcile_inflight(
            state,
            expected,
            lambda shard_id, record: _verify_committed_shard(
                protocol, shards_dir, shard_by_id, shard_id, record,
            ),
        )
    except InflightJournalError as exc:
        raise EvaluationProtocolError(str(exc)) from exc
    if recovered:
        _atomic_json(state_path, state)


def _validate_resume_prefix(
        completed: object, schedule: list[dict],
) -> dict:
    if not isinstance(completed, dict):
        raise EvaluationProtocolError(
            "evaluation state has invalid completed_shards"
        )
    schedule_ids = [shard["id"] for shard in schedule]
    completed_ids = set(completed)
    prefix_length = len(completed_ids)
    if (
            completed_ids != set(schedule_ids[:prefix_length]) or
            prefix_length % 2 != 0
    ):
        raise EvaluationProtocolError(
            "interrupted state is not at a complete adjacent A/B pair "
            "boundary; this run is inadmissible"
        )
    return completed


def run(protocol_path: Path) -> dict:
    protocol_path = protocol_path.resolve()
    raw_protocol = _load_json(protocol_path, "protocol")
    protocol = _validate_protocol(raw_protocol, protocol_path)
    protocol_sha = _sha256_file(protocol_path)
    run_dir = Path(protocol["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        with RunFileLock(run_dir / ".evaluation.lock"):
            return _run_locked(
                protocol_path, raw_protocol, protocol,
                protocol_sha, run_dir,
            )
    except EvaluationRunLockError as exc:
        raise EvaluationProtocolError(str(exc)) from exc


def _run_locked(
        protocol_path: Path, raw_protocol: dict,
        protocol: dict, protocol_sha: str, run_dir: Path,
) -> dict:
    state_path = run_dir / "evaluation_state.json"
    shards_dir = run_dir / "evaluation_shards"
    shards_dir.mkdir(parents=True, exist_ok=True)

    resumed_state = state_path.exists()
    if resumed_state:
        state = _load_json(state_path, "evaluation state")
        if state.get("protocol_sha256") != protocol_sha:
            raise EvaluationProtocolError(
                "protocol changed after evaluation state was created"
            )
        if state.get("status") == "failed":
            raise EvaluationProtocolError(
                "evaluation already exhausted its frozen retry budget"
            )
    else:
        state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": protocol["run_id"],
            "status": "running",
            "protocol": str(protocol_path),
            "protocol_sha256": protocol_sha,
            "started_at_unix": time.time(),
            "completed_shards": {},
        }
        _atomic_json(state_path, state)

    _reconcile_state_inflight(
        protocol=protocol,
        protocol_sha256=protocol_sha,
        state=state,
        state_path=state_path,
        shards_dir=shards_dir,
    )
    if state.get("status") == "complete":
        report_path = Path(str(state.get("report") or ""))
        if (
                not report_path.is_file() or
                _sha256_file(report_path) !=
                state.get("report_sha256")
        ):
            raise EvaluationProtocolError(
                "completed evaluation report failed integrity"
            )
        return _load_json(report_path, "completed evaluation report")
    if state.get("status") != "running":
        raise EvaluationProtocolError(
            "evaluation state has an invalid status"
        )
    completed = _validate_resume_prefix(
        state.get("completed_shards"), protocol["schedule"],
    )
    expected_inflight = _expected_inflight_records(
        protocol, protocol_sha,
    )
    shard_by_id = {
        shard["id"]: shard for shard in protocol["schedule"]
    }

    downstream = RecurisDownstream(
        domain=protocol["domain"],
        max_concurrency=protocol["execution"]["max_concurrency"],
        eval_timeout=protocol["execution"]["eval_timeout_seconds"],
        workdir=str(run_dir / "tau2_artifacts"),
        artifact_namespace=protocol["run_id"],
        resume_attempts=protocol["execution"]["resume_attempts"],
        simulation_timeout=protocol["execution"]["simulation_timeout_seconds"],
        simulation_retries=protocol["execution"]["simulation_retries"],
        simulation_retry_delay=(
            protocol["execution"]["simulation_retry_delay_seconds"]
        ),
    )

    try:
        for shard in protocol["schedule"]:
            shard_id = shard["id"]
            shard_path = shards_dir / f"{shard_id}.json"
            if shard_id in completed:
                record = completed[shard_id]
                if not isinstance(record, dict):
                    raise EvaluationProtocolError(
                        f"state record for {shard_id} is invalid"
                    )
                if not _verify_committed_shard(
                        protocol, shards_dir, shard_by_id,
                        shard_id, record):
                    raise EvaluationProtocolError(
                        f"completed shard failed integrity: {shard_id}"
                    )
                continue
            if shard_path.exists():
                raise EvaluationProtocolError(
                    f"orphan shard exists without committed state: {shard_path}"
                )
            for arm in ARMS:
                actual_hash = _package_tree_sha256(
                    Path(protocol["packages"][arm]["path"])
                )
                if actual_hash != protocol["packages"][arm]["sha256"]:
                    raise EvaluationProtocolError(
                        f"{arm} package changed before shard {shard_id}"
                    )
            if protocol["gate"]["mode"] == V2_GATE_MODE:
                # Re-read every V2 trust anchor before each expensive shard.
                # Initial validation alone is insufficient if a generation
                # artifact or the treatment-relevant runtime changes mid-run.
                _validate_protocol(raw_protocol, protocol_path)
            elif protocol["gate"]["mode"] == "short-repair-v1":
                _candidate_freeze_binding(
                    protocol.get("candidate_freeze"),
                    base_dir=protocol_path.parent,
                    packages=protocol["packages"],
                    required=True,
                    expected_generation=protocol.get(
                        "expected_generation"
                    ),
                    tasks=protocol["tasks"],
                    guard_phase=protocol["guard_phase"],
                )
            inflight = expected_inflight[shard_id]
            try:
                begin_inflight(state, inflight)
            except InflightJournalError as exc:
                raise EvaluationProtocolError(str(exc)) from exc
            _atomic_json(state_path, state)
            print(
                f"[eval-only] {shard_id}: {shard['arm']} "
                f"{shard['phase']} task {shard['task_id']}",
                flush=True,
            )
            result = downstream.evaluate(
                protocol["packages"][shard["arm"]]["path"],
                [shard["task_id"]],
                seeds=_phase_trial_settings(
                    protocol, shard["phase"],
                )[0],
                tag=shard_id,
            )
            payload = {
                "schema_version": SCHEMA_VERSION,
                "run_id": protocol["run_id"],
                **shard,
                "completed_at_unix": time.time(),
                "result": asdict(result),
            }
            _atomic_json(shard_path, payload)
            shard_sha = _sha256_file(shard_path)
            completed[shard_id] = {
                "path": str(shard_path),
                "sha256": shard_sha,
            }
            state["completed_shards"] = completed
            state["completed_count"] = len(completed)
            state["last_completed_shard"] = shard_id
            _atomic_json(state_path, state)
            try:
                clear_inflight(state, inflight)
            except InflightJournalError as exc:
                raise EvaluationProtocolError(str(exc)) from exc
            _atomic_json(state_path, state)

        shard_payloads = []
        for shard in protocol["schedule"]:
            record = completed.get(shard["id"])
            if not isinstance(record, dict):
                raise EvaluationProtocolError(
                    f"missing committed shard {shard['id']}"
                )
            shard_payloads.append(_load_shard(
                Path(record["path"]), str(record["sha256"])
            ))
        if protocol["gate"]["mode"] == V2_GATE_MODE:
            _validate_protocol(raw_protocol, protocol_path)
        report = _merge_and_gate(protocol, shard_payloads)
        report["protocol_sha256"] = protocol_sha
        report["finished_at_unix"] = time.time()
        report_path = run_dir / "final_report.json"
        _atomic_json(report_path, report)
        (run_dir / "final_report.md").write_text(
            _markdown_report(report), encoding="utf-8", newline="\n"
        )
        state.update({
            "status": "complete",
            "finished_at_unix": report["finished_at_unix"],
            "report": str(report_path),
            "report_sha256": _sha256_file(report_path),
        })
        _atomic_json(state_path, state)
        return report
    except Exception as exc:
        state.update({
            "status": "failed",
            "finished_at_unix": time.time(),
            "error": {
                "type": type(exc).__name__,
                "message": str(exc)[:4000],
            },
        })
        _atomic_json(state_path, state)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.protocol)
    print(json.dumps({
        "status": report["status"],
        "decision": report["decision"],
        "dev_delta": (
            report.get("dev") or {}
        ).get("delta_accuracy"),
        "guard_delta": (
            report.get("guard") or report.get("dev")
        )["delta_accuracy"],
        "repair_delta": (
            report.get("repair") or {}
        ).get("delta_accuracy"),
        "overall_delta": report.get("delta_accuracy"),
        "selection_score": report.get("selection_score"),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
