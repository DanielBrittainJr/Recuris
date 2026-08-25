"""Filesystem locations, resolved from the environment with explicit errors.

Nothing in this package embeds an absolute path. Every location a run needs is
resolved here, in one of three ways, in order:

1. an explicit argument passed by the caller;
2. an environment variable (documented in the README);
3. a default relative to the repository root.

A location that cannot be resolved raises :class:`PathError` naming both the
variable to set and the setup command that creates the directory. Failing at
startup with a remedy is the point; a run that silently picks the wrong data
directory produces numbers nobody can trace.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathError(RuntimeError):
    """A required location is missing or not configured."""


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name)
    return Path(raw).expanduser().resolve() if raw else None


MARKERS = ("skill_memories", "splits", "third_party")


def repo_root() -> Path:
    """The checkout: the directory holding ``skill_memories/`` and ``splits/``.

    This repository is meant to be used from a checkout, not from a wheel. The
    Skill Memory packages, the frozen splits and the integrity anchors are data
    the code reads at run time, and they are not installed alongside the
    module. So resolving upward from ``__file__`` is only right for an editable
    install; from ``site-packages`` it lands somewhere arbitrary, and the
    failure then shows up much later as a missing package rather than as a
    missing checkout.

    Resolution order: ``RECURIS_REPO_ROOT`` if set; the directory two levels
    above this file if it looks like the checkout; then upward from the working
    directory. Failing that, raise -- an arbitrary directory silently treated
    as the repository root is how a run ends up scoring against the wrong data.
    """
    override = _env_path("RECURIS_REPO_ROOT")
    if override is not None:
        return override

    candidates = [Path(__file__).resolve().parents[2], *Path.cwd().resolve().parents]
    candidates.insert(1, Path.cwd().resolve())
    for candidate in candidates:
        if all((candidate / marker).is_dir() for marker in MARKERS):
            return candidate

    raise PathError(
        "cannot locate the Recuris checkout: no directory above "
        f"{Path(__file__).resolve().parents[2]} or {Path.cwd()} contains "
        f"{', '.join(MARKERS)}. Run from a clone of the repository, or set "
        "RECURIS_REPO_ROOT. A wheel install does not carry the Skill Memory "
        "packages, the splits or the integrity anchors."
    )


def workspace_root() -> Path:
    """Where a campaign writes run directories, candidate packages and caches.

    Override with ``RECURIS_WORKSPACE``. Defaults to the repository root, which
    is what a single-checkout run wants; point it elsewhere to keep a large
    campaign's artefacts off the repository volume.
    """
    return _env_path("RECURIS_WORKSPACE") or repo_root()


def path_prefix(target: Path, base: Path) -> str:
    """Render ``target`` the way a tool-permission rule should refer to it.

    Permission rules are evaluated relative to the working directory, so a
    path inside the workspace is written relative and anything outside it is
    written absolute. Both forms are POSIX-separated, which is what the coding
    agent's matcher expects on every platform.
    """
    target, base = Path(target).resolve(), Path(base).resolve()
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return target.as_posix()


def external_root() -> Path:
    """Where ``third_party/*/setup.sh`` places third-party checkouts.

    Override with ``RECURIS_EXTERNAL_ROOT``. Defaults to ``external/`` at the
    repository root, which is git-ignored.
    """
    return _env_path("RECURIS_EXTERNAL_ROOT") or (repo_root() / "external")


def tau2_root(*, required: bool = True) -> Path:
    """The tau2-Bench checkout.

    Override with ``TAU2_ROOT``. Defaults to ``external/tau2-bench``.

    tau2-Bench records the benchmark revision by running ``git rev-parse HEAD``
    in the process working directory, so this must be a real git checkout and
    not an unpacked archive. ``third_party/tau2/setup.sh`` produces one.
    """
    root = _env_path("TAU2_ROOT") or (external_root() / "tau2-bench")
    if required:
        if not root.exists():
            raise PathError(
                f"tau2-Bench checkout not found at {root}. "
                "Run `bash third_party/tau2/setup.sh`, or set TAU2_ROOT."
            )
        if not (root / ".git").exists():
            raise PathError(
                f"{root} is not a git checkout. tau2-Bench reads the benchmark "
                "revision with `git rev-parse HEAD`, so an unpacked archive "
                "cannot be used. Re-run `bash third_party/tau2/setup.sh`."
            )
    return root


def tau2_data_dir(*, required: bool = True) -> Path:
    """The tau2 domain-data directory (domains, tasks, and run output).

    Override with ``TAU2_DATA_DIR``. Defaults to ``<tau2_root>/data``.
    """
    d = _env_path("TAU2_DATA_DIR") or (tau2_root(required=required) / "data")
    if required and not d.exists():
        raise PathError(
            f"tau2 data directory not found at {d}. "
            "Run `bash third_party/tau2/setup.sh`, or set TAU2_DATA_DIR."
        )
    return d


def skill_memory_root() -> Path:
    """Directory holding the Skill Memory packages shipped with this repo.

    Override with ``RECURIS_SKILL_MEMORY_ROOT``.
    """
    return _env_path("RECURIS_SKILL_MEMORY_ROOT") or (repo_root() / "skill_memories")


def resolve_skill_memory(spec: str) -> Path:
    """Resolve a Skill Memory package given either a path or a bare name.

    ``retail`` and ``skill_memories/tau2_retail`` both work, so the
    quickstart commands stay short without hiding where the package came from.
    """
    p = Path(spec).expanduser()
    if p.exists():
        return p.resolve()
    candidate = skill_memory_root() / spec
    if candidate.exists():
        return candidate.resolve()
    available = sorted(
        c.name for c in skill_memory_root().glob("*") if (c / "manifest.yaml").exists()
    )
    raise PathError(
        f"Skill Memory package {spec!r} not found (looked at {p} and {candidate}). "
        f"Available packages: {', '.join(available) or '(none)'}"
    )


def splits_root() -> Path:
    """Directory holding the frozen evaluation splits."""
    return _env_path("RECURIS_SPLITS_ROOT") or (repo_root() / "splits")


def load_dotenv() -> list[str]:
    """Load the repository-root ``.env`` into the environment.

    The README tells the reader to put their endpoint in a ``.env`` file, so
    every entry point has to honour it. tau2 used to be the only one that did,
    and only by accident: the benchmark loads ``.env`` itself, so the tau2 arm
    worked while the Terminal-Bench driver exited saying OPENAI_BASE_URL was
    unset with the file sitting right there.

    Anything already set in the environment wins, so an explicit export still
    overrides the file. Returns the names loaded, never the values.
    """
    try:
        env_file = repo_root() / ".env"
    except PathError:
        return []
    if not env_file.is_file():
        return []
    loaded: list[str] = []
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or name in os.environ:
            continue
        os.environ[name] = value.strip().strip('"').strip("'")
        loaded.append(name)
    return loaded


__all__ = [
    "PathError",
    "load_dotenv",
    "path_prefix",
    "workspace_root",
    "external_root",
    "repo_root",
    "resolve_skill_memory",
    "skill_memory_root",
    "splits_root",
    "tau2_data_dir",
    "tau2_root",
]
