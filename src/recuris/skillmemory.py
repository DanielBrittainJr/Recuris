"""Skill Memory package loader — `load_skill_memory(path)` is THE entry point.

A package = manifest.yaml (selects strategies by name) + em/ (md library)
+ strategies.py (optional, `local:` prefix, always human-reviewed).
Load-time validation: unknown names / missing SPI methods fail at startup,
never mid-run. Switching tasks = switching the path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from recuris import builtin
from recuris.em.store import EMStore
from recuris.errors import ProfileError
from recuris.wm.schema import MatchPolicy, RenderPolicy, WMSchema


@dataclass
class SkillMemory:
    name: str
    root: Path
    wm_schema: WMSchema
    manager: Any
    matcher_name: str
    em: EMStore
    deliverers: list = field(default_factory=list)   # [(instance, event_or_None)]
    checkers: list = field(default_factory=list)     # [(instance, event_or_None)]
    status_board: bool = False
    board_notice: bool = True
    gate_lines: str = "all_pending"   # all_pending | authorized_only (v3)
    stay_notice: bool = False         # board footer "please stay on the line"
    stay_notice_text: str = ""        # empty = engine default; a package may override it
    oracle: Any = None                # FeasibilityOracle instance (airline BLOCKED rulings)
    raw: dict = field(default_factory=dict)


import threading as _threading

_LOCAL_IMPORT_LOCK = _threading.Lock()
_LOCAL_NAME_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _load_local(root: Path, spec: str, cfg: dict | None):
    """Resolve 'local:module.ClassName' from the package directory.

    Audit fix #7 (critical): module name is identifier-validated and the resolved
    path must stay INSIDE the package root — no traversal, no absolute paths.
    'strategies.py is always human-reviewed' only holds if local: cannot reach
    code elsewhere."""
    modattr = spec[len("local:"):]
    if "." not in modattr:
        raise ProfileError(f"{root}: bad local spec '{spec}' (want local:module.Class)")
    modname, clsname = modattr.rsplit(".", 1)
    if not (_LOCAL_NAME_RE.match(modname) and _LOCAL_NAME_RE.match(clsname)):
        raise ProfileError(f"{root}: illegal local spec '{spec}'")
    path = root / f"{modname}.py"
    if not path.resolve().is_relative_to(root.resolve()):
        raise ProfileError(f"{root}: local module escapes package root: {spec}")
    if not path.exists():
        raise ProfileError(f"{root}: local module missing: {path.name}")
    key = f"skm_{root.name}_{modname}"
    # concurrency-safe (smoke-found bug): under cc>1 two sims load the same
    # plugin; the old code registered the half-initialized module in sys.modules
    # BEFORE exec_module ran, so the racing thread got a module with no classes
    # yet ("class X not found"). Fix: exec fully, THEN register, under a lock.
    with _LOCAL_IMPORT_LOCK:
        mod = sys.modules.get(key)
        if mod is None:
            spec_ = importlib.util.spec_from_file_location(key, path)
            if spec_ is None or spec_.loader is None:
                raise ProfileError(f"cannot load {path}")
            mod = importlib.util.module_from_spec(spec_)
            spec_.loader.exec_module(mod)   # finish loading BEFORE publishing
            sys.modules[key] = mod
    cls = getattr(mod, clsname, None)
    if cls is None:
        raise ProfileError(f"{path.name}: class {clsname} not found")
    return cls(**(cfg or {}))


def _make(root: Path, registry_maker, spec: str, cfg: dict | None):
    if spec.startswith("local:"):
        return _load_local(root, spec, cfg)
    return registry_maker(spec, cfg)


def _build_schema(wm_cfg: dict, entry_kind) -> WMSchema:
    s = wm_cfg.get("schema") or {}
    match_keys = {k: s[k] for k in ("binding_key", "collection_key") if k in s}
    render_keys = {}
    if "machine_id_pattern" in s:
        render_keys["machine_id_pattern"] = s["machine_id_pattern"]
    return WMSchema(
        max_entries=int(s.get("max_entries", 8)),
        match=MatchPolicy(**match_keys),
        render=RenderPolicy(**render_keys),
        board_marker=s.get("board_marker", "--- Progress (system-verified)"),
        entry_kind=entry_kind,
    )


_VALID_EVENTS = {e.value for e in __import__("recuris.events", fromlist=["Event"]).Event}


def _bindings(root: Path, items, maker, required_attr: str) -> list:
    """Audit fix #8/#10: role-specific SPI check + event-name validation at load
    (typo in 'at:' must fail at startup, never become a silent no-op)."""
    out = []
    for it in items or []:
        spec = it.get("use")
        if not spec:
            raise ProfileError(f"{root}: binding missing 'use': {it}")
        inst = _make(root, maker, spec, it.get("cfg"))
        if not callable(getattr(inst, required_attr, None)):
            raise ProfileError(
                f"{root}: {spec} lacks required SPI method '{required_attr}'"
            )
        at = it.get("at")
        for ev in ([at] if at else list(getattr(inst, "events", ()) or ())):
            if ev not in _VALID_EVENTS:
                raise ProfileError(f"{root}: {spec} bound to unknown event '{ev}'")
        out.append((inst, at))
    return out


def _apply_env_overrides(sm: SkillMemory) -> SkillMemory:
    """TAU2_* compatibility layer for A/B arms (unset vars change nothing)."""
    wr = os.getenv("TAU2_WRITE_REVIEW")
    if wr == "0":
        sm.deliverers = [(d, at) for d, at in sm.deliverers if d.name != "exemplar_bounce"]
    elif wr == "1" and not any(d.name == "exemplar_bounce" for d, _ in sm.deliverers):
        sm.deliverers.append((builtin.make_deliverer("exemplar_bounce"), None))
    tg = os.getenv("TAU2_TRUTH_GUARD")
    if tg == "0":
        sm.checkers = [(c, at) for c, at in sm.checkers if c.name != "truth_protocol"]
    elif tg == "1" and not any(c.name == "truth_protocol" for c, _ in sm.checkers):
        sm.checkers.append((builtin.make_checker("truth_protocol"), None))
    level = os.getenv("TAU2_TRUTH_LEVEL")
    if level:
        # audit fix #9: rebuild preserving the checker's original cfg (patterns)
        sm.checkers = [
            (
                builtin.make_checker(
                    "truth_protocol",
                    {**getattr(c, "init_cfg", {}), "level": level},
                )
                if c.name == "truth_protocol"
                else c,
                at,
            )
            for c, at in sm.checkers
        ]
    sb = os.getenv("TAU2_STATUS_BOARD")
    if sb is not None and sb != "":
        sm.status_board = sb == "1"
    return sm


def load_skill_memory(root: str | Path) -> SkillMemory:
    root = Path(root)
    mpath = root / "manifest.yaml"
    if not mpath.exists():
        raise ProfileError(f"manifest.yaml not found under {root}")
    raw: dict = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}

    wm_cfg = raw.get("wm") or {}
    entry_kind = _make(
        root, lambda n, c: builtin.make_entry_kind(n), wm_cfg.get("entry_kind", "generic_item"), None
    )
    schema = _build_schema(wm_cfg, entry_kind)
    manager = _make(
        root,
        builtin.make_manager,
        wm_cfg.get("manager", "self_maintain_each_turn"),
        wm_cfg.get("manager_cfg"),
    )
    matcher_name = (raw.get("grounding") or {}).get("matcher", "delivery_receipt")

    oracle = None
    oracle_spec = (raw.get("feasibility") or {}).get("oracle")
    if oracle_spec:
        oracle = _make(root, lambda n, c: None, oracle_spec,
                       (raw.get("feasibility") or {}).get("cfg"))
        if oracle is None or not callable(getattr(oracle, "rule", None)):
            raise ProfileError(f"{root}: feasibility oracle '{oracle_spec}' lacks rule()")

    board_cfg = raw.get("board") or {}
    notice_mode = board_cfg.get("notice_in_wm", "always")
    status_board = bool(board_cfg.get("status_board", False))
    board_notice = (
        True
        if notice_mode == "always"
        else (status_board if notice_mode == "when_board_enabled" else False)
    )

    sm = SkillMemory(
        name=raw.get("name") or root.name,
        root=root,
        wm_schema=schema,
        manager=manager,
        matcher_name=matcher_name,
        em=EMStore.load(root / "em"),
        deliverers=_bindings(root, raw.get("delivery"), builtin.make_deliverer, "select"),
        checkers=_bindings(root, raw.get("checkers"), builtin.make_checker, "inspect"),
        status_board=status_board,
        board_notice=board_notice,
        gate_lines=(raw.get("gate") or {}).get("lines", "all_pending"),
        stay_notice=bool(board_cfg.get("stay_notice", False)),
        stay_notice_text=str(board_cfg.get("stay_notice_text", "")),
        oracle=oracle,
        raw=raw,
    )
    # audit fix #2: TAU2_* env overrides are HARNESS vocabulary - applied by the
    # tau2 adapter (apply_tau2_env_overrides), never by the generic loader.
    return sm


# exported for the tau2 adapter only
apply_tau2_env_overrides = _apply_env_overrides
