# -*- coding: utf-8 -*-
"""REAL downstream: run the actual recuris kernel on a real benchmark and
return a per-task x seed reward matrix + aggregated mechanism fingerprint +
the full text of failed trajectories. Replaces the demo's ToyDownstream.

Currently wired to tau2 (airline/retail), Docker-free text-sim, via a subprocess
call to ``recuris.adapters.tau2.run`` with a given Skill Memory package. The
subprocess isolates crashes and wedges, and reuses the exact entry point the
standalone arms use, so the campaign cannot drift from the arms it is
compared against.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from recuris.metaagent.benchmark_protocol import (
    COMPLETION_GUARD_V1,
    materialize_completion_guard_v1,
)
from recuris.metaagent.benchmark_protocol import (
    OFFICIAL as OFFICIAL_BENCHMARK_PROTOCOL,
)
from recuris.metaagent.benchmark_protocol import (
    SUPPORTED_PROTOCOLS as SUPPORTED_BENCHMARK_PROTOCOLS,
)
from recuris.metaagent.integrity import assert_mutation_paths_safe
from recuris.paths import tau2_data_dir, tau2_root, workspace_root

ROOT = workspace_root()
# Kept as an alias: the workspace root used to be derived from this
# module's location, and both names appear in call sites and tests.
HERE = ROOT
TAU2_ROOT = tau2_root(required=False)
# Defaults to the interpreter running the campaign: recuris and tau2-Bench
# share a virtualenv in the documented setup.
PY = os.environ.get("RECURIS_DOWNSTREAM_PY") or sys.executable
DATA_DIR = str(tau2_data_dir(required=False))
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
FORMAL_BASE_SEED = 300
FORMAL_DOWNSTREAM_MODEL = "doubao-seed-2-0-pro-260215"
FORMAL_DOWNSTREAM_REASONING = "medium"
FORMAL_DOWNSTREAM_TEMPERATURE = 0.0
FORMAL_ALLOWED_OPENAI_PARAMS = ["reasoning_effort"]
FORMAL_DOWNSTREAM_TIMEOUT = 360
FORMAL_DOWNSTREAM_RETRIES = 2
FORMAL_LLM_ARG_KEYS = {
    "temperature", "timeout", "num_retries", "reasoning_effort",
    "allowed_openai_params",
}
EMBED_FINGERPRINT_ENV = "RECURIS_EMBED_FINGERPRINT_V1"
EMBEDDED_FINGERPRINT_KEY = "recuris_fingerprint_v1"


class DownstreamRunError(RuntimeError):
    """A benchmark run is incomplete, ambiguous, or unsafe for a gate."""


class DownstreamTimeoutError(DownstreamRunError):
    """One bounded Tau2 subprocess attempt exceeded its wall-clock cap."""


def _is_linklike(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_tree_sha256(root: Path) -> str:
    """Hash the exact functional package tree without following links.

    The same digest is taken before and after an evaluation.  This binds a
    score to the bytes that were actually supplied to the worker and prevents
    a concurrent edit from being committed as though it had been evaluated.
    """
    root = Path(root)
    if not root.is_dir():
        raise DownstreamRunError(f"Skill Memory package is missing: {root}")
    if _is_linklike(root):
        raise DownstreamRunError(f"refusing linked Skill Memory package: {root}")
    rows: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise DownstreamRunError(f"cannot inspect Skill Memory package {current}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root)
            if entry.is_symlink() or _is_linklike(path):
                raise DownstreamRunError(f"refusing linked path inside Skill Memory: {path}")
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in {"__pycache__", ".pytest_cache"}:
                    stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                if path.suffix.lower() not in {".pyc", ".pyo"}:
                    rows.append(f"{relative.as_posix()}:{_sha256_file(path)}")
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


def _benchmark_git_commit() -> str:
    """Resolve the benchmark checkout itself, never the caller's cwd repo."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(TAU2_ROOT),
            capture_output=True, text=True, timeout=30, check=True,
        )
    except Exception as exc:
        raise DownstreamRunError(
            f"cannot resolve Tau2 benchmark git commit under {TAU2_ROOT}: {exc}"
        ) from exc
    commit = completed.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DownstreamRunError(f"invalid Tau2 benchmark git commit: {commit!r}")
    return commit


def _input_tree_sha256(root: Path) -> str:
    """Hash an exact benchmark-input tree without following links."""
    root = Path(root)
    if not root.is_dir() or _is_linklike(root):
        raise DownstreamRunError(f"benchmark input tree is missing or linked: {root}")
    rows: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise DownstreamRunError(f"cannot inspect benchmark input {current}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root)
            if entry.is_symlink() or _is_linklike(path):
                raise DownstreamRunError(f"refusing linked benchmark input: {path}")
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                rows.append(f"{relative.as_posix()}:{_sha256_file(path)}")
    if not rows:
        raise DownstreamRunError(f"benchmark input tree is empty: {root}")
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


def _benchmark_input_manifest(
    domain: str, data_dir: str | Path | None = None,
) -> dict[str, object]:
    """Bind the exact data copy read through TAU2_DATA_DIR for this run."""
    data_root = Path(DATA_DIR if data_dir is None else data_dir)
    trees = {
        f"tau2/domains/{domain}": data_root / "tau2" / "domains" / domain,
        "tau2/user_simulator": data_root / "tau2" / "user_simulator",
    }
    hashes = {name: _input_tree_sha256(path) for name, path in trees.items()}
    combined = hashlib.sha256(
        "\n".join(f"{name}:{digest}" for name, digest in sorted(hashes.items()))
        .encode("utf-8")
    ).hexdigest()
    return {
        "data_root": str(data_root.resolve()),
        "trees": hashes,
        "combined_sha256": combined,
    }


@dataclass
class Results:
    matrix: dict = field(default_factory=dict)        # task_id -> [0/1]×seeds
    fingerprint: dict = field(default_factory=dict)   # counter_name -> count
    failing: list = field(default_factory=list)       # task_ids with mean<thresh
    trajectories: dict = field(default_factory=dict)  # task_id -> [transcript str]
    failure_details: dict = field(default_factory=dict)  # task_id -> "what specifically failed"
    artifact: dict = field(default_factory=dict)       # hashes + immutable run paths
    seeds: dict = field(default_factory=dict)          # task_id -> per-trial simulation seeds
    benchmark_git_commit: str = ""
    benchmark_input_sha256: str = ""
    benchmark_input_trees: dict = field(default_factory=dict)
    observed_response_models: dict = field(default_factory=dict)
    fingerprint_by_task: dict = field(default_factory=dict)
    failure_owners: dict = field(default_factory=dict)
    trial_evidence: dict = field(default_factory=dict)
    opportunity_summary: dict = field(default_factory=dict)
    fingerprint_embedding: dict = field(default_factory=dict)


def _failure_detail(sim: dict) -> str:
    """Extract WHICH checks failed (reward_breakdown + the expected actions that
    did not match + failed nl-assertions) so the diagnoser targets the real cause
    instead of guessing from the conversation."""
    ri = sim.get("reward_info") or {}
    messages = [message for message in (sim.get("messages") or [])
                if isinstance(message, dict)]
    final = messages[-1] if messages else {}
    final_text = str(final.get("content") or "").replace("\n", " ").strip()
    terminal_user_event = (
        sim.get("termination_reason") == "user_stop"
        and final.get("role") == "user"
    )
    parts = [
        f"termination_reason: {sim.get('termination_reason')!r}; "
        f"final_actor: {final.get('role')!r}; final_contains_STOP: "
        f"{'###STOP###' in final_text}",
        f"final_message: {final_text[:600] or '(empty)'}",
        f"reward_breakdown: {ri.get('reward_breakdown')}  (basis {ri.get('reward_basis')})",
    ]
    if terminal_user_event:
        parts.insert(
            1,
            "post_terminal_assistant_event: absent; no draft_ready or pre_write "
            "event occurs after the final user message",
        )
    dbc = ri.get("db_check") or {}
    if dbc and not dbc.get("db_match", True):
        parts.append("DB STATE MISMATCH: the write-actions taken did not produce the required database state.")
    miss = []
    for ac in (ri.get("action_checks") or []):
        if not ac.get("action_match", True):
            a = ac.get("action") or {}
            miss.append(f"  - REQUIRED action NOT correctly done: {a.get('name')}({json.dumps(a.get('arguments') or {})[:160]})")
    if miss:
        parts.append("MISSING/WRONG ACTIONS:\n" + "\n".join(miss[:8]))
    nlf = [c for c in (ri.get("nl_assertions") or []) if not c.get("met", True)]
    if nlf:
        parts.append("FAILED nl-assertions: " + "; ".join(str(c.get("nl_assertion") or c.get("info"))[:120] for c in nlf[:5]))
    comf = [c for c in (ri.get("communicate_checks") or []) if not c.get("met", True)]
    if comf:
        parts.append("FAILED communicate-checks: " + "; ".join(str(c.get("info"))[:80] for c in comf[:5]))
    return "\n".join(parts)


def _required_write_names(reward_info: dict, *, missing_only: bool) -> set[str]:
    names = {
        str((check.get("action") or {}).get("name") or "")
        for check in (reward_info.get("action_checks") or [])
        if isinstance(check, dict)
        and check.get("tool_type") == "write"
        and (not missing_only or not check.get("action_match", True))
    }
    names.discard("")
    return names


def _actual_tool_call_names(messages: list[dict]) -> set[str]:
    names = {
        str((call.get("function") or {}).get("name") or call.get("name") or "")
        for message in messages
        for call in (message.get("tool_calls") or [])
        if isinstance(call, dict)
    }
    names.discard("")
    return names


def _action_opportunity(sim: dict) -> dict[str, object]:
    """Return conservative, auditable O evidence for one exact trial.

    An observed evaluator-required write call proves an execution opportunity.
    All other cases stay ``unknown``: interpreting dialogue or termination
    markers is causal diagnosis and belongs to the Meta-Agent, not the adapter.
    """
    reward_info = sim.get("reward_info") or {}
    messages = [message for message in (sim.get("messages") or [])
                if isinstance(message, dict)]
    missing = _required_write_names(reward_info, missing_only=True)
    required = _required_write_names(reward_info, missing_only=False) | missing
    attempted = required & _actual_tool_call_names(messages)
    if attempted:
        return {
            "status": "present",
            "basis": "observed_required_write_call",
            "required_actions": sorted(required),
            "reason": (
                "At least one benchmark-required write appears in the actual tool-call "
                f"trace: {sorted(attempted)}."
            ),
        }
    return {
        "status": "unknown",
        "basis": "not_determined",
        "required_actions": sorted(required),
        "reason": (
            "The deterministic adapter did not prove either an action opportunity "
            "or an impossible action turn; Meta-Agent diagnosis must cite the trace."
        ),
    }


def _read_env(name: str) -> str:
    """Read one key from .env without ever echoing it."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    envf = ROOT / ".env"
    if not envf.is_file():
        return ""
    for line in envf.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _clip_piece(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[message middle omitted]...\n"
    room = limit - len(marker)
    left = room // 2
    return text[:left] + marker + text[-(room - left):]


def _render_transcript(sim: dict, max_chars: int = 12000) -> str:
    """Render a sim's messages into a compact readable transcript for the
    LLM diagnoser (role: content [+ tool calls / tool results])."""
    messages = [message for message in (sim.get("messages") or [])
                if isinstance(message, dict)]
    final = messages[-1] if messages else {}
    terminal_user_event = (
        sim.get("termination_reason") == "user_stop"
        and final.get("role") == "user"
    )
    out = [
        "[simulation_meta] "
        f"termination_reason={sim.get('termination_reason')!r}; "
        f"message_count={len(messages)}; final_role={final.get('role')!r}; "
        f"final_contains_STOP={'###STOP###' in str(final.get('content') or '')}; "
        "post_terminal_assistant_event="
        f"{'absent' if terminal_user_event else 'not_applicable'}"
    ]
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content") or ""
        piece = f"[{role}] {content}".strip()
        tcs = m.get("tool_calls") or []
        for tc in tcs:
            fn = (tc.get("function") or {}).get("name") or tc.get("name") or "?"
            args = (tc.get("function") or {}).get("arguments") or tc.get("arguments") or ""
            piece += f"\n    -> tool_call {fn}({args})"
        if role == "tool":
            piece = f"[tool_result] {content}"
        if piece.strip():
            out.append(_clip_piece(piece))
    text = "\n".join(out)
    if len(text) <= max_chars:
        return text
    marker = "\n...[transcript middle omitted; terminal tail preserved]...\n"
    room = max_chars - len(marker)
    head = room // 2
    return text[:head] + marker + text[-(room - head):]


def _validate_open_worker_args(agent_model: str, agent_args: str) -> dict:
    """Validate an open-worker arm with the same rule the standalone arms use.

    Importing the adapter's validator rather than restating it is the point:
    a campaign arm and a hand-launched arm are only comparable if they were
    admitted by identical checks.
    """
    from recuris.adapters.tau2.treatment import validate_open_downstream_agent

    return validate_open_downstream_agent(
        agent_model=agent_model, agent_args=agent_args)


class RecurisDownstream:
    """Evaluate a real package: run the tau2 arm, parse the score matrix,
    the mechanism fingerprint, and the trajectories of the failures."""

    def __init__(self, domain: str = "airline", max_concurrency: int = 4,
                 fail_thresh: float = 0.5, workdir: str | None = None,
                 llm_args: str | None = None, eval_timeout: int = 1500,
                 worker_model: str = FORMAL_DOWNSTREAM_MODEL,
                 worker_reasoning: str = FORMAL_DOWNSTREAM_REASONING,
                 simulator_model: str = FORMAL_DOWNSTREAM_MODEL,
                 simulator_reasoning: str = FORMAL_DOWNSTREAM_REASONING,
                 worker_llm_args: str | None = None,
                 simulator_llm_args: str | None = None,
                 artifact_namespace: str | None = None,
                 benchmark_protocol: str = OFFICIAL_BENCHMARK_PROTOCOL,
                 resume_attempts: int = 0,
                 simulation_timeout: float | None = None,
                 simulation_retries: int = 0,
                 simulation_retry_delay: float = 1.0,
                 retrieval_config: str | None = None,
                 open_worker: bool = False):
        # Knowledge domains (banking_knowledge) pick their KB access mode via
        # tau2's --retrieval-config; the sanitized subprocess env drops every
        # TAU2_* variable by design, so the choice must ride an explicit,
        # provenance-recorded parameter rather than the caller's environment.
        self.retrieval_config = str(retrieval_config) if retrieval_config else None
        self.domain = domain
        self.max_concurrency = max_concurrency
        self.fail_thresh = fail_thresh
        self.eval_timeout = eval_timeout
        if (simulation_timeout is not None and
                (isinstance(simulation_timeout, bool) or
                 not isinstance(simulation_timeout, (int, float)) or
                 float(simulation_timeout) <= 0)):
            raise ValueError("simulation_timeout must be positive or None")
        if (not isinstance(simulation_retries, int) or
                isinstance(simulation_retries, bool) or
                simulation_retries < 0):
            raise ValueError("simulation_retries must be a non-negative integer")
        if (isinstance(simulation_retry_delay, bool) or
                not isinstance(simulation_retry_delay, (int, float)) or
                float(simulation_retry_delay) < 0):
            raise ValueError("simulation_retry_delay must be non-negative")
        self.simulation_timeout = (
            float(simulation_timeout) if simulation_timeout is not None else None
        )
        self.simulation_retries = simulation_retries
        self.simulation_retry_delay = float(simulation_retry_delay)
        if (not isinstance(resume_attempts, int) or isinstance(resume_attempts, bool) or
                not 0 <= resume_attempts <= 3):
            raise ValueError("resume_attempts must be an integer between 0 and 3")
        self.resume_attempts = resume_attempts
        namespace = artifact_namespace or f"standalone-{os.getpid()}-{time.time_ns()}"
        if not SAFE_LABEL_RE.fullmatch(namespace):
            raise ValueError("artifact_namespace must be a safe single path component")
        base_workdir = (Path(workdir) if workdir else
                        (Path(DATA_DIR) / "simulations" / "metaagent_fingerprints"))
        lexical_workdir = Path(base_workdir).absolute() / namespace
        for candidate in (Path(base_workdir).absolute(), lexical_workdir):
            if candidate.exists() and _is_linklike(candidate):
                raise DownstreamRunError(f"refusing linked artifact directory: {candidate}")
        self.workdir = lexical_workdir.resolve()
        assert_mutation_paths_safe([self.workdir])
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.artifact_namespace = namespace
        if benchmark_protocol not in SUPPORTED_BENCHMARK_PROTOCOLS:
            raise ValueError(
                f"benchmark_protocol must be one of {sorted(SUPPORTED_BENCHMARK_PROTOCOLS)}"
            )
        self.benchmark_protocol = benchmark_protocol
        self.benchmark_protocol_manifest: dict[str, object] | None = None
        if benchmark_protocol == OFFICIAL_BENCHMARK_PROTOCOL:
            self.data_dir = Path(DATA_DIR).resolve()
        elif benchmark_protocol == COMPLETION_GUARD_V1:
            guarded_root = self.workdir / "benchmark_inputs" / COMPLETION_GUARD_V1
            assert_mutation_paths_safe([guarded_root])
            self.data_dir, self.benchmark_protocol_manifest = (
                materialize_completion_guard_v1(
                    source_root=Path(DATA_DIR), destination=guarded_root,
                    domain=self.domain,
                )
            )
        else:  # exhaustive guard for future protocol additions
            raise AssertionError(benchmark_protocol)
        self.worker_model = worker_model
        self.worker_reasoning = worker_reasoning
        self.simulator_model = simulator_model
        self.simulator_reasoning = simulator_reasoning
        self.open_worker = bool(open_worker)
        if self.open_worker:
            # Open-worker arms: the executing agent is served elsewhere over
            # an OpenAI-compatible endpoint. Only the worker is unfrozen -- the
            # simulator is not what is being optimised, so it stays pinned to
            # the reference model and the comparison stays single-variable.
            if not str(worker_model).startswith("openai/"):
                raise ValueError(
                    "open_worker requires worker_model like 'openai/<served-name>'")
            frozen = {
                "simulator_model": (simulator_model, FORMAL_DOWNSTREAM_MODEL),
                "simulator_reasoning": (simulator_reasoning,
                                        FORMAL_DOWNSTREAM_REASONING),
            }
        else:
            frozen = {
                "worker_model": (worker_model, FORMAL_DOWNSTREAM_MODEL),
                "worker_reasoning": (worker_reasoning, FORMAL_DOWNSTREAM_REASONING),
                "simulator_model": (simulator_model, FORMAL_DOWNSTREAM_MODEL),
                "simulator_reasoning": (simulator_reasoning,
                                        FORMAL_DOWNSTREAM_REASONING),
            }
        for field_name, (actual, expected) in frozen.items():
            if actual != expected:
                raise ValueError(f"{field_name} is frozen at {expected!r}")
        # ``llm_args`` is retained only for old callers.  New experiments keep
        # the downstream worker and Tau2 user simulator independently frozen.
        default_worker = json.dumps({"temperature": 0.0,
                                     "timeout": FORMAL_DOWNSTREAM_TIMEOUT,
                                     "num_retries": FORMAL_DOWNSTREAM_RETRIES,
                                     "reasoning_effort": worker_reasoning,
                                     "allowed_openai_params":
                                         FORMAL_ALLOWED_OPENAI_PARAMS})
        default_simulator = json.dumps({"temperature": 0.0,
                                        "timeout": FORMAL_DOWNSTREAM_TIMEOUT,
                                        "num_retries": FORMAL_DOWNSTREAM_RETRIES,
                                        "reasoning_effort": simulator_reasoning,
                                        "allowed_openai_params":
                                            FORMAL_ALLOWED_OPENAI_PARAMS})
        self.worker_llm_args = worker_llm_args or llm_args or default_worker
        self.simulator_llm_args = simulator_llm_args or llm_args or default_simulator
        parsed_args: dict[str, dict] = {}
        if self.open_worker:
            if not worker_llm_args:
                raise ValueError(
                    "open_worker requires explicit worker_llm_args JSON "
                    "(api_base/temperature/timeout/num_retries"
                    "[,extra_body,stop_token_ids,max_tokens])")
            parsed_args["worker"] = _validate_open_worker_args(
                worker_model, self.worker_llm_args)
        _validate_pairs = (
            (("simulator", self.simulator_llm_args, simulator_reasoning),)
            if self.open_worker else
            (("worker", self.worker_llm_args, worker_reasoning),
             ("simulator", self.simulator_llm_args, simulator_reasoning)))
        for label, raw, expected_reasoning in _validate_pairs:
            try:
                parsed = json.loads(raw)
            except Exception as exc:
                raise ValueError(f"invalid {label} llm args JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"{label} llm args must be a JSON object")
            if parsed.get("reasoning_effort") != expected_reasoning:
                raise ValueError(
                    f"{label} llm args reasoning_effort must be {expected_reasoning!r}"
                )
            temperature = parsed.get("temperature")
            if (isinstance(temperature, bool) or
                    not isinstance(temperature, (int, float)) or
                    float(temperature) != FORMAL_DOWNSTREAM_TEMPERATURE):
                raise ValueError(
                    f"{label} llm args temperature must be "
                    f"{FORMAL_DOWNSTREAM_TEMPERATURE}"
                )
            if parsed.get("allowed_openai_params") != FORMAL_ALLOWED_OPENAI_PARAMS:
                raise ValueError(
                    f"{label} llm args must explicitly allow only "
                    "reasoning_effort through LiteLLM"
                )
            if parsed.get("timeout") != FORMAL_DOWNSTREAM_TIMEOUT:
                raise ValueError(
                    f"{label} llm args timeout must be {FORMAL_DOWNSTREAM_TIMEOUT}"
                )
            retries = parsed.get("num_retries")
            if (isinstance(retries, bool) or not isinstance(retries, int) or
                    retries != FORMAL_DOWNSTREAM_RETRIES):
                raise ValueError(
                    f"{label} llm args num_retries must be {FORMAL_DOWNSTREAM_RETRIES}"
                )
            if set(parsed) != FORMAL_LLM_ARG_KEYS:
                raise ValueError(
                    f"{label} llm args keys must exactly equal the frozen formal set "
                    f"{sorted(FORMAL_LLM_ARG_KEYS)}"
                )
            parsed_args[label] = parsed
        self.worker_llm_args_record = parsed_args["worker"]
        self.simulator_llm_args_record = parsed_args["simulator"]
        self._eval_i = 0
        self.artifacts: list[dict[str, object]] = []

    def evaluate(self, package_path: str | None, task_ids, seeds: int = 4,
                 tag: str = "eval") -> Results:
        """package_path=None means the bare llm_agent (no skill memory).
        Returns Results with matrix/fingerprint/failing/trajectories."""
        if not SAFE_LABEL_RE.fullmatch(tag):
            raise ValueError("evaluation tag must be a safe single path component")
        if not isinstance(seeds, int) or isinstance(seeds, bool) or seeds < 1:
            raise ValueError("seeds must be a positive integer")
        if task_ids is None:
            raise ValueError("Meta-Agent evaluations require an explicit task-id list")
        requested = [str(task) for task in task_ids]
        if not requested or len(requested) != len(set(requested)):
            raise ValueError("task_ids must be non-empty and unique")
        if any(not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", task)
               for task in requested):
            # Retail/airline ids are numeric; knowledge domains use safe
            # identifiers like task_001. Both stay shell- and path-safe.
            raise ValueError("Tau2 task_ids must be safe identifiers")
        benchmark_commit = _benchmark_git_commit()
        benchmark_inputs = _benchmark_input_manifest(self.domain, self.data_dir)
        skill_mode = bool(package_path)
        package_root: Path | None = None
        package_sha256: str | None = None
        if skill_mode:
            lexical_package = Path(str(package_path)).absolute()
            if _is_linklike(lexical_package):
                raise DownstreamRunError(f"refusing linked Skill Memory package: {lexical_package}")
            package_root = lexical_package.resolve()
            package_sha256 = _package_tree_sha256(package_root)
        self._eval_i += 1
        token = f"{self._eval_i:04d}_{tag}_{uuid.uuid4().hex[:12]}"
        save_rel = f"metaagent/{self.artifact_namespace}/{token}"
        # Tau2 resolves ``save_to`` under the active TAU2_DATA_DIR.  Alternate
        # benchmark protocols therefore write results beside their materialized
        # input tree, not under the official data root.
        save_abs = self.data_dir / "simulations" / save_rel
        save_namespace_root = save_abs.parent
        if save_namespace_root.exists() and _is_linklike(save_namespace_root):
            raise DownstreamRunError(f"refusing linked Tau2 namespace: {save_namespace_root}")
        fp_path = self.workdir / f"{token}_fp.jsonl"
        log_path = self.workdir / f"{token}.log"
        embed_cache = self.workdir / "embed_cache" / (package_sha256 or "bare")
        assert_mutation_paths_safe([save_abs, fp_path, log_path, embed_cache])
        # Never delete/reuse benchmark artifacts. A collision is evidence of a
        # broken namespace and must fail before launching the subprocess.
        if save_abs.exists() or fp_path.exists() or log_path.exists():
            raise DownstreamRunError(f"refusing to overwrite evaluation artifact: {token}")

        expected_agent = "recuris_agent" if skill_mode else "llm_agent"
        result_path = save_abs / "results.json"
        sanitized_prefixes = ("TAU2_", "RECURIS_", "OPENAI_", "ANTHROPIC_", "LITELLM_")
        artifact: dict[str, object] = {
            "index": self._eval_i,
            "token": token,
            "tag": tag,
            "status": "started",
            "started_at_unix": time.time(),
            "save_to": save_rel,
            "fingerprint": str(fp_path),
            "fingerprint_sha256": None,
            "log": str(log_path),
            "results": str(result_path),
            "results_sha256": None,
            "package_tree_sha256": package_sha256,
            "agent_implementation": expected_agent,
            "requested_task_ids": requested,
            "num_trials": seeds,
            "base_seed": FORMAL_BASE_SEED,
            "trial_seeds": None,
            "benchmark_git_commit": benchmark_commit,
            "benchmark_protocol": self.benchmark_protocol,
            "benchmark_protocol_manifest": self.benchmark_protocol_manifest,
            "benchmark_inputs": benchmark_inputs,
            "observed_response_models": None,
            "worker": {
                "model": self.worker_model,
                "reasoning_effort": self.worker_reasoning,
                "llm_args": self.worker_llm_args_record,
            },
            "simulator": {
                "model": self.simulator_model,
                "reasoning_effort": self.simulator_reasoning,
                "llm_args": self.simulator_llm_args_record,
            },
            "sanitized_env_prefixes": list(sanitized_prefixes),
            "controlled_mechanism_env": None,
            "resume_policy": {
                "max_resume_attempts": self.resume_attempts,
                "mode": "same-checkpoint-explicit-resume",
                "simulation_timeout_seconds": self.simulation_timeout,
                "whole_simulation_retries": self.simulation_retries,
                "whole_simulation_retry_delay_seconds":
                    self.simulation_retry_delay,
                "retryable_termination_reasons": [
                    "infrastructure_error", "timeout",
                ],
            },
            "process_attempts": [],
        }
        # Register before reading credentials or launching Tau2. Driver persists
        # this record in a finally block, so a charged failed attempt remains
        # auditable instead of disappearing from provenance.
        self.artifacts.append(artifact)
        try:
            # A previous ablation shell may carry switches that alter the
            # treatment. Formal runs start clean and inject only controlled
            # paths plus the configured provider endpoint.
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(sanitized_prefixes)}
            env.update({
                "PYTHONUTF8": "1", "PYTHONPATH": "src",
                "TAU2_DATA_DIR": str(self.data_dir),
                "OPENAI_API_KEY": _read_env("OPENAI_API_KEY"),
                "OPENAI_BASE_URL": _read_env("OPENAI_BASE_URL"),
                "RECURIS_FINGERPRINT": str(fp_path),
                # The builtin cache key predates package-body versioning. Scope
                # it by run namespace + exact package digest so one candidate,
                # embedder implementation, or offline test cannot poison
                # another formal treatment.
                "RECURIS_EMBED_CACHE": str(embed_cache),
            })
            if self.retrieval_config:
                env["TAU2_RETRIEVAL_CONFIG"] = self.retrieval_config
                artifact["tau2_retrieval_config"] = self.retrieval_config
            if skill_mode and self.resume_attempts > 0:
                env[EMBED_FINGERPRINT_ENV] = "1"
            if not env["OPENAI_API_KEY"] or not env["OPENAI_BASE_URL"]:
                raise DownstreamRunError(
                    "OPENAI_API_KEY/OPENAI_BASE_URL are missing from process env and .env"
                )
            cmd = [PY, "-m", "recuris.adapters.tau2.run", "--domain", self.domain,
                   "--save-to", save_rel, "--num-trials", str(seeds),
                   "--max-concurrency", str(self.max_concurrency),
                   "--max-retries", str(self.simulation_retries),
                   "--retry-delay", str(self.simulation_retry_delay),
                   "--agent-llm", self._provider_model(self.worker_model),
                   "--user-llm", self._provider_model(self.simulator_model),
                   "--agent-llm-args", self.worker_llm_args,
                   "--user-llm-args", self.simulator_llm_args]
            if self.open_worker:
                cmd.append("--open-downstream")
            if self.simulation_timeout is not None:
                cmd += ["--simulation-timeout", str(self.simulation_timeout)]
            if skill_mode:
                env["RECURIS_SKILL_MEMORY"] = str(package_root)
            else:
                env.pop("RECURIS_SKILL_MEMORY", None)
            cmd += ["--agent", expected_agent, "--task-ids", *requested]
            artifact["controlled_mechanism_env"] = {
                "TAU2_DATA_DIR": env["TAU2_DATA_DIR"],
                "RECURIS_FINGERPRINT": env["RECURIS_FINGERPRINT"],
                "RECURIS_SKILL_MEMORY": env.get("RECURIS_SKILL_MEMORY"),
                "RECURIS_EMBED_CACHE": env["RECURIS_EMBED_CACHE"],
                EMBED_FINGERPRINT_ENV: env.get(EMBED_FINGERPRINT_ENV),
            }
            try:
                self._run_with_checkpoint_resume(
                    cmd, env, timeout=self.eval_timeout, log_path=log_path,
                    result_path=result_path, fp_path=fp_path,
                    expected_task_ids=requested, expected_trials=seeds,
                    expected_agent=expected_agent,
                    expected_benchmark_commit=benchmark_commit,
                    require_embedded_fingerprint=(
                        skill_mode and self.resume_attempts > 0
                    ),
                    artifact=artifact,
                )
            finally:
                if package_root is not None:
                    package_after = _package_tree_sha256(package_root)
                    if package_after != package_sha256:
                        raise DownstreamRunError(
                            "Skill Memory package changed while it was being evaluated; "
                            "the result is not admissible"
                        )
            if _benchmark_git_commit() != benchmark_commit:
                raise DownstreamRunError(
                    "Tau2 benchmark git commit changed during evaluation"
                )
            benchmark_inputs_after = _benchmark_input_manifest(self.domain, self.data_dir)
            if benchmark_inputs_after != benchmark_inputs:
                raise DownstreamRunError(
                    "Tau2 benchmark input data changed during evaluation"
                )
            if skill_mode and self.resume_attempts > 0:
                artifact["fingerprint_reconciliation"] = (
                    self._finalize_committed_fingerprint(
                        result_path=result_path, fp_path=fp_path,
                        log_path=log_path,
                        expected_task_ids=requested,
                        expected_trials=seeds,
                        expected_agent=expected_agent,
                        expected_benchmark_commit=benchmark_commit,
                    )
                )
            result = self._parse(
                result_path, str(fp_path), requested, seeds,
                expected_agent=expected_agent, require_fingerprint=skill_mode,
                expected_benchmark_commit=benchmark_commit,
                benchmark_inputs=benchmark_inputs,
                require_embedded_fingerprint=(
                    skill_mode and self.resume_attempts > 0
                ),
            )
            artifact.update({
                "status": "success",
                "finished_at_unix": time.time(),
                "fingerprint_sha256": (_sha256_file(fp_path)
                                       if fp_path.is_file() else None),
                "results_sha256": _sha256_file(result_path),
                "trial_seeds": result.seeds,
                "benchmark_git_commit": result.benchmark_git_commit,
                "observed_response_models": result.observed_response_models,
                "opportunity_summary": result.opportunity_summary,
                "fingerprint_embedding": result.fingerprint_embedding,
            })
        except Exception as exc:
            artifact.update({
                "status": "failed",
                "finished_at_unix": time.time(),
                "error": {"type": type(exc).__name__, "message": str(exc)[:2000]},
            })
            for path, hash_key in (
                    (fp_path, "fingerprint_sha256"),
                    (result_path, "results_sha256")):
                if path.is_file():
                    try:
                        artifact[hash_key] = _sha256_file(path)
                    except OSError:
                        pass
            raise
        result.artifact = artifact
        return result

    @staticmethod
    def _provider_model(model: str) -> str:
        return model if "/" in model else f"openai/{model}"

    @staticmethod
    def _resume_command(command: list[str]) -> list[str]:
        if "--resume" in command:
            return list(command)
        try:
            task_index = command.index("--task-ids")
        except ValueError as exc:
            raise DownstreamRunError(
                "cannot construct a Tau2 resume command without --task-ids"
            ) from exc
        return [*command[:task_index], "--resume", *command[task_index:]]

    @staticmethod
    def _validated_fingerprint_record(
            record: object, *, label: str,
            expected_tags: set[str]) -> dict[str, object]:
        if not isinstance(record, dict):
            raise DownstreamRunError(f"{label} is not an object")
        tag = record.get("sim_tag")
        counters = record.get("counters")
        events = record.get("events")
        if tag not in expected_tags:
            raise DownstreamRunError(
                f"{label} has unexpected sim_tag {tag!r}"
            )
        if not isinstance(counters, dict):
            raise DownstreamRunError(f"{label} has no counters object")
        if not isinstance(events, list) or any(
                not isinstance(event, dict) for event in events):
            raise DownstreamRunError(f"{label} has an invalid events list")
        for key, value in counters.items():
            if (not isinstance(key, str) or not key or
                    not isinstance(value, int) or isinstance(value, bool) or
                    value < 0):
                raise DownstreamRunError(
                    f"{label} contains an invalid counter"
                )
        return {
            "sim_tag": str(tag),
            "counters": dict(counters),
            "events": list(events),
        }

    def _load_partial_checkpoint(
            self, result_path: Path, expected_task_ids: list[str],
            expected_trials: int, *, expected_agent: str,
            expected_benchmark_commit: str,
            require_embedded_fingerprint: bool,
    ) -> tuple[dict, list[tuple[str, int, dict[str, object]]], list[tuple[str, int]]]:
        if not result_path.is_file():
            raise DownstreamRunError(
                "cannot resume Tau2: timeout produced no results checkpoint"
            )
        try:
            document = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise DownstreamRunError(
                f"cannot resume Tau2 from an invalid checkpoint: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise DownstreamRunError(
                "cannot resume Tau2: checkpoint root is not an object"
            )
        self._validate_results_info(
            document, expected_agent=expected_agent,
            expected_trials=expected_trials,
            expected_benchmark_commit=expected_benchmark_commit,
        )
        simulations = document.get("simulations")
        if not isinstance(simulations, list):
            raise DownstreamRunError(
                "cannot resume Tau2: checkpoint has no simulations list"
            )
        expected_tasks = set(expected_task_ids)
        seen: set[tuple[str, int]] = set()
        completed: set[tuple[str, int]] = set()
        seed_by_trial: dict[int, int] = {}
        embedded: list[tuple[str, int, dict[str, object]]] = []
        for sim_index, sim in enumerate(simulations, start=1):
            if not isinstance(sim, dict):
                raise DownstreamRunError(
                    f"cannot resume Tau2: simulation {sim_index} is not an object"
                )
            task = str(sim.get("task_id"))
            trial = sim.get("trial")
            if task not in expected_tasks:
                raise DownstreamRunError(
                    f"cannot resume Tau2: unexpected task_id {task!r}"
                )
            if (not isinstance(trial, int) or isinstance(trial, bool) or
                    not 0 <= trial < expected_trials):
                raise DownstreamRunError(
                    f"cannot resume Tau2: task {task} has invalid trial {trial!r}"
                )
            cell = (task, trial)
            if cell in seen:
                raise DownstreamRunError(
                    f"cannot resume Tau2: duplicate checkpoint cell {cell}"
                )
            seen.add(cell)
            sim_seed = sim.get("seed")
            if not isinstance(sim_seed, int) or isinstance(sim_seed, bool):
                raise DownstreamRunError(
                    f"cannot resume Tau2: task {task} trial {trial} has invalid seed"
                )
            prior_seed = seed_by_trial.setdefault(trial, sim_seed)
            if prior_seed != sim_seed:
                raise DownstreamRunError(
                    f"cannot resume Tau2: trial {trial} has inconsistent seeds"
                )
            termination_reason = str(sim.get("termination_reason") or "")
            # These outcomes describe execution reliability, not downstream
            # task quality.  They are deliberately left uncommitted so the
            # same task/trial/seed cell can be retried from the checkpoint.
            if termination_reason in {"infrastructure_error", "timeout"}:
                continue
            reward = (sim.get("reward_info") or {}).get("reward")
            if (not isinstance(reward, (int, float)) or isinstance(reward, bool) or
                    not math.isfinite(float(reward)) or
                    not 0.0 <= float(reward) <= 1.0):
                raise DownstreamRunError(
                    f"cannot resume Tau2: task {task} trial {trial} has invalid reward"
                )
            messages = sim.get("messages")
            if not isinstance(messages, list):
                raise DownstreamRunError(
                    f"cannot resume Tau2: task {task} trial {trial} has no messages"
                )
            actual_counts = {"worker": 0, "simulator": 0}
            embedded_records: list[object] = []
            for message in messages:
                if not isinstance(message, dict):
                    raise DownstreamRunError(
                        f"cannot resume Tau2: task {task} trial {trial} has "
                        "a non-object message"
                    )
                raw = message.get("raw_data")
                if isinstance(raw, dict) and EMBEDDED_FINGERPRINT_KEY in raw:
                    embedded_records.append(raw[EMBEDDED_FINGERPRINT_KEY])
                role = message.get("role")
                owner = "worker" if role == "assistant" else (
                    "simulator" if role == "user" else None
                )
                actual_model = raw.get("model") if isinstance(raw, dict) else None
                if owner is None or not actual_model:
                    continue
                expected_model = (
                    self.worker_model if owner == "worker" else self.simulator_model
                )
                if str(actual_model) not in {
                        expected_model, self._provider_model(expected_model),
                        str(expected_model).split("/", 1)[-1]}:
                    raise DownstreamRunError(
                        f"cannot resume Tau2: task {task} trial {trial} {owner} "
                        f"reports model {actual_model!r}"
                    )
                actual_counts[owner] += 1
            missing_models = [
                owner for owner, count in actual_counts.items() if count == 0
            ]
            if missing_models:
                raise DownstreamRunError(
                    f"cannot resume Tau2: task {task} trial {trial} lacks "
                    f"response-model proof for {missing_models}"
                )
            if require_embedded_fingerprint:
                if len(embedded_records) != 1:
                    raise DownstreamRunError(
                        f"cannot safely resume Tau2: task {task} trial {trial} "
                        f"has {len(embedded_records)} embedded fingerprints"
                    )
                record = self._validated_fingerprint_record(
                    embedded_records[0],
                    label=f"task {task} trial {trial} embedded fingerprint",
                    expected_tags={f"task{task}"},
                )
                embedded.append((task, trial, record))
            completed.add(cell)
        maximum = len(expected_task_ids) * expected_trials
        if len(seen) > maximum:
            raise DownstreamRunError(
                "cannot resume Tau2: checkpoint exceeds requested Cartesian coverage"
            )
        order = {task: index for index, task in enumerate(expected_task_ids)}
        embedded.sort(key=lambda item: (order[item[0]], item[1]))
        return document, embedded, sorted(
            completed, key=lambda item: (order[item[0]], item[1])
        )

    @staticmethod
    def _exclusive_copy(source: Path, destination: Path) -> None:
        if destination.exists() or _is_linklike(destination):
            raise DownstreamRunError(
                f"refusing to overwrite checkpoint snapshot: {destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)

    @staticmethod
    def _replace_fingerprint_from_embedded(
            fp_path: Path,
            embedded: list[tuple[str, int, dict[str, object]]]) -> str:
        serialized = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for _, _, record in embedded
        ).encode("utf-8")
        temp_path = fp_path.with_name(
            f".{fp_path.name}.reconcile-{uuid.uuid4().hex}.tmp"
        )
        assert_mutation_paths_safe([fp_path, temp_path])
        if temp_path.exists() or _is_linklike(temp_path):
            raise DownstreamRunError(
                f"refusing unsafe fingerprint reconciliation path: {temp_path}"
            )
        try:
            with temp_path.open("xb") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, fp_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return _sha256_file(fp_path)

    @staticmethod
    def _replace_checkpoint_document(result_path: Path, document: dict) -> str:
        serialized = (
            json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        temp_path = result_path.with_name(
            f".{result_path.name}.reconcile-{uuid.uuid4().hex}.tmp"
        )
        assert_mutation_paths_safe([result_path, temp_path])
        if temp_path.exists() or _is_linklike(temp_path):
            raise DownstreamRunError(
                f"refusing unsafe checkpoint reconciliation path: {temp_path}"
            )
        try:
            with temp_path.open("xb") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, result_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return _sha256_file(result_path)

    @staticmethod
    def _raw_fingerprint_summary(
            fp_path: Path, expected_task_ids: list[str]) -> dict[str, object]:
        expected_tags = {f"task{task}" for task in expected_task_ids}
        counts = {tag: 0 for tag in sorted(expected_tags)}
        invalid_records = 0
        unexpected_tags: dict[str, int] = {}
        total_records = 0
        if not fp_path.is_file():
            return {
                "records": 0,
                "valid_records": 0,
                "invalid_records": 0,
                "records_by_tag": counts,
                "unexpected_tags": {},
            }
        for index, line in enumerate(
                fp_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines(), start=1):
            if not line.strip():
                continue
            total_records += 1
            tag = None
            try:
                record = json.loads(line)
                tag = record.get("sim_tag") if isinstance(record, dict) else None
                RecurisDownstream._validated_fingerprint_record(
                    record, label=f"raw fingerprint line {index}",
                    expected_tags=expected_tags,
                )
            except Exception:
                invalid_records += 1
                if isinstance(tag, str) and tag not in expected_tags:
                    unexpected_tags[tag] = unexpected_tags.get(tag, 0) + 1
                continue
            counts[str(tag)] += 1
        return {
            "records": total_records,
            "valid_records": total_records - invalid_records,
            "invalid_records": invalid_records,
            "records_by_tag": counts,
            "unexpected_tags": unexpected_tags,
        }

    def _prepare_checkpoint_resume(
            self, *, result_path: Path, fp_path: Path, log_path: Path,
            attempt_number: int, expected_task_ids: list[str],
            expected_trials: int, expected_agent: str,
            expected_benchmark_commit: str,
            require_embedded_fingerprint: bool,
    ) -> dict[str, object]:
        document, embedded, completed_cells = self._load_partial_checkpoint(
            result_path, expected_task_ids, expected_trials,
            expected_agent=expected_agent,
            expected_benchmark_commit=expected_benchmark_commit,
            require_embedded_fingerprint=require_embedded_fingerprint,
        )
        snapshot_stem = f"{log_path.stem}.attempt{attempt_number}"
        result_snapshot = self.workdir / f"{snapshot_stem}.partial_results.json"
        fp_snapshot = self.workdir / f"{snapshot_stem}.partial_fingerprint.jsonl"
        assert_mutation_paths_safe([result_snapshot, fp_snapshot, fp_path])
        self._exclusive_copy(result_path, result_snapshot)
        raw_fp_hash = None
        raw_fp_records = 0
        if fp_path.is_file():
            self._exclusive_copy(fp_path, fp_snapshot)
            raw_fp_hash = _sha256_file(fp_snapshot)
            raw_fp_records = len([
                line for line in fp_snapshot.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines() if line.strip()
            ])
        else:
            fp_snapshot = None

        completed_set = set(completed_cells)
        simulations = document.get("simulations")
        if not isinstance(simulations, list):
            raise DownstreamRunError(
                "cannot reconcile Tau2 checkpoint without simulations"
            )
        retryable_cells: list[tuple[str, int]] = []
        retained_simulations: list[dict] = []
        for sim in simulations:
            cell = (str(sim.get("task_id")), int(sim.get("trial")))
            if cell in completed_set:
                retained_simulations.append(sim)
            else:
                retryable_cells.append(cell)
        sanitized_results_hash = _sha256_file(result_path)
        if retryable_cells:
            document["simulations"] = retained_simulations
            sanitized_results_hash = self._replace_checkpoint_document(
                result_path, document
            )

        reconciled_hash = None
        if require_embedded_fingerprint:
            reconciled_hash = self._replace_fingerprint_from_embedded(
                fp_path, embedded
            )

        expected_total = len(expected_task_ids) * expected_trials
        return {
            "completed_simulations": len(completed_cells),
            "expected_simulations": expected_total,
            "complete_checkpoint": len(completed_cells) == expected_total,
            "completed_cells": [
                {"task_id": task, "trial": trial}
                for task, trial in completed_cells
            ],
            "partial_results_snapshot": str(result_snapshot),
            "partial_results_sha256": _sha256_file(result_snapshot),
            "partial_fingerprint_snapshot": (
                str(fp_snapshot) if fp_snapshot is not None else None
            ),
            "partial_fingerprint_sha256": raw_fp_hash,
            "partial_fingerprint_records": raw_fp_records,
            "embedded_fingerprint_records": len(embedded),
            "reconciled_fingerprint_sha256": reconciled_hash,
            "sidecar_record_delta": raw_fp_records - len(embedded),
            "retryable_cells_removed": [
                {"task_id": task, "trial": trial}
                for task, trial in retryable_cells
            ],
            "sanitized_results_sha256": sanitized_results_hash,
        }

    def _finalize_committed_fingerprint(
            self, *, result_path: Path, fp_path: Path, log_path: Path,
            expected_task_ids: list[str], expected_trials: int,
            expected_agent: str, expected_benchmark_commit: str,
    ) -> dict[str, object]:
        """Separate attempt-level sidecar rows from committed simulations.

        Tau2 may internally retry one task. ``agent.stop`` runs for the
        discarded attempt too, so the append-only sidecar is intentionally an
        attempt log. The trajectory-embedded records are bound to the final
        SimulationRun checkpoints and are therefore the only admissible
        mechanism evidence.
        """
        _, embedded, completed_cells = self._load_partial_checkpoint(
            result_path, expected_task_ids, expected_trials,
            expected_agent=expected_agent,
            expected_benchmark_commit=expected_benchmark_commit,
            require_embedded_fingerprint=True,
        )
        expected_total = len(expected_task_ids) * expected_trials
        if len(completed_cells) != expected_total:
            raise DownstreamRunError(
                "cannot finalize fingerprints before exact Cartesian coverage"
            )

        raw_summary = self._raw_fingerprint_summary(
            fp_path, expected_task_ids
        )
        raw_snapshot = self.workdir / (
            f"{log_path.stem}.raw_attempt_fingerprint.jsonl"
        )
        raw_hash = None
        if fp_path.is_file():
            assert_mutation_paths_safe([raw_snapshot])
            self._exclusive_copy(fp_path, raw_snapshot)
            raw_hash = _sha256_file(raw_snapshot)
        else:
            raw_snapshot = None

        committed_counts = {
            f"task{task}": 0 for task in expected_task_ids
        }
        for _, _, record in embedded:
            committed_counts[str(record["sim_tag"])] += 1
        raw_counts = dict(raw_summary["records_by_tag"])
        delta_by_tag = {
            tag: int(raw_counts.get(tag, 0)) - count
            for tag, count in committed_counts.items()
            if int(raw_counts.get(tag, 0)) - count != 0
        }
        committed_hash = self._replace_fingerprint_from_embedded(
            fp_path, embedded
        )
        return {
            "authority": "trajectory-embedded-v1",
            "raw_attempt_sidecar": (
                str(raw_snapshot) if raw_snapshot is not None else None
            ),
            "raw_attempt_sidecar_sha256": raw_hash,
            "raw_attempt_summary": raw_summary,
            "committed_records": len(embedded),
            "committed_records_by_tag": committed_counts,
            "attempt_minus_committed_by_tag": delta_by_tag,
            "committed_sidecar": str(fp_path),
            "committed_sidecar_sha256": committed_hash,
        }

    def _run_with_checkpoint_resume(
            self, command: list[str], env: dict[str, str], *, timeout: int,
            log_path: Path, result_path: Path, fp_path: Path,
            expected_task_ids: list[str], expected_trials: int,
            expected_agent: str, expected_benchmark_commit: str,
            require_embedded_fingerprint: bool,
            artifact: dict[str, object]) -> None:
        current_command = list(command)
        attempts = artifact["process_attempts"]
        if not isinstance(attempts, list):
            raise AssertionError("process_attempts must be a list")
        for attempt_index in range(self.resume_attempts + 1):
            attempt_number = attempt_index + 1
            attempt_log = (
                log_path if attempt_index == 0 else
                log_path.with_name(
                    f"{log_path.stem}.resume{attempt_index}{log_path.suffix}"
                )
            )
            record: dict[str, object] = {
                "attempt": attempt_number,
                "resume": attempt_index > 0,
                "log": str(attempt_log),
                "timeout_seconds": timeout,
                "started_at_unix": time.time(),
            }
            attempts.append(record)
            try:
                self._run_bounded(
                    current_command, env, timeout=timeout, log_path=attempt_log
                )
            except DownstreamTimeoutError as exc:
                record.update({
                    "status": "timeout",
                    "finished_at_unix": time.time(),
                    "error": str(exc)[:2000],
                })
                if attempt_index >= self.resume_attempts:
                    raise
                checkpoint = self._prepare_checkpoint_resume(
                    result_path=result_path, fp_path=fp_path,
                    log_path=log_path, attempt_number=attempt_number,
                    expected_task_ids=expected_task_ids,
                    expected_trials=expected_trials,
                    expected_agent=expected_agent,
                    expected_benchmark_commit=expected_benchmark_commit,
                    require_embedded_fingerprint=require_embedded_fingerprint,
                )
                record["checkpoint_after_timeout"] = checkpoint
                if checkpoint["complete_checkpoint"]:
                    record["status"] = "timeout_with_complete_checkpoint"
                    return
                current_command = self._resume_command(command)
            except Exception as exc:
                record.update({
                    "status": "failed",
                    "finished_at_unix": time.time(),
                    "error": f"{type(exc).__name__}: {str(exc)[:1900]}",
                })
                raise
            else:
                if self.resume_attempts == 0:
                    record.update({
                        "status": "success",
                        "finished_at_unix": time.time(),
                    })
                    return
                _, _, completed_cells = self._load_partial_checkpoint(
                    result_path, expected_task_ids, expected_trials,
                    expected_agent=expected_agent,
                    expected_benchmark_commit=expected_benchmark_commit,
                    require_embedded_fingerprint=require_embedded_fingerprint,
                )
                expected_total = len(expected_task_ids) * expected_trials
                if len(completed_cells) != expected_total:
                    checkpoint = self._prepare_checkpoint_resume(
                        result_path=result_path, fp_path=fp_path,
                        log_path=log_path, attempt_number=attempt_number,
                        expected_task_ids=expected_task_ids,
                        expected_trials=expected_trials,
                        expected_agent=expected_agent,
                        expected_benchmark_commit=expected_benchmark_commit,
                        require_embedded_fingerprint=require_embedded_fingerprint,
                    )
                    record.update({
                        "status": "retryable_incomplete_checkpoint",
                        "finished_at_unix": time.time(),
                        "checkpoint_after_exit": checkpoint,
                    })
                    if attempt_index >= self.resume_attempts:
                        raise DownstreamRunError(
                            "Tau2 exited without complete valid task x trial "
                            f"coverage ({len(completed_cells)}/{expected_total}); "
                            "infrastructure retry budget exhausted"
                        )
                    # Endpoint connection outages last minutes; an immediate
                    # retry lands inside the same outage window (observed
                    # twice: 0/48 and 5/48 coverage with back-to-back
                    # attempts).  Back off before resuming.
                    time.sleep(min(600.0, 90.0 * (2 ** max(attempt_index, 0))))
                    current_command = self._resume_command(command)
                    continue
                record.update({
                    "status": "success",
                    "finished_at_unix": time.time(),
                })
                return
        raise AssertionError("checkpoint resume loop exhausted without a terminal result")

    def _run_bounded(self, cmd, env, timeout, log_path: Path):
        """Run the eval subprocess with a wall-clock cap; on timeout, tree-kill
        (a wedged tau2 sub-run would otherwise hang the whole loop forever).
        Partial output is diagnostic only and is never admitted to a gate."""
        with log_path.open("x", encoding="utf-8") as log_stream:
            p = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                 stdout=log_stream, stderr=subprocess.STDOUT,
                                 text=True)
            try:
                return_code = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                # kill the whole process tree (parent + tau2 worker children)
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(p.pid)] if os.name == "nt"
                    else ["bash", "-c",
                          f"pkill -9 -P {p.pid} 2>/dev/null; kill -9 {p.pid} 2>/dev/null; true"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    p.wait(timeout=15)
                except Exception:
                    pass
                raise DownstreamTimeoutError(
                    f"Tau2 evaluation timed out after {timeout}s; partial results are rejected"
                ) from exc
        if return_code != 0:
            raise DownstreamRunError(
                f"Tau2 evaluation exited with code {return_code}; see {log_path}"
            )

    def _validate_results_info(self, document: dict, *, expected_agent: str,
                               expected_trials: int,
                               expected_benchmark_commit: str | None = None) -> None:
        """Reject results whose recorded treatment differs from the request."""
        info = document.get("info")
        if not isinstance(info, dict):
            raise DownstreamRunError("Tau2 results.json has no info object")
        if info.get("num_trials") != expected_trials:
            raise DownstreamRunError(
                f"Tau2 info.num_trials is {info.get('num_trials')!r}, "
                f"expected {expected_trials}"
            )
        if info.get("seed") != FORMAL_BASE_SEED:
            raise DownstreamRunError(
                f"Tau2 info.seed is {info.get('seed')!r}, expected {FORMAL_BASE_SEED}"
            )
        recorded_commit = str(info.get("git_commit") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", recorded_commit):
            raise DownstreamRunError("Tau2 info.git_commit is missing or invalid")
        if (expected_benchmark_commit is not None and
                recorded_commit != expected_benchmark_commit):
            raise DownstreamRunError(
                f"Tau2 info.git_commit is {recorded_commit!r}, but the benchmark "
                f"checkout used by the driver is {expected_benchmark_commit!r}"
            )
        agent_info = info.get("agent_info")
        user_info = info.get("user_info")
        environment_info = info.get("environment_info")
        if not isinstance(agent_info, dict):
            raise DownstreamRunError("Tau2 results info.agent_info is missing or invalid")
        if not isinstance(user_info, dict):
            raise DownstreamRunError("Tau2 results info.user_info is missing or invalid")
        if not isinstance(environment_info, dict):
            raise DownstreamRunError("Tau2 results info.environment_info is missing or invalid")
        if agent_info.get("implementation") != expected_agent:
            raise DownstreamRunError(
                f"Tau2 agent implementation is {agent_info.get('implementation')!r}, "
                f"expected {expected_agent!r}"
            )
        if user_info.get("implementation") != "user_simulator":
            raise DownstreamRunError(
                f"Tau2 user implementation is {user_info.get('implementation')!r}, "
                "expected 'user_simulator'"
            )
        if environment_info.get("domain_name") != self.domain:
            raise DownstreamRunError(
                f"Tau2 result domain is {environment_info.get('domain_name')!r}, "
                f"expected {self.domain!r}"
            )

        expected_worker_model = self._provider_model(self.worker_model)
        expected_user_model = self._provider_model(self.simulator_model)
        if agent_info.get("llm") != expected_worker_model:
            raise DownstreamRunError(
                f"Tau2 worker model is {agent_info.get('llm')!r}, "
                f"expected {expected_worker_model!r}"
            )
        if user_info.get("llm") != expected_user_model:
            raise DownstreamRunError(
                f"Tau2 user model is {user_info.get('llm')!r}, "
                f"expected {expected_user_model!r}"
            )
        worker_args = agent_info.get("llm_args")
        user_args = user_info.get("llm_args")
        if not isinstance(worker_args, dict):
            raise DownstreamRunError("Tau2 worker llm_args are missing or invalid")
        if not isinstance(user_args, dict):
            raise DownstreamRunError("Tau2 user llm_args are missing or invalid")
        if (not getattr(self, "open_worker", False) and
                worker_args.get("reasoning_effort") != self.worker_reasoning):
            raise DownstreamRunError(
                f"Tau2 worker reasoning_effort is {worker_args.get('reasoning_effort')!r}, "
                f"expected {self.worker_reasoning!r}"
            )
        if user_args.get("reasoning_effort") != self.simulator_reasoning:
            raise DownstreamRunError(
                f"Tau2 user reasoning_effort is {user_args.get('reasoning_effort')!r}, "
                f"expected {self.simulator_reasoning!r}"
            )
        if worker_args != self.worker_llm_args_record:
            raise DownstreamRunError("Tau2 worker llm_args differ from the frozen request")
        if user_args != self.simulator_llm_args_record:
            raise DownstreamRunError("Tau2 user llm_args differ from the frozen request")

    @staticmethod
    def _parse_fingerprint(fp_path: Path, expected_task_ids: list[str],
                           expected_trials: int, *, required: bool) -> tuple[dict, dict]:
        if not required:
            return {}, {}
        if not fp_path.is_file():
            raise DownstreamRunError(f"Skill evaluation fingerprint is missing: {fp_path}")
        try:
            lines = [line.strip() for line in fp_path.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
        except Exception as exc:
            raise DownstreamRunError(f"cannot read Skill evaluation fingerprint: {exc}") from exc
        expected_records = len(expected_task_ids) * expected_trials
        if len(lines) != expected_records:
            raise DownstreamRunError(
                f"Skill evaluation fingerprint has {len(lines)} records, "
                f"expected {expected_records}"
            )
        expected_tags = {f"task{task}" for task in expected_task_ids}
        tag_to_task = {f"task{task}": task for task in expected_task_ids}
        tag_counts = {tag: 0 for tag in expected_tags}
        aggregate: dict[str, int] = {}
        by_task: dict[str, dict[str, int]] = {task: {} for task in expected_task_ids}
        for index, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except Exception as exc:
                raise DownstreamRunError(
                    f"fingerprint line {index} is invalid JSON: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise DownstreamRunError(f"fingerprint line {index} is not an object")
            tag = record.get("sim_tag")
            counters = record.get("counters")
            events = record.get("events")
            if tag not in expected_tags:
                raise DownstreamRunError(
                    f"fingerprint line {index} has unexpected sim_tag {tag!r}"
                )
            if not isinstance(counters, dict):
                raise DownstreamRunError(
                    f"fingerprint line {index} has no counters object"
                )
            if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                raise DownstreamRunError(
                    f"fingerprint line {index} has an invalid events list"
                )
            tag_counts[tag] += 1
            task_counters = by_task[tag_to_task[tag]]
            for key, value in counters.items():
                if (not isinstance(key, str) or not key or not isinstance(value, int) or
                        isinstance(value, bool) or value < 0):
                    raise DownstreamRunError(
                        f"fingerprint line {index} contains an invalid counter"
                    )
                aggregate[key] = aggregate.get(key, 0) + value
                task_counters[key] = task_counters.get(key, 0) + value
        wrong_counts = {tag: count for tag, count in tag_counts.items()
                        if count != expected_trials}
        if wrong_counts:
            raise DownstreamRunError(
                f"fingerprint task/trial attribution is incomplete: {wrong_counts}"
            )
        return aggregate, by_task

    def _validate_embedded_fingerprint_consistency(
            self, document: dict, fp_path: Path,
            expected_task_ids: list[str], expected_trials: int, *,
            required: bool) -> dict[str, object]:
        simulations = document.get("simulations")
        if not isinstance(simulations, list):
            raise DownstreamRunError(
                "cannot validate embedded fingerprints without simulations"
            )
        expected_tags = {f"task{task}" for task in expected_task_ids}
        embedded: list[dict[str, object]] = []
        missing: list[str] = []
        duplicate: list[str] = []
        for sim in simulations:
            task = str(sim.get("task_id"))
            trial = sim.get("trial")
            records = []
            for message in sim.get("messages") or []:
                raw = message.get("raw_data") if isinstance(message, dict) else None
                if isinstance(raw, dict) and EMBEDDED_FINGERPRINT_KEY in raw:
                    records.append(raw[EMBEDDED_FINGERPRINT_KEY])
            label = f"{task}:{trial}"
            if not records:
                missing.append(label)
                continue
            if len(records) != 1:
                duplicate.append(label)
                continue
            embedded.append(self._validated_fingerprint_record(
                records[0], label=f"task {task} trial {trial} embedded fingerprint",
                expected_tags={f"task{task}"},
            ))
        if not embedded and not required:
            return {
                "required": False,
                "mode": "legacy-sidecar-only",
                "records": 0,
                "matches_sidecar": None,
            }
        if missing or duplicate:
            raise DownstreamRunError(
                "embedded fingerprint coverage is incomplete or ambiguous; "
                f"missing={missing}, duplicate={duplicate}"
            )
        expected_records = len(expected_task_ids) * expected_trials
        if len(embedded) != expected_records:
            raise DownstreamRunError(
                f"embedded fingerprint has {len(embedded)} records, "
                f"expected {expected_records}"
            )
        if not fp_path.is_file():
            raise DownstreamRunError(
                "cannot cross-check embedded fingerprints: sidecar is missing"
            )
        sidecar: list[dict[str, object]] = []
        for index, line in enumerate(
                fp_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except Exception as exc:
                raise DownstreamRunError(
                    f"fingerprint line {index} is invalid JSON: {exc}"
                ) from exc
            sidecar.append(self._validated_fingerprint_record(
                record, label=f"fingerprint line {index}",
                expected_tags=expected_tags,
            ))
        canonical = lambda record: json.dumps(  # noqa: E731
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        embedded_multiset = Counter(canonical(record) for record in embedded)
        sidecar_multiset = Counter(canonical(record) for record in sidecar)
        if embedded_multiset != sidecar_multiset:
            raise DownstreamRunError(
                "embedded fingerprints do not match the sidecar multiset"
            )
        return {
            "required": required,
            "mode": "trajectory-embedded-v1",
            "records": len(embedded),
            "matches_sidecar": True,
        }

    def _parse(self, results_json: Path, fp_path: str,
               expected_task_ids: list[str], expected_trials: int, *,
               expected_agent: str, require_fingerprint: bool,
               expected_benchmark_commit: str | None = None,
               benchmark_inputs: dict | None = None,
               require_embedded_fingerprint: bool = False) -> Results:
        if not results_json.is_file():
            raise DownstreamRunError(f"Tau2 results.json is missing: {results_json}")
        matrix, trajectories, failure_details = {}, {}, {}
        trial_evidence: dict[str, list[dict[str, object]]] = {}
        try:
            d = json.loads(results_json.read_text(encoding="utf-8"))
        except Exception as exc:
            raise DownstreamRunError(f"Tau2 results.json is invalid: {exc}") from exc
        if not isinstance(d, dict):
            raise DownstreamRunError("Tau2 results.json root must be an object")
        self._validate_results_info(
            d, expected_agent=expected_agent, expected_trials=expected_trials,
            expected_benchmark_commit=expected_benchmark_commit,
        )
        simulations = d.get("simulations")
        if not isinstance(simulations, list):
            raise DownstreamRunError("Tau2 results.json has no simulations list")
        expected_tasks = set(expected_task_ids)
        trials_by_task: dict[str, set[int]] = {task: set() for task in expected_task_ids}
        seen: set[tuple[str, int]] = set()
        seed_by_key: dict[tuple[str, int], int] = {}
        seed_by_trial: dict[int, int] = {}
        observed_models: dict[str, set[str]] = {"worker": set(), "simulator": set()}
        for sim in simulations:
            if not isinstance(sim, dict):
                raise DownstreamRunError("Tau2 results contain a non-object simulation")
            tid = str(sim.get("task_id"))
            trial = sim.get("trial")
            if tid not in expected_tasks:
                raise DownstreamRunError(f"Tau2 returned unexpected task_id {tid!r}")
            if not isinstance(trial, int) or isinstance(trial, bool):
                raise DownstreamRunError(f"task {tid} has invalid trial {trial!r}")
            key = (tid, trial)
            if key in seen:
                raise DownstreamRunError(f"duplicate Tau2 result for task/trial {key}")
            seen.add(key)
            trials_by_task[tid].add(trial)
            sim_seed = sim.get("seed")
            if not isinstance(sim_seed, int) or isinstance(sim_seed, bool):
                raise DownstreamRunError(f"task {tid} trial {trial} has invalid seed {sim_seed!r}")
            previous_seed = seed_by_trial.setdefault(trial, sim_seed)
            if previous_seed != sim_seed:
                raise DownstreamRunError(
                    f"Tau2 trial {trial} used inconsistent seeds across tasks"
                )
            seed_by_key[key] = sim_seed
            messages = sim.get("messages")
            if not isinstance(messages, list):
                raise DownstreamRunError(f"task {tid} trial {trial} has no messages list")
            per_sim_actual = {"worker": 0, "simulator": 0}
            for message in messages:
                if not isinstance(message, dict):
                    raise DownstreamRunError(
                        f"task {tid} trial {trial} contains a non-object message"
                    )
                role = message.get("role")
                owner = "worker" if role == "assistant" else (
                    "simulator" if role == "user" else None
                )
                raw = message.get("raw_data")
                actual_model = raw.get("model") if isinstance(raw, dict) else None
                if owner is None or not actual_model:
                    continue
                expected_model = (self.worker_model if owner == "worker"
                                  else self.simulator_model)
                accepted_names = {expected_model, self._provider_model(expected_model),
                                  str(expected_model).split("/", 1)[-1]}
                if str(actual_model) not in accepted_names:
                    raise DownstreamRunError(
                        f"task {tid} trial {trial} {owner} response reports model "
                        f"{actual_model!r}, expected {expected_model!r}"
                    )
                observed_models[owner].add(str(actual_model))
                per_sim_actual[owner] += 1
            missing_actual = [owner for owner, count in per_sim_actual.items() if count == 0]
            if missing_actual:
                raise DownstreamRunError(
                    f"task {tid} trial {trial} lacks actual response-model proof for "
                    f"{missing_actual}"
                )
            reward = (sim.get("reward_info") or {}).get("reward")
            if not isinstance(reward, (int, float)) or isinstance(reward, bool):
                raise DownstreamRunError(f"task {tid} trial {trial} has no numeric reward")
            r = float(reward)
            if not math.isfinite(r) or not 0.0 <= r <= 1.0:
                raise DownstreamRunError(f"task {tid} trial {trial} has invalid reward {reward!r}")
            matrix.setdefault(tid, []).append(1 if r >= 1.0 else 0)
            passed = r >= 1.0
            action_opportunity = _action_opportunity(sim)
            rendered = _render_transcript(sim)
            detail = "" if passed else _failure_detail(sim)
            embedded_fires = None
            for message in (sim.get("messages") or []):
                raw_block = message.get("raw_data") if isinstance(message, dict) else None
                if isinstance(raw_block, dict) and EMBEDDED_FINGERPRINT_KEY in raw_block:
                    embedded_fires = raw_block[EMBEDDED_FINGERPRINT_KEY]
                    break
            trial_evidence.setdefault(tid, []).append({
                "trial": trial,
                "seed": sim_seed,
                "passed": passed,
                "fires": embedded_fires,
                "reward": r,
                "termination_reason": str(sim.get("termination_reason") or ""),
                "preclassified_owner": None,
                "action_opportunity": action_opportunity,
                "failure_detail": detail,
                "transcript": rendered,
            })
            if r < 1.0:
                trajectories.setdefault(tid, []).append(rendered)
                if tid not in failure_details:
                    failure_details[tid] = detail
        expected_trial_set = set(range(expected_trials))
        coverage_errors = [
            f"{task}: got trials {sorted(trials)}, expected {sorted(expected_trial_set)}"
            for task, trials in trials_by_task.items() if trials != expected_trial_set
        ]
        if coverage_errors:
            raise DownstreamRunError(
                "incomplete Tau2 task x trial coverage; gate rejected:\n  " +
                "\n  ".join(coverage_errors)
            )
        # Sort by trial rather than trusting result serialization order.
        ordered: dict[str, list[int]] = {}
        reward_by_key = {(str(sim["task_id"]), int(sim["trial"])):
                         1 if float(sim["reward_info"]["reward"]) >= 1.0 else 0
                         for sim in simulations}
        for task in expected_task_ids:
            ordered[task] = [reward_by_key[(task, trial)]
                             for trial in range(expected_trials)]
        matrix = ordered
        for task in trial_evidence:
            trial_evidence[task].sort(key=lambda record: int(record["trial"]))
        opportunity_summary = {
            "all": {status: 0 for status in ("present", "absent", "unknown")},
            "failed": {status: 0 for status in ("present", "absent", "unknown")},
        }
        for records in trial_evidence.values():
            for record in records:
                status = str((record.get("action_opportunity") or {}).get("status"))
                opportunity_summary["all"][status] += 1
                if not record.get("passed"):
                    opportunity_summary["failed"][status] += 1
        opportunity_summary["total"] = sum(opportunity_summary["all"].values())
        opportunity_summary["failed_total"] = sum(
            opportunity_summary["failed"].values()
        )
        ordered_seeds = {
            task: [seed_by_key[(task, trial)] for trial in range(expected_trials)]
            for task in expected_task_ids
        }

        fp, fp_by_task = self._parse_fingerprint(
            Path(fp_path), expected_task_ids, expected_trials,
            required=require_fingerprint,
        )
        fingerprint_embedding = self._validate_embedded_fingerprint_consistency(
            d, Path(fp_path), expected_task_ids, expected_trials,
            required=require_embedded_fingerprint,
        )
        failing = [t for t, row in matrix.items()
                   if statistics.mean(row) < self.fail_thresh]
        failure_owners: dict[str, dict[str, str]] = {}
        input_manifest = benchmark_inputs or {}
        return Results(matrix=matrix, fingerprint=fp, failing=failing,
                       trajectories=trajectories, failure_details=failure_details,
                       seeds=ordered_seeds,
                       benchmark_git_commit=str((d.get("info") or {})["git_commit"]),
                       benchmark_input_sha256=str(
                           input_manifest.get("combined_sha256") or ""
                       ),
                       benchmark_input_trees=dict(input_manifest.get("trees") or {}),
                       fingerprint_by_task=fp_by_task,
                       failure_owners=failure_owners,
                       trial_evidence=trial_evidence,
                       opportunity_summary=opportunity_summary,
                       fingerprint_embedding=fingerprint_embedding,
                       observed_response_models={
                           owner: sorted(models) for owner, models in observed_models.items()
                       })
