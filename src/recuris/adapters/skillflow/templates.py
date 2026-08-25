"""Template routing for SkillFlow: which prompt template a task family gets.

Two policies ship, and which one is in force must always be stated, because
they answer different questions.

``default``
    Every family gets ``templates/sf-<family-slug>.j2`` if that file exists,
    and ``templates/sf-universal.j2`` otherwise. This is the policy used for
    the cross-model transfer arms, and it is the one to use for new work: it
    involves no per-family choice made after seeing scores.

``frozen_insample``
    ``default``, overridden by ``templates/ROUTING.map`` for six families whose
    best-performing template was picked by reading their own scores. This
    reproduces the arm reported for the model the packages were evolved on.
    **Those six choices are in-sample.** The selection is stated in the map's
    own header, here, and in the README, and the renderer prints a warning
    when it is used.
    Shipping both policies is fine; shipping them without saying which produced
    which number is not.
"""

from __future__ import annotations

import re
from pathlib import Path

ROUTING_POLICIES = ("default", "frozen_insample")
UNIVERSAL = "sf-universal.j2"


def family_slug(family: str) -> str:
    """Normalise a task-family directory name into its template slug.

    ``Inventory-&-Finance-Integration`` becomes ``inventory-finance-integration``
    so that the directory names on disk and the template names in the package
    agree without a lookup table.
    """
    slug = family.strip().lower().replace("&", " ")
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def load_routing_map(package: Path) -> dict[str, str]:
    """Parse ``templates/ROUTING.map``: ``<family-slug> <template-filename>``."""
    path = package / "templates" / "ROUTING.map"
    overrides: dict[str, str] = {}
    if not path.is_file():
        return overrides
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"{path}: expected '<family-slug> <template>', got {line!r}")
        overrides[parts[0]] = parts[1]
    return overrides


def resolve_template(family: str, package: Path, routing: str = "default") -> Path:
    """Return the template file a family should be rendered with."""
    if routing not in ROUTING_POLICIES:
        raise ValueError(f"routing must be one of {ROUTING_POLICIES}, got {routing!r}")
    templates = package / "templates"
    slug = family_slug(family)

    if routing == "frozen_insample":
        override = load_routing_map(package).get(slug)
        if override:
            path = templates / override
            if not path.is_file():
                raise FileNotFoundError(f"ROUTING.map names a missing template: {path}")
            return path

    specific = templates / f"sf-{slug}.j2"
    if specific.is_file():
        return specific
    universal = templates / UNIVERSAL
    if not universal.is_file():
        raise FileNotFoundError(f"no template for family {family!r} and no {UNIVERSAL}")
    return universal


__all__ = [
    "ROUTING_POLICIES",
    "family_slug",
    "load_routing_map",
    "resolve_template",
]
