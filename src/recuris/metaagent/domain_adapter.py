# -*- coding: utf-8 -*-
"""Domain-neutral contract for the self-evolving Meta-Agent.

This module deliberately contains no benchmark implementation and performs no
I/O.  Adapters translate a downstream runtime into these immutable records so
the evolution loop can reason about packages, evidence, patches, and gates
without knowing how a domain names its evaluation items.

``ItemId`` and ``TrialId`` are opaque strings.  Consumers must compare and
serialize them verbatim; parsing, sorting numerically, or inferring semantics
from their spelling is outside this contract.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, NewType, Protocol, Sequence, runtime_checkable

ItemId = NewType("ItemId", str)
TrialId = NewType("TrialId", str)


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty, trimmed string")
    return value


def _tokens(values: Sequence[str], field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field} must be a sequence of opaque strings, not one string")
    result = tuple(_token(value, f"{field}[]") for value in values)
    if not result and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{field} contains duplicates")
    return result


def _finite(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


class FailureOwner(str, Enum):
    """The smallest component or external system that owns a failure."""

    E = "E"
    W = "W"
    RHO = "RHO"
    C = "C"
    MODEL = "MODEL"
    HARNESS = "HARNESS"
    BENCHMARK = "BENCHMARK"

    @property
    def memory_patchable(self) -> bool:
        return self in {self.E, self.W, self.RHO, self.C}


class OpportunityStatus(str, Enum):
    """Whether the worker had a legal chance to perform the required step."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class PatchabilityStatus(str, Enum):
    """Whether the diagnosed root cause belongs to the Skill Memory surface."""

    MEMORY = "memory"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class AttemptStatus(str, Enum):
    """Whether a scheduled evaluation cell produced an admissible score."""

    SCORED = "SCORED"
    MODEL_ERROR = "MODEL_ERROR"
    HARNESS_ERROR = "HARNESS_ERROR"
    BENCHMARK_ERROR = "BENCHMARK_ERROR"

    @property
    def failure_owner(self) -> FailureOwner | None:
        return {
            AttemptStatus.SCORED: None,
            AttemptStatus.MODEL_ERROR: FailureOwner.MODEL,
            AttemptStatus.HARNESS_ERROR: FailureOwner.HARNESS,
            AttemptStatus.BENCHMARK_ERROR: FailureOwner.BENCHMARK,
        }[self]


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """Stable scoring and package semantics supplied by one adapter."""

    domain_id: str
    package_kind: str
    score_name: str = "score"
    score_min: float = 0.0
    score_max: float = 1.0
    pass_threshold: float = 1.0
    higher_is_better: bool = True

    def __post_init__(self) -> None:
        for field in ("domain_id", "package_kind", "score_name"):
            _token(getattr(self, field), field)
        lo = _finite(self.score_min, "score_min")
        hi = _finite(self.score_max, "score_max")
        threshold = _finite(self.pass_threshold, "pass_threshold")
        if hi <= lo:
            raise ValueError("score_max must be greater than score_min")
        if not lo <= threshold <= hi:
            raise ValueError("pass_threshold must lie inside the score range")
        if not isinstance(self.higher_is_better, bool):
            raise TypeError("higher_is_better must be bool")
        object.__setattr__(self, "score_min", lo)
        object.__setattr__(self, "score_max", hi)
        object.__setattr__(self, "pass_threshold", threshold)

    def normalize_score(self, raw_score: float) -> float:
        """Map a valid raw score to [0, 1], where larger is always better."""
        score = _finite(raw_score, "raw_score")
        if not self.score_min <= score <= self.score_max:
            raise ValueError("raw_score is outside the declared score range")
        normalized = (score - self.score_min) / (self.score_max - self.score_min)
        return normalized if self.higher_is_better else 1.0 - normalized

    @property
    def normalized_pass_threshold(self) -> float:
        return self.normalize_score(self.pass_threshold)


@dataclass(frozen=True, slots=True)
class PackageRef:
    """Content-addressed package identity; ``locator`` remains adapter-private."""

    domain_id: str
    version_id: str
    locator: str
    content_digest: str

    def __post_init__(self) -> None:
        for field in ("domain_id", "version_id", "locator", "content_digest"):
            _token(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class EvalAttempt:
    """One scheduled item/trial cell, including explicit infrastructure errors."""

    item_id: ItemId
    trial_id: TrialId
    status: AttemptStatus
    evidence_ref: str
    score: float | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        _token(self.item_id, "item_id")
        _token(self.trial_id, "trial_id")
        _token(self.evidence_ref, "evidence_ref")
        if not isinstance(self.status, AttemptStatus):
            raise TypeError("status must be AttemptStatus")
        if not isinstance(self.detail, str):
            raise TypeError("detail must be str")
        if self.status is AttemptStatus.SCORED:
            if self.score is None:
                raise ValueError("a SCORED attempt requires score")
            object.__setattr__(self, "score", _finite(self.score, "score"))
        else:
            if self.score is not None:
                raise ValueError("an error attempt must not carry a score")
            if not self.detail.strip():
                raise ValueError("an error attempt requires detail")


@dataclass(frozen=True, slots=True)
class EvalBatch:
    """An exact, normalized evaluation grid for one immutable package."""

    spec: DomainSpec
    package: PackageRef
    split_id: str
    expected_item_ids: tuple[ItemId, ...]
    trial_ids: tuple[TrialId, ...]
    attempts: tuple[EvalAttempt, ...]
    artifact_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.spec, DomainSpec):
            raise TypeError("spec must be DomainSpec")
        if not isinstance(self.package, PackageRef):
            raise TypeError("package must be PackageRef")
        if self.package.domain_id != self.spec.domain_id:
            raise ValueError("package and spec domain_id differ")
        _token(self.split_id, "split_id")
        _token(self.artifact_ref, "artifact_ref")
        items = _tokens(self.expected_item_ids, "expected_item_ids")
        trials = _tokens(self.trial_ids, "trial_ids")
        attempts = tuple(self.attempts)
        if not attempts:
            raise ValueError("attempts must not be empty")
        if not all(isinstance(attempt, EvalAttempt) for attempt in attempts):
            raise TypeError("attempts must contain EvalAttempt records")

        expected = {(item, trial) for item in items for trial in trials}
        seen: set[tuple[str, str]] = set()
        for attempt in attempts:
            key = (attempt.item_id, attempt.trial_id)
            if key in seen:
                raise ValueError(f"duplicate evaluation cell: {key!r}")
            seen.add(key)
            if attempt.status is AttemptStatus.SCORED:
                self.spec.normalize_score(attempt.score)  # range + finiteness check
        missing = expected - seen
        extra = seen - expected
        if missing or extra:
            raise ValueError(
                "evaluation coverage must equal expected_item_ids x trial_ids; "
                f"missing={sorted(missing)!r}, extra={sorted(extra)!r}"
            )
        object.__setattr__(self, "expected_item_ids", tuple(ItemId(item) for item in items))
        object.__setattr__(self, "trial_ids", tuple(TrialId(trial) for trial in trials))
        object.__setattr__(self, "attempts", attempts)

    @property
    def coverage_pairs(self) -> frozenset[tuple[ItemId, TrialId]]:
        return frozenset((attempt.item_id, attempt.trial_id) for attempt in self.attempts)

    @property
    def gateable(self) -> bool:
        return all(attempt.status is AttemptStatus.SCORED for attempt in self.attempts)

    def normalized_matrix(self) -> Mapping[ItemId, tuple[float, ...]]:
        """Return item rows in declared trial order, rejecting partial/error runs."""
        if not self.gateable:
            owners = sorted({attempt.status.value for attempt in self.attempts
                             if attempt.status is not AttemptStatus.SCORED})
            raise ValueError(f"evaluation batch contains unscored attempts: {owners}")
        lookup = {(attempt.item_id, attempt.trial_id): attempt for attempt in self.attempts}
        rows = {
            item: tuple(self.spec.normalize_score(lookup[(item, trial)].score)
                        for trial in self.trial_ids)
            for item in self.expected_item_ids
        }
        return MappingProxyType(rows)

    def mean_scores(self) -> Mapping[ItemId, float]:
        rows = self.normalized_matrix()
        return MappingProxyType({item: sum(row) / len(row) for item, row in rows.items()})


@dataclass(frozen=True, slots=True)
class ActivationReceipt:
    """Non-model evidence that one applied patch reached a runtime evaluation."""

    receipt_id: str
    domain_id: str
    package_version: str
    package_digest: str
    patch_id: str
    item_id: ItemId
    trial_id: TrialId
    activation_point: str
    evidence_ref: str
    observer: FailureOwner = FailureOwner.HARNESS

    def __post_init__(self) -> None:
        for field in ("receipt_id", "domain_id", "package_version", "package_digest",
                      "patch_id", "item_id", "trial_id", "activation_point", "evidence_ref"):
            _token(getattr(self, field), field)
        if self.observer not in {FailureOwner.HARNESS, FailureOwner.BENCHMARK}:
            raise ValueError("activation must be observed by HARNESS or BENCHMARK, not self-reported")


@dataclass(frozen=True, slots=True)
class OpportunityEvidence:
    """Non-model evidence for O at one exact item/trial cell.

    ``UNKNOWN`` is deliberately first-class: an adapter must not turn an
    ambiguous trajectory into a positive patch opportunity.  The record may
    name several required actions when the benchmark specifies a compound
    step; opaque action identifiers are never interpreted by the core loop.
    """

    item_id: ItemId
    trial_id: TrialId
    status: OpportunityStatus
    summary: str
    evidence_refs: tuple[str, ...]
    required_action_ids: tuple[str, ...] = ()
    observer: FailureOwner = FailureOwner.HARNESS

    def __post_init__(self) -> None:
        _token(self.item_id, "item_id")
        _token(self.trial_id, "trial_id")
        if not isinstance(self.status, OpportunityStatus):
            raise TypeError("status must be OpportunityStatus")
        _token(self.summary, "summary")
        evidence = _tokens(self.evidence_refs, "evidence_refs")
        actions = _tokens(
            self.required_action_ids, "required_action_ids", allow_empty=True,
        )
        if self.observer not in {FailureOwner.HARNESS, FailureOwner.BENCHMARK}:
            raise ValueError(
                "opportunity must be observed by HARNESS or BENCHMARK, not self-reported"
            )
        object.__setattr__(self, "item_id", ItemId(self.item_id))
        object.__setattr__(self, "trial_id", TrialId(self.trial_id))
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "required_action_ids", actions)


@dataclass(frozen=True, slots=True)
class FailureAttribution:
    """Evidence-backed ownership for one failure cluster."""

    attribution_id: str
    item_ids: tuple[ItemId, ...]
    owner: FailureOwner
    summary: str
    evidence_refs: tuple[str, ...]
    trial_ids: tuple[TrialId, ...] = ()
    patch_target_id: str | None = None

    def __post_init__(self) -> None:
        _token(self.attribution_id, "attribution_id")
        items = _tokens(self.item_ids, "item_ids")
        trials = _tokens(self.trial_ids, "trial_ids", allow_empty=True)
        _token(self.summary, "summary")
        evidence = _tokens(self.evidence_refs, "evidence_refs")
        if not isinstance(self.owner, FailureOwner):
            raise TypeError("owner must be FailureOwner")
        if self.patch_target_id is not None:
            _token(self.patch_target_id, "patch_target_id")
            if not self.owner.memory_patchable:
                raise ValueError("MODEL/HARNESS/BENCHMARK failures cannot name a memory patch target")
        object.__setattr__(self, "item_ids", tuple(ItemId(item) for item in items))
        object.__setattr__(self, "trial_ids", tuple(TrialId(trial) for trial in trials))
        object.__setattr__(self, "evidence_refs", evidence)

    @property
    def patchability(self) -> PatchabilityStatus:
        return (
            PatchabilityStatus.MEMORY
            if self.owner.memory_patchable
            else PatchabilityStatus.EXTERNAL
        )


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Adapter-produced evidence plus its disclosure/sanitization state."""

    batch: EvalBatch
    opportunities: tuple[OpportunityEvidence, ...]
    attributions: tuple[FailureAttribution, ...] = ()
    activation_receipts: tuple[ActivationReceipt, ...] = ()
    sanitized: bool = False
    sanitization_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.batch, EvalBatch):
            raise TypeError("batch must be EvalBatch")
        opportunities = tuple(self.opportunities)
        attributions = tuple(self.attributions)
        receipts = tuple(self.activation_receipts)
        if not all(isinstance(value, OpportunityEvidence) for value in opportunities):
            raise TypeError("opportunities must contain OpportunityEvidence records")
        if not all(isinstance(value, FailureAttribution) for value in attributions):
            raise TypeError("attributions must contain FailureAttribution records")
        if not all(isinstance(value, ActivationReceipt) for value in receipts):
            raise TypeError("activation_receipts must contain ActivationReceipt records")
        if bool(self.sanitization_ref) != self.sanitized:
            raise ValueError("sanitized and sanitization_ref must be set together")
        if self.sanitization_ref is not None:
            _token(self.sanitization_ref, "sanitization_ref")

        item_set = set(self.batch.expected_item_ids)
        trial_set = set(self.batch.trial_ids)
        opportunity_keys = [(value.item_id, value.trial_id) for value in opportunities]
        if len(opportunity_keys) != len(set(opportunity_keys)):
            raise ValueError("duplicate opportunity evidence for one evaluation cell")
        opportunity_set = set(opportunity_keys)
        if opportunity_set != set(self.batch.coverage_pairs):
            missing = set(self.batch.coverage_pairs) - opportunity_set
            extra = opportunity_set - set(self.batch.coverage_pairs)
            raise ValueError(
                "opportunity coverage must equal the evaluation grid; "
                f"missing={sorted(missing)!r}, extra={sorted(extra)!r}"
            )
        attribution_ids: set[str] = set()
        attributed_patchability: dict[tuple[ItemId, TrialId], PatchabilityStatus] = {}
        for attribution in attributions:
            if attribution.attribution_id in attribution_ids:
                raise ValueError("duplicate attribution_id")
            attribution_ids.add(attribution.attribution_id)
            if not set(attribution.item_ids) <= item_set:
                raise ValueError("attribution refers to an item outside the evaluation batch")
            if attribution.trial_ids and not set(attribution.trial_ids) <= trial_set:
                raise ValueError("attribution refers to a trial outside the evaluation batch")
            covered_trials = attribution.trial_ids or self.batch.trial_ids
            for item in attribution.item_ids:
                for trial in covered_trials:
                    key = (item, trial)
                    previous = attributed_patchability.get(key)
                    if previous is not None and previous is not attribution.patchability:
                        raise ValueError(
                            "conflicting memory/external attributions for one evaluation cell"
                        )
                    attributed_patchability[key] = attribution.patchability

        receipt_ids: set[str] = set()
        receipt_keys: set[tuple[str, str, str, str]] = set()
        for receipt in receipts:
            if receipt.receipt_id in receipt_ids:
                raise ValueError("duplicate activation receipt_id")
            receipt_ids.add(receipt.receipt_id)
            key = (receipt.patch_id, receipt.item_id, receipt.trial_id, receipt.activation_point)
            if key in receipt_keys:
                raise ValueError("duplicate activation receipt for the same evaluation cell")
            receipt_keys.add(key)
            if receipt.domain_id != self.batch.spec.domain_id:
                raise ValueError("activation receipt domain differs from evaluation batch")
            if (receipt.package_version != self.batch.package.version_id or
                    receipt.package_digest != self.batch.package.content_digest):
                raise ValueError("activation receipt is not bound to the evaluated package bytes")
            if (receipt.item_id, receipt.trial_id) not in self.batch.coverage_pairs:
                raise ValueError("activation receipt refers to an unevaluated cell")
        object.__setattr__(self, "opportunities", opportunities)
        object.__setattr__(self, "attributions", attributions)
        object.__setattr__(self, "activation_receipts", receipts)

    def require_sanitized(self) -> None:
        if not self.sanitized:
            raise ValueError("evidence must be sanitized before it is disclosed to the Meta-Agent")

    def opportunity_for(self, item_id: ItemId, trial_id: TrialId) -> OpportunityEvidence:
        for record in self.opportunities:
            if record.item_id == item_id and record.trial_id == trial_id:
                return record
        raise KeyError((item_id, trial_id))

    def patchability_for(
        self, item_id: ItemId, trial_id: TrialId,
    ) -> PatchabilityStatus:
        statuses = {
            attribution.patchability
            for attribution in self.attributions
            if item_id in attribution.item_ids
            and (not attribution.trial_ids or trial_id in attribution.trial_ids)
        }
        if not statuses:
            return PatchabilityStatus.UNKNOWN
        if len(statuses) != 1:
            raise ValueError("conflicting patchability attributions")
        return next(iter(statuses))

    @property
    def patch_eligible_cells(self) -> frozenset[tuple[ItemId, TrialId]]:
        """Cells for which both O=present and P=memory are evidenced."""
        return frozenset(
            (record.item_id, record.trial_id)
            for record in self.opportunities
            if record.status is OpportunityStatus.PRESENT
            and self.patchability_for(record.item_id, record.trial_id)
            is PatchabilityStatus.MEMORY
        )


@dataclass(frozen=True, slots=True)
class PatchTarget:
    """One adapter-supported mutation surface and its legal operations."""

    target_id: str
    owner: FailureOwner
    operations: tuple[str, ...]
    description: str
    related_targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _token(self.target_id, "target_id")
        if not isinstance(self.owner, FailureOwner) or not self.owner.memory_patchable:
            raise ValueError("a patch target owner must be one of E/W/RHO/C")
        operations = _tokens(self.operations, "operations")
        _token(self.description, "description")
        related = _tokens(self.related_targets, "related_targets", allow_empty=True)
        if self.target_id in related:
            raise ValueError("a patch target cannot relate to itself")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "related_targets", related)


@dataclass(frozen=True, slots=True)
class PatchOperation:
    target_id: str
    operation: str
    payload_ref: str

    def __post_init__(self) -> None:
        for field in ("target_id", "operation", "payload_ref"):
            _token(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class PatchSet:
    """A minimal connected patch rooted at one primary target."""

    patch_id: str
    primary_target_id: str
    operations: tuple[PatchOperation, ...]

    def __post_init__(self) -> None:
        _token(self.patch_id, "patch_id")
        _token(self.primary_target_id, "primary_target_id")
        operations = tuple(self.operations)
        if not operations or not all(isinstance(op, PatchOperation) for op in operations):
            raise ValueError("operations must contain at least one PatchOperation")
        keys = {(op.target_id, op.operation) for op in operations}
        if len(keys) != len(operations):
            raise ValueError("duplicate patch operation")
        if self.primary_target_id not in {op.target_id for op in operations}:
            raise ValueError("primary_target_id must be changed by the patch")
        object.__setattr__(self, "operations", operations)


@dataclass(frozen=True, slots=True)
class PatchCatalog:
    """Domain-owned mutation vocabulary, including cross-component adjacency."""

    domain_id: str
    targets: tuple[PatchTarget, ...]
    schema_version: str = "1"

    def __post_init__(self) -> None:
        _token(self.domain_id, "domain_id")
        _token(self.schema_version, "schema_version")
        targets = tuple(self.targets)
        if not targets or not all(isinstance(target, PatchTarget) for target in targets):
            raise ValueError("targets must contain at least one PatchTarget")
        target_ids = [target.target_id for target in targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("duplicate patch target_id")
        known = set(target_ids)
        for target in targets:
            unknown = set(target.related_targets) - known
            if unknown:
                raise ValueError(f"patch target {target.target_id!r} has unknown relations: {unknown!r}")
        object.__setattr__(self, "targets", targets)

    def target(self, target_id: str) -> PatchTarget:
        _token(target_id, "target_id")
        for target in self.targets:
            if target.target_id == target_id:
                return target
        raise KeyError(target_id)

    def validate_patch(self, patch: PatchSet) -> None:
        """Reject illegal operations and disconnected multi-component edits."""
        if not isinstance(patch, PatchSet):
            raise TypeError("patch must be PatchSet")
        selected = {operation.target_id for operation in patch.operations}
        for operation in patch.operations:
            target = self.target(operation.target_id)
            if operation.operation not in target.operations:
                raise ValueError(
                    f"operation {operation.operation!r} is illegal for {operation.target_id!r}"
                )

        # Relations are interpreted as undirected compatibility edges.  Every
        # edited target must be reachable from the declared primary target.
        reached = {patch.primary_target_id}
        while True:
            previous = set(reached)
            for target_id in tuple(reached):
                target = self.target(target_id)
                reached.update(set(target.related_targets) & selected)
                reached.update(
                    candidate.target_id for candidate in self.targets
                    if target_id in candidate.related_targets and candidate.target_id in selected
                )
            if reached == previous:
                break
        if reached != selected:
            raise ValueError(f"patch targets are not connected to the primary target: {selected - reached!r}")


@dataclass(frozen=True, slots=True)
class RefereeCheck:
    check_id: str
    passed: bool
    detail: str
    evidence_ref: str
    failure_owner: FailureOwner | None = None

    def __post_init__(self) -> None:
        for field in ("check_id", "detail", "evidence_ref"):
            _token(getattr(self, field), field)
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be bool")
        if self.passed and self.failure_owner is not None:
            raise ValueError("a passing referee check cannot have a failure_owner")
        if not self.passed and not isinstance(self.failure_owner, FailureOwner):
            raise ValueError("a failing referee check requires a FailureOwner")


@dataclass(frozen=True, slots=True)
class RefereeReport:
    """Adapter referee output (package shape, lint, leakage, and safety checks)."""

    report_id: str
    domain_id: str
    package: PackageRef
    checks: tuple[RefereeCheck, ...]
    patch_id: str | None = None

    def __post_init__(self) -> None:
        _token(self.report_id, "report_id")
        _token(self.domain_id, "domain_id")
        if not isinstance(self.package, PackageRef):
            raise TypeError("package must be PackageRef")
        if self.package.domain_id != self.domain_id:
            raise ValueError("package and referee report domain_id differ")
        if self.patch_id is not None:
            _token(self.patch_id, "patch_id")
        checks = tuple(self.checks)
        if not checks or not all(isinstance(check, RefereeCheck) for check in checks):
            raise ValueError("checks must contain at least one RefereeCheck")
        ids = [check.check_id for check in checks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate referee check_id")
        object.__setattr__(self, "checks", checks)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


@dataclass(frozen=True, slots=True)
class GateSpec:
    """Adapter-selected paired gate units and admission policy."""

    gate_id: str
    domain_id: str
    repair_item_ids: tuple[ItemId, ...]
    heldout_item_ids: tuple[ItemId, ...]
    trial_ids: tuple[TrialId, ...]
    min_repair_gain: float = 0.0
    min_heldout_gain: float = 0.0
    regression_tolerance: float = 0.0
    max_regressions: int = 0
    confidence_level: float = 0.95
    bootstrap_samples: int = 10_000
    require_referee_pass: bool = True
    require_activation_receipt: bool = True

    def __post_init__(self) -> None:
        _token(self.gate_id, "gate_id")
        _token(self.domain_id, "domain_id")
        repair = _tokens(self.repair_item_ids, "repair_item_ids", allow_empty=True)
        heldout = _tokens(self.heldout_item_ids, "heldout_item_ids")
        trials = _tokens(self.trial_ids, "trial_ids")
        overlap = set(repair) & set(heldout)
        if overlap:
            raise ValueError(f"repair and held-out units must be disjoint: {overlap!r}")
        for field in ("min_repair_gain", "min_heldout_gain", "regression_tolerance"):
            value = _finite(getattr(self, field), field)
            if value < 0:
                raise ValueError(f"{field} must be non-negative")
            object.__setattr__(self, field, value)
        if isinstance(self.max_regressions, bool) or self.max_regressions < 0:
            raise ValueError("max_regressions must be a non-negative integer")
        if not isinstance(self.max_regressions, int):
            raise TypeError("max_regressions must be int")
        confidence = _finite(self.confidence_level, "confidence_level")
        if not 0.0 < confidence < 1.0:
            raise ValueError("confidence_level must lie strictly between 0 and 1")
        if isinstance(self.bootstrap_samples, bool) or not isinstance(self.bootstrap_samples, int):
            raise TypeError("bootstrap_samples must be int")
        if self.bootstrap_samples <= 0:
            raise ValueError("bootstrap_samples must be positive")
        if not isinstance(self.require_referee_pass, bool) or not isinstance(
                self.require_activation_receipt, bool):
            raise TypeError("gate requirements must be bool")
        object.__setattr__(self, "repair_item_ids", tuple(ItemId(item) for item in repair))
        object.__setattr__(self, "heldout_item_ids", tuple(ItemId(item) for item in heldout))
        object.__setattr__(self, "trial_ids", tuple(TrialId(trial) for trial in trials))
        object.__setattr__(self, "confidence_level", confidence)


@runtime_checkable
class DomainAdapter(Protocol):
    """Facade implemented once per downstream runtime/domain family.

    The deterministic loop owns versioning, transactions, and accept/revert.
    An adapter owns domain package materialization, evidence disclosure,
    evaluation, referee checks (including lint/leak checks), activation proof,
    and the paired-gate units/policy.
    """

    @property
    def spec(self) -> DomainSpec: ...

    @property
    def patch_catalog(self) -> PatchCatalog: ...

    def materialize(
        self, *, source: PackageRef | None, destination_ref: str
    ) -> PackageRef: ...

    def evaluate(
        self,
        *,
        package: PackageRef,
        item_ids: Sequence[ItemId],
        trial_ids: Sequence[TrialId],
        artifact_namespace: str,
    ) -> EvalBatch: ...

    def collect_evidence(self, batch: EvalBatch) -> EvidenceBundle: ...

    def sanitize_evidence(self, evidence: EvidenceBundle) -> EvidenceBundle: ...

    def referee(self, *, package: PackageRef, patch: PatchSet | None) -> RefereeReport: ...

    def collect_activation_receipts(
        self, *, patch: PatchSet, batch: EvalBatch
    ) -> tuple[ActivationReceipt, ...]: ...

    def make_gate_spec(
        self,
        *,
        repair_item_ids: Sequence[ItemId],
        heldout_item_ids: Sequence[ItemId],
        trial_ids: Sequence[TrialId],
    ) -> GateSpec: ...


__all__ = [
    "ActivationReceipt",
    "AttemptStatus",
    "DomainAdapter",
    "DomainSpec",
    "EvalAttempt",
    "EvalBatch",
    "EvidenceBundle",
    "FailureAttribution",
    "FailureOwner",
    "GateSpec",
    "ItemId",
    "OpportunityEvidence",
    "OpportunityStatus",
    "PackageRef",
    "PatchCatalog",
    "PatchOperation",
    "PatchSet",
    "PatchTarget",
    "PatchabilityStatus",
    "RefereeCheck",
    "RefereeReport",
    "TrialId",
]
