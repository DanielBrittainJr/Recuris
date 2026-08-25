"""Versioned benchmark-input protocols for causal Meta-Agent experiments.

The official Tau2 data tree is always read-only.  Alternate protocols are
materialized into a fresh run-scoped directory, content-addressed, and kept
separate in provenance.  They are diagnostic protocols, never official Tau2
scores and never mixed with official arms in one paired gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

OFFICIAL = "official"
COMPLETION_GUARD_V1 = "completion-guard-v1"
SUPPORTED_PROTOCOLS = {OFFICIAL, COMPLETION_GUARD_V1}

_TARGET_BASE_SHA256 = {
    "simulation_guidelines.md":
        "e054adb606a4aa6f586f33f3aa61542954b6f699d029c9cd4c156e28b6300067",
    "simulation_guidelines_tools.md":
        "18edb8d870a9a094f3514531e8da3398ddaa87849c48344841f4714c1c5d0239",
}
_VOICE_BASE_SHA256 = {
    "simulation_guidelines_voice.md":
        "c5b5172defd04c63cf03ceb573df4ed16997cbeb1993d29a46648ab718e6d314",
    "simulation_guidelines_voice_tools.md":
        "a19257bd8e8d929343640232d05064d8c800be87318e1c5826a87663b3a16024",
}
_TARGET_OUTPUT_SHA256 = {
    "simulation_guidelines.md":
        "8ccd89084debcd4d0ca69a33da3aaf682f66a0984818a828e23e45352f544bde",
    "simulation_guidelines_tools.md":
        "e56cb045735757969fab58e74dfe897d10bd793373a947ea1032834e60b732aa",
}
_ANCHOR = (
    "- Disclose information progressively. Wait for the agent to ask for "
    "specific information before providing it."
)
_COMPLETION_RULES = (
    "- **Do not end the conversation prematurely.** Agreeing to an action is not "
    "the same as the action being completed. If the agent offers to do something "
    "(e.g., cancel an order, process a refund), wait for the agent to confirm it "
    "is done before ending the conversation.\n"
    "- **Before ending the conversation, verify that ALL items in your scenario "
    "instructions have been addressed.** If your instructions include multiple "
    "requests, questions, or tasks, make sure every single one has been completed "
    "— do not stop after only some of them are resolved."
)
_RULES_SHA256 = "4671deee17327d083c024a1a8936ddbef423fce72a8720219ca8f1999c58944e"


class BenchmarkProtocolError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_linklike(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _tree_sha256(root: Path) -> str:
    root = Path(root)
    if not root.is_dir() or _is_linklike(root):
        raise BenchmarkProtocolError(f"benchmark tree is missing or linked: {root}")
    rows: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in sorted(os.scandir(current), key=lambda value: value.name):
            path = Path(entry.path)
            if entry.is_symlink() or _is_linklike(path):
                raise BenchmarkProtocolError(f"linked benchmark input is forbidden: {path}")
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                rows.append(
                    f"{path.relative_to(root).as_posix()}:{_sha256_file(path)}"
                )
    if not rows:
        raise BenchmarkProtocolError(f"benchmark tree is empty: {root}")
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


def _tree_manifest(data_root: Path, domain: str) -> dict[str, str]:
    return {
        f"tau2/domains/{domain}": _tree_sha256(
            data_root / "tau2" / "domains" / domain
        ),
        "tau2/user_simulator": _tree_sha256(data_root / "tau2" / "user_simulator"),
    }


def _validate_frozen_sources(source_root: Path) -> None:
    simulator = source_root / "tau2" / "user_simulator"
    for names in (_TARGET_BASE_SHA256, _VOICE_BASE_SHA256):
        for name, expected in names.items():
            path = simulator / name
            actual = _sha256_file(path) if path.is_file() else "missing"
            if actual != expected:
                raise BenchmarkProtocolError(
                    f"completion-guard-v1 base source drifted: {name}={actual}, "
                    f"expected {expected}"
                )
    if hashlib.sha256(_COMPLETION_RULES.encode("utf-8")).hexdigest() != _RULES_SHA256:
        raise BenchmarkProtocolError("completion-guard-v1 rule constant drifted")
    for voice_name in _VOICE_BASE_SHA256:
        voice = (simulator / voice_name).read_text(encoding="utf-8")
        for rule in _COMPLETION_RULES.splitlines():
            if rule not in voice:
                raise BenchmarkProtocolError(
                    f"frozen completion rule is absent from voice source {voice_name}"
                )


def _validate_materialized(
    destination: Path, source_root: Path, domain: str,
) -> dict[str, object]:
    manifest_path = destination / "protocol_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BenchmarkProtocolError(
            f"materialized protocol manifest is missing or invalid: {exc}"
        ) from exc
    source_trees = _tree_manifest(source_root, domain)
    output_trees = _tree_manifest(destination, domain)
    expected_fields = {
        "schema_version": 1,
        "protocol_id": COMPLETION_GUARD_V1,
        "domain": domain,
        "source_trees": source_trees,
        "output_trees": output_trees,
        "target_base_sha256": _TARGET_BASE_SHA256,
        "target_output_sha256": _TARGET_OUTPUT_SHA256,
        "rules_sha256": _RULES_SHA256,
    }
    for key, expected in expected_fields.items():
        if manifest.get(key) != expected:
            raise BenchmarkProtocolError(
                f"materialized completion-guard-v1 failed {key} validation"
            )
    simulator = destination / "tau2" / "user_simulator"
    for name, expected in _TARGET_OUTPUT_SHA256.items():
        if _sha256_file(simulator / name) != expected:
            raise BenchmarkProtocolError(f"materialized guarded file drifted: {name}")
    return manifest


def materialize_completion_guard_v1(
    *, source_root: Path, destination: Path, domain: str,
) -> tuple[Path, dict[str, object]]:
    """Create or validate one run-scoped completion-guard-v1 data root."""
    source_root = Path(source_root).resolve()
    destination = Path(destination).absolute()
    if not domain or domain != domain.strip() or "/" in domain or "\\" in domain:
        raise BenchmarkProtocolError("domain must be one safe path component")
    _validate_frozen_sources(source_root)
    source_trees = _tree_manifest(source_root, domain)

    if destination.exists():
        if _is_linklike(destination):
            raise BenchmarkProtocolError(f"protocol destination is linked: {destination}")
        return destination.resolve(), _validate_materialized(
            destination.resolve(), source_root, domain,
        )

    destination.mkdir(parents=True, exist_ok=False)
    domain_target = destination / "tau2" / "domains" / domain
    simulator_target = destination / "tau2" / "user_simulator"
    domain_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root / "tau2" / "domains" / domain, domain_target)
    shutil.copytree(source_root / "tau2" / "user_simulator", simulator_target)

    for name, expected in _TARGET_OUTPUT_SHA256.items():
        target = simulator_target / name
        text = target.read_text(encoding="utf-8").replace("\r\n", "\n")
        if text.count(_ANCHOR) != 1:
            raise BenchmarkProtocolError(
                f"completion-guard-v1 insertion anchor is not unique in {name}"
            )
        guarded = text.replace(_ANCHOR, _ANCHOR + "\n" + _COMPLETION_RULES)
        target.write_bytes(guarded.encode("utf-8"))
        actual = _sha256_file(target)
        if actual != expected:
            raise BenchmarkProtocolError(
                f"completion-guard-v1 generated unexpected bytes for {name}: {actual}"
            )

    output_trees = _tree_manifest(destination, domain)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "protocol_id": COMPLETION_GUARD_V1,
        "domain": domain,
        "source_root": str(source_root),
        "source_trees": source_trees,
        "output_trees": output_trees,
        "target_base_sha256": _TARGET_BASE_SHA256,
        "target_output_sha256": _TARGET_OUTPUT_SHA256,
        "rules_sha256": _RULES_SHA256,
        "interpretation": (
            "Diagnostic alternate protocol; results are not official Tau2 scores."
        ),
    }
    with (destination / "protocol_manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return destination.resolve(), _validate_materialized(
        destination.resolve(), source_root, domain,
    )


__all__ = [
    "BenchmarkProtocolError",
    "COMPLETION_GUARD_V1",
    "OFFICIAL",
    "SUPPORTED_PROTOCOLS",
    "materialize_completion_guard_v1",
]
