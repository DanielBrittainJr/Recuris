"""Tau2 implementation of the domain-neutral Meta-Agent adapter contract.

The existing Tau2 driver remains the reference implementation.  This adapter
only translates its immutable evaluation artifacts into domain-neutral records;
it does not change the Recuris runtime, Tau2 benchmark, or admission policy.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

from recuris.metaagent.domain_adapter import (
    ActivationReceipt,
    AttemptStatus,
    DomainSpec,
    EvalAttempt,
    EvalBatch,
    EvidenceBundle,
    FailureAttribution,
    FailureOwner,
    GateSpec,
    ItemId,
    OpportunityEvidence,
    OpportunityStatus,
    PackageRef,
    PatchCatalog,
    PatchSet,
    PatchTarget,
    RefereeCheck,
    RefereeReport,
    TrialId,
)
from recuris.metaagent.downstream import RecurisDownstream, Results, _package_tree_sha256

ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_DOMAINS = frozenset({"retail", "airline", "banking_knowledge"})
BARE_LOCATOR_PREFIX = "tau2-bare://"


def _default_referee(package: Path, domain: str) -> tuple[bool, str]:
    command = [
        sys.executable,
        str(ROOT / "metaagent" / "ma_lint.py"),
        "--pkg",
        str(package),
        "--domain",
        domain,
    ]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    detail = (completed.stdout + "\n" + completed.stderr).strip()
    return completed.returncode == 0, detail or "ma_lint produced no output"


class Tau2DomainAdapter:
    """Translate Retail/Airline runs into the generic evolution contract."""

    def __init__(
        self,
        domain: str,
        *,
        downstream_factory: Callable[[str], RecurisDownstream] | None = None,
        referee_runner: Callable[[Path, str], tuple[bool, str]] | None = None,
    ) -> None:
        if domain not in SUPPORTED_DOMAINS:
            raise ValueError(
                f"Tau2DomainAdapter supports only {sorted(SUPPORTED_DOMAINS)}"
            )
        self.domain = domain
        self._downstream_factory = downstream_factory or (
            lambda namespace: RecurisDownstream(
                domain=domain,
                artifact_namespace=namespace,
            )
        )
        self._referee_runner = referee_runner or _default_referee
        self._results: dict[str, Results] = {}

        self._spec = DomainSpec(
            domain_id=f"tau2:{domain}",
            package_kind="recuris-skill-memory",
            score_name="task_success",
            score_min=0.0,
            score_max=1.0,
            pass_threshold=1.0,
        )
        self._patch_catalog = PatchCatalog(
            domain_id=self._spec.domain_id,
            targets=(
                PatchTarget(
                    target_id="em.cards",
                    owner=FailureOwner.E,
                    operations=("add", "revise", "merge", "deprecate"),
                    description="Markdown knowledge, procedure, and action-result cards",
                    related_targets=("rho.delivery",),
                ),
                PatchTarget(
                    target_id="wm.state",
                    owner=FailureOwner.W,
                    operations=("configure",),
                    description="Working-memory entry kind, manager, and schema",
                    related_targets=("rho.delivery", "c.checkers"),
                ),
                PatchTarget(
                    target_id="rho.delivery",
                    owner=FailureOwner.RHO,
                    operations=("configure",),
                    description="Skill delivery bindings and retrieval configuration",
                    related_targets=("em.cards", "wm.state"),
                ),
                PatchTarget(
                    target_id="c.checkers",
                    owner=FailureOwner.C,
                    operations=("configure",),
                    description="Completion checkers, gate policy, and status board",
                    related_targets=("wm.state",),
                ),
            ),
        )

    @property
    def spec(self) -> DomainSpec:
        return self._spec

    @property
    def patch_catalog(self) -> PatchCatalog:
        return self._patch_catalog

    def bare_package(self) -> PackageRef:
        """Return the no-memory baseline treatment used before M0 exists."""
        return PackageRef(
            domain_id=self.spec.domain_id,
            version_id="M-1",
            locator=f"{BARE_LOCATOR_PREFIX}{self.domain}",
            content_digest="bare-no-memory",
        )

    def materialize(
        self, *, source: PackageRef | None, destination_ref: str
    ) -> PackageRef:
        destination = Path(destination_ref).resolve()
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite package: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        if source is None:
            destination.mkdir()
            (destination / "em").mkdir()
            manifest = (
                f"name: tau2_{self.domain}_neutral\n\n"
                "wm:\n"
                "  entry_kind: generic_item\n"
                "  manager: self_maintain_each_turn\n"
                "  schema:\n"
                "    max_entries: 8\n\n"
                "grounding:\n"
                "  matcher: delivery_receipt\n\n"
                "delivery: []\n"
                "checkers: []\n\n"
                "board:\n"
                "  status_board: false\n"
                "  notice_in_wm: never\n"
            )
            (destination / "manifest.yaml").write_text(manifest, encoding="utf-8")
        else:
            if source.domain_id != self.spec.domain_id:
                raise ValueError("source package belongs to a different domain")
            if source.locator.startswith(BARE_LOCATOR_PREFIX):
                raise ValueError("the bare treatment cannot be copied as a Skill Memory")
            source_path = Path(source.locator).resolve()
            if not source_path.is_dir():
                raise FileNotFoundError(f"source package is missing: {source_path}")
            if _package_tree_sha256(source_path) != source.content_digest:
                raise ValueError("source package bytes do not match PackageRef")
            shutil.copytree(source_path, destination)

        return PackageRef(
            domain_id=self.spec.domain_id,
            version_id=destination.name,
            locator=str(destination),
            content_digest=_package_tree_sha256(destination),
        )

    def evaluate(
        self,
        *,
        package: PackageRef,
        item_ids: Sequence[ItemId],
        trial_ids: Sequence[TrialId],
        artifact_namespace: str,
    ) -> EvalBatch:
        if package.domain_id != self.spec.domain_id:
            raise ValueError("package belongs to a different domain")
        items = tuple(ItemId(str(value)) for value in item_ids)
        trials = tuple(TrialId(str(value)) for value in trial_ids)
        if not items or len(items) != len(set(items)):
            raise ValueError("item_ids must be non-empty and unique")
        if not trials or len(trials) != len(set(trials)):
            raise ValueError("trial_ids must be non-empty and unique")

        package_path: str | None
        if package.locator == f"{BARE_LOCATOR_PREFIX}{self.domain}":
            package_path = None
        else:
            path = Path(package.locator).resolve()
            actual_digest = _package_tree_sha256(path)
            if actual_digest != package.content_digest:
                raise ValueError("package bytes changed after PackageRef creation")
            package_path = str(path)

        downstream = self._downstream_factory(artifact_namespace)
        result = downstream.evaluate(
            package_path,
            [str(item) for item in items],
            seeds=len(trials),
            tag=artifact_namespace,
        )
        evaluated_digest = result.artifact.get("package_tree_sha256")
        if package_path is not None and evaluated_digest != package.content_digest:
            raise ValueError("Tau2 result is not bound to the requested package bytes")
        if package_path is None and evaluated_digest is not None:
            raise ValueError("bare Tau2 result unexpectedly reports package bytes")

        artifact_ref = str(result.artifact.get("results") or "").strip()
        if not artifact_ref:
            raise ValueError("Tau2 result has no immutable results artifact")
        attempts: list[EvalAttempt] = []
        for item in items:
            row = result.matrix.get(str(item))
            records = result.trial_evidence.get(str(item))
            if not isinstance(row, list) or len(row) != len(trials):
                raise ValueError(f"Tau2 result has invalid score row for {item}")
            if not isinstance(records, list) or len(records) != len(trials):
                raise ValueError(f"Tau2 result has invalid trial evidence for {item}")
            for index, trial in enumerate(trials):
                record = records[index]
                attempts.append(
                    EvalAttempt(
                        item_id=item,
                        trial_id=trial,
                        status=AttemptStatus.SCORED,
                        score=float(row[index]),
                        evidence_ref=self._cell_ref(artifact_ref, item, trial),
                        detail=(
                            f"tau2_trial={record.get('trial')}; "
                            f"seed={record.get('seed')}"
                        ),
                    )
                )

        batch = EvalBatch(
            spec=self.spec,
            package=package,
            split_id=artifact_namespace,
            expected_item_ids=items,
            trial_ids=trials,
            attempts=tuple(attempts),
            artifact_ref=artifact_ref,
        )
        self._results[artifact_ref] = result
        return batch

    @staticmethod
    def _cell_ref(artifact_ref: str, item: ItemId, trial: TrialId) -> str:
        return f"{artifact_ref}#item={item}&trial={trial}"

    def collect_evidence(self, batch: EvalBatch) -> EvidenceBundle:
        result = self._results.get(batch.artifact_ref)
        if result is None:
            raise ValueError("evaluation artifact was not produced by this adapter")

        opportunities: list[OpportunityEvidence] = []
        attributions: list[FailureAttribution] = []
        for item in batch.expected_item_ids:
            records = result.trial_evidence.get(str(item)) or []
            if len(records) != len(batch.trial_ids):
                raise ValueError(f"Tau2 evidence coverage changed for {item}")
            for index, trial in enumerate(batch.trial_ids):
                record = records[index]
                raw_opportunity = record.get("action_opportunity") or {}
                raw_status = str(raw_opportunity.get("status") or "unknown")
                try:
                    status = OpportunityStatus(raw_status)
                except ValueError as exc:
                    raise ValueError(
                        f"Tau2 emitted invalid opportunity status {raw_status!r}"
                    ) from exc
                evidence_ref = self._cell_ref(batch.artifact_ref, item, trial)
                opportunities.append(
                    OpportunityEvidence(
                        item_id=item,
                        trial_id=trial,
                        status=status,
                        summary=str(
                            raw_opportunity.get("reason")
                            or "Tau2 did not provide an opportunity explanation."
                        ),
                        evidence_refs=(evidence_ref,),
                        required_action_ids=tuple(
                            str(value)
                            for value in raw_opportunity.get("required_actions") or ()
                        ),
                    )
                )

                owner_record = record.get("preclassified_owner")
                if owner_record:
                    owner_name = str(owner_record.get("owner") or "")
                    try:
                        owner = FailureOwner(owner_name)
                    except ValueError as exc:
                        raise ValueError(
                            f"Tau2 emitted invalid failure owner {owner_name!r}"
                        ) from exc
                    attributions.append(
                        FailureAttribution(
                            attribution_id=f"tau2:{item}:{trial}:{owner.value}",
                            item_ids=(item,),
                            trial_ids=(trial,),
                            owner=owner,
                            summary=str(owner_record.get("reason") or owner.value),
                            evidence_refs=(evidence_ref,),
                        )
                    )

        return EvidenceBundle(
            batch=batch,
            opportunities=tuple(opportunities),
            attributions=tuple(attributions),
        )

    def sanitize_evidence(self, evidence: EvidenceBundle) -> EvidenceBundle:
        if evidence.batch.spec.domain_id != self.spec.domain_id:
            raise ValueError("evidence belongs to a different domain")
        # This projection contains only opaque item/trial IDs, action names,
        # deterministic opportunity facts, and artifact references.  Raw
        # transcripts and gold action arguments remain behind the adapter.
        return EvidenceBundle(
            batch=evidence.batch,
            opportunities=evidence.opportunities,
            attributions=evidence.attributions,
            activation_receipts=evidence.activation_receipts,
            sanitized=True,
            sanitization_ref=(
                f"tau2-adapter://{self.domain}/structural-projection-v1/"
                f"{evidence.batch.split_id}"
            ),
        )

    def referee(self, *, package: PackageRef, patch: PatchSet | None) -> RefereeReport:
        if package.domain_id != self.spec.domain_id:
            raise ValueError("package belongs to a different domain")
        if patch is not None:
            self.patch_catalog.validate_patch(patch)
        if package.locator.startswith(BARE_LOCATOR_PREFIX):
            return RefereeReport(
                report_id=f"tau2:{self.domain}:bare-referee",
                domain_id=self.spec.domain_id,
                package=package,
                patch_id=patch.patch_id if patch else None,
                checks=(
                    RefereeCheck(
                        check_id="bare-treatment",
                        passed=True,
                        detail="No-memory Tau2 baseline has no package to lint.",
                        evidence_ref=f"tau2-adapter://{self.domain}/bare",
                    ),
                ),
            )

        package_path = Path(package.locator).resolve()
        actual_digest = _package_tree_sha256(package_path)
        if actual_digest != package.content_digest:
            return RefereeReport(
                report_id=f"tau2:{self.domain}:referee:digest-mismatch",
                domain_id=self.spec.domain_id,
                package=package,
                patch_id=patch.patch_id if patch else None,
                checks=(
                    RefereeCheck(
                        check_id="package-digest",
                        passed=False,
                        detail="Package bytes changed after materialization.",
                        evidence_ref=f"tau2-adapter://{self.domain}/package-digest",
                        failure_owner=FailureOwner.HARNESS,
                    ),
                ),
            )

        passed, detail = self._referee_runner(package_path, self.domain)
        return RefereeReport(
            report_id=f"tau2:{self.domain}:referee:{package.version_id}",
            domain_id=self.spec.domain_id,
            package=package,
            patch_id=patch.patch_id if patch else None,
            checks=(
                RefereeCheck(
                    check_id="package-digest",
                    passed=True,
                    detail="Package digest matches the materialized PackageRef.",
                    evidence_ref=f"tau2-adapter://{self.domain}/package-digest",
                ),
                RefereeCheck(
                    check_id="tau2-package-lint",
                    passed=passed,
                    detail=detail[:2000],
                    evidence_ref=f"tau2-adapter://{self.domain}/ma-lint",
                    failure_owner=None if passed else FailureOwner.HARNESS,
                ),
            ),
        )

    def collect_activation_receipts(
        self, *, patch: PatchSet, batch: EvalBatch
    ) -> tuple[ActivationReceipt, ...]:
        self.patch_catalog.validate_patch(patch)
        if batch.spec.domain_id != self.spec.domain_id:
            raise ValueError("evaluation batch belongs to a different domain")
        # Current Tau2 fingerprints are aggregated per task, not bound to one
        # exact trial and changed artifact.  Returning no receipt is deliberate:
        # the generic gate must fail closed until the runtime emits cell-level,
        # patch-bound activation evidence.
        return ()

    def make_gate_spec(
        self,
        *,
        repair_item_ids: Sequence[ItemId],
        heldout_item_ids: Sequence[ItemId],
        trial_ids: Sequence[TrialId],
    ) -> GateSpec:
        return GateSpec(
            gate_id=f"tau2:{self.domain}:paired-gate-v1",
            domain_id=self.spec.domain_id,
            repair_item_ids=tuple(repair_item_ids),
            heldout_item_ids=tuple(heldout_item_ids),
            trial_ids=tuple(trial_ids),
            min_repair_gain=0.0,
            min_heldout_gain=0.0,
            regression_tolerance=0.0,
            max_regressions=1,
            require_referee_pass=True,
            require_activation_receipt=True,
        )


__all__ = ["Tau2DomainAdapter"]
