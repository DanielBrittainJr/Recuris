"""Arm configuration: inheritance from a shared base, and no silent keys.

Two arms of a paired comparison must differ in exactly one thing. Written by
hand they differ in more, and the extra difference is never the one anyone
notices. We have been bitten twice: once by a turn limit that was throttled in
the treatment arm only, and once by a misspelled key that the loader dropped
without a word, so the mechanism it was meant to enable never ran and the arm
scored like a control.

Both are structural problems, so both get structural answers.

**Inheritance.** An arm config names a base and states only its delta. What it
does not say, it cannot differ in.

**Unknown keys raise.** A key that is not in the schema is an error, not a
shrug. A typo becomes a startup failure rather than a plausible-looking number.

The schema is deliberately shallow. It is not trying to type-check the whole
system; it is trying to make sure two configs that claim to be a pair are one.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from recuris.paths import repo_root


class ConfigError(ValueError):
    """A configuration is malformed, or names something that does not exist."""


# Keys every arm config may carry, with the type each must have. Anything else
# is rejected. `treatment` is spelled out because leaving one of its three
# switches unset is the specific mistake that produced two arms which looked
# identical on the command line and were not.
SCHEMA: dict[str, type | tuple[type, ...]] = {
    "extends": str,
    "description": str,
    "benchmark": str,
    "domain": str,
    "arm": str,
    "agent": str,
    "skill_memory": (str, type(None)),
    "num_trials": int,
    "max_concurrency": int,
    "split": (str, type(None)),
    "model": (str, type(None)),
    "open_downstream": bool,
    "treatment": dict,
    "llm_args": dict,
    "metaagent": dict,
    "notes": str,
}

TREATMENT_KEYS = {"gate_term", "gate_term_wm", "status_board"}


def config_root() -> Path:
    return repo_root() / "configs"


def _read(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"config not found: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return loaded


def _merge(base: dict, delta: dict) -> dict:
    """Shallow merge, one level deep for nested mappings.

    Deliberately shallow. A deep merge lets a delta reach into a structure it
    never names, which is the opposite of what inheritance is for here.
    """
    merged = dict(base)
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def validate(config: dict, *, source: str = "<config>") -> dict:
    problems: list[str] = []

    for key, value in config.items():
        if key not in SCHEMA:
            near = [name for name in SCHEMA if name.startswith(key[:3])]
            hint = f" (did you mean {near[0]!r}?)" if near else ""
            problems.append(f"unknown key {key!r}{hint}")
            continue
        expected = SCHEMA[key]
        if not isinstance(value, expected):
            names = (
                expected.__name__
                if isinstance(expected, type)
                else "/".join(t.__name__ for t in expected)
            )
            problems.append(f"{key!r} must be {names}, got {type(value).__name__}")

    treatment = config.get("treatment")
    if isinstance(treatment, dict):
        missing = TREATMENT_KEYS - set(treatment)
        extra = set(treatment) - TREATMENT_KEYS
        if missing:
            problems.append(
                f"treatment must declare all of {sorted(TREATMENT_KEYS)}; "
                f"missing {sorted(missing)}. Leaving one unset is how two arms "
                "end up differing invisibly."
            )
        if extra:
            problems.append(f"treatment has unknown switches: {sorted(extra)}")
        for name in sorted(TREATMENT_KEYS & set(treatment)):
            if not isinstance(treatment[name], bool):
                problems.append(f"treatment.{name} must be true or false")

    if problems:
        raise ConfigError(f"{source}:\n  " + "\n  ".join(problems))
    return config


def load(name: str | Path, *, _seen: tuple[str, ...] = ()) -> dict:
    """Load a config by path or by ``family/name``, resolving ``extends``."""
    path = Path(name)
    if not path.is_file():
        candidate = config_root() / f"{name}.yaml"
        if candidate.is_file():
            path = candidate
        else:
            raise ConfigError(
                f"config not found: {name}. Available: "
                + ", ".join(sorted(known_configs())) or "(none)"
            )
    path = path.resolve()

    key = path.as_posix()
    if key in _seen:
        raise ConfigError(f"circular extends: {' -> '.join([*_seen, key])}")

    raw = _read(path)
    parent = raw.pop("extends", None)
    if parent is not None:
        base = load(config_root() / f"{parent}.yaml", _seen=(*_seen, key))
        raw = _merge(base, raw)

    return validate(raw, source=path.as_posix())


def known_configs() -> list[str]:
    root = config_root()
    if not root.is_dir():
        return []
    return sorted(
        p.relative_to(root).with_suffix("").as_posix()
        for p in root.rglob("*.yaml")
        if not p.name.startswith("_") and "_base" not in p.parts
    )


__all__ = ["ConfigError", "config_root", "known_configs", "load", "validate"]
