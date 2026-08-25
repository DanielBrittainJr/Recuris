# -*- coding: utf-8 -*-
"""Meta-Agent package linter — the deterministic referee that catches a Skill
Memory package whose cards/delivery will SILENTLY not load or not fire.

It mirrors the framework loader exactly (src/recuris/em/store.py loads only
`*.md`; em/entry.py requires `type` in {knowledge,procedure,action_result} and
reads trigger.event / trigger.tool) and checks the manifest delivery uses the
real `- use: <deliverer>` schema and actually covers the cards present.

Usage:
  recuris metaagent lint --pkg skill_memories/<package>
Exit code 0 = PASS (every card will load AND be delivered); 1 = FAIL.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from recuris.metaagent.reachability import (
    entry_kind_preserves_tool,
    tau2_tool_capabilities,
    tb21_tool_capabilities,
)

VALID_TYPES = {"knowledge", "procedure", "action_result"}
# the ONLY top-level manifest sections the kernel reads — anything else is silently ignored
VALID_MANIFEST_KEYS = {"name", "wm", "grounding", "delivery", "checkers", "gate",
                       "feasibility", "board"}
ALLOWED_CHECKERS = {"truth_protocol", "execution_gate", "anti_escalation"}
ALLOWED_DELIVERERS = {"exemplar_bounce", "state_reminder", "need_driven_retrieval",
                      "embedding_retrieval", "standing_inject", "boundary_inject"}
# deliverers that can surface knowledge/procedure cards (retrieval, standing injection,
# or the state_reminder specialization which pulls named procedure docs by id)
KN_DELIVERERS = {"need_driven_retrieval", "embedding_retrieval",
                 "standing_inject", "boundary_inject", "state_reminder"}

def _ctor_keys(registry_name: str, name: str):
    """Valid cfg keys = the builtin's constructor params. Returns a set, or None
    if the framework can't be imported (then cfg-key validation is skipped)."""
    import inspect
    import sys as _sys
    root = str(Path(__file__).resolve().parents[1] / "src")
    if root not in _sys.path:
        _sys.path.insert(0, root)
    try:
        from recuris import builtin  # type: ignore
    except Exception:
        return None
    reg = getattr(builtin, registry_name, {})
    cls = reg.get(name)
    if cls is None:
        return set()
    params = inspect.signature(cls).parameters
    return {p for p in params if p != "self"}

def _registry_names(registry_name: str):
    """Valid names in a builtin registry (ENTRY_KINDS/MANAGERS/MATCHERS/...), or None
    if the framework can't be imported."""
    import sys as _sys
    root = str(Path(__file__).resolve().parents[1] / "src")
    if root not in _sys.path:
        _sys.path.insert(0, root)
    try:
        from recuris import builtin  # type: ignore
    except Exception:
        return None
    reg = getattr(builtin, registry_name, None)
    if reg is None:
        return None
    return set(reg) if isinstance(reg, dict) else set(reg)

def parse_card(p: Path):
    """Mirror EMEntry.parse; return (ok, info_or_error)."""
    text = p.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("---"):
        return False, "does not start with '---' frontmatter"
    try:
        _, fm_text, body = text.split("---", 2)
    except ValueError:
        return False, "malformed frontmatter (need opening and closing '---')"
    fm = yaml.safe_load(fm_text) or {}
    etype = str(fm.get("type") or "")
    if etype not in VALID_TYPES:
        bad = "missing" if not etype else f"'{etype}'"
        return False, (f"frontmatter `type` is {bad}; must be one of "
                       f"{sorted(VALID_TYPES)} (NOTE: the key is `type`, not `em_type`)")
    trig = fm.get("trigger") or {}
    return True, {"id": str(fm.get("id") or p.stem), "type": etype,
                  "event": str(trig.get("event") or ""),
                  "tool": str(trig.get("tool") or "")}

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pkg", required=True)
    ap.add_argument("--domain", help="also run the gold-id leak scan for this domain "
                                     "(red line: cards must use placeholder ids only)")
    ap.add_argument("--benchmark", choices=("tau2", "tb21"), default="tau2",
                    help="downstream runtime the package targets; selects the "
                         "gold-id harvester and the tool-capability provider")
    args = ap.parse_args()
    pkg = Path(args.pkg)
    problems, warnings, loaded = [], [], []

    # 0) leakage red line — card/manifest text must not contain any REAL domain id
    if args.domain:
        from recuris.metaagent import sanitize as ma_sanitize
        # The harbor benchmarks have no tau2 gold caches (harvest_gold_ids would
        # FileNotFoundError); empty gold keeps scan_package a structural no-op,
        # mirroring driver E7.1.  Shape MUST be the {class: []} dict — scan_text
        # calls gold.get() (ma_sanitize.py:114), so a bare set() would
        # AttributeError.
        gold = ({cls: [] for cls in ma_sanitize.ID_PATTERNS}
                if args.benchmark == "tb21"
                else ma_sanitize.harvest_gold_ids(args.domain))
        leaks = ma_sanitize.scan_package(pkg, gold)
        for rel, hits in leaks.items():
            problems.append(f"LEAKED REAL GOLD ID(s) in {rel}: {hits} -> cards must use "
                            f"placeholder ids only (e.g. #W0000001); copying a task's real "
                            f"id is answer-leakage and is always rejected")

    if not pkg.exists():
        print(f"FAIL: package dir not found: {pkg}")
        sys.exit(1)

    # 1) manifest
    man_path = pkg / "manifest.yaml"
    if not man_path.exists():
        problems.append("no manifest.yaml")
        man = {}
    else:
        try:
            man = yaml.safe_load(man_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            problems.append(f"manifest.yaml does not parse as YAML: {e}")
            man = {}
    # top-level sections: the kernel only reads a fixed set; invented sections do NOTHING
    for k in man:
        if k not in VALID_MANIFEST_KEYS:
            problems.append(f"manifest has INVALID top-level section `{k}:` -> the kernel "
                            f"only reads {sorted(VALID_MANIFEST_KEYS)}; `{k}` is silently "
                            f"ignored (you cannot add a custom section like `validators`). "
                            f"Realize discipline via `checkers` (builtin gates) or note it "
                            f"out-of-scope.")

    # 2) cards — every file under em/, split .md (loaded) vs ignored
    em_dir = pkg / "em"
    md_files, ignored = [], []
    if em_dir.exists():
        for p in sorted(em_dir.rglob("*")):
            if p.is_file():
                if p.suffix.lower() == ".md" and p.name.lower() != "readme.md":
                    md_files.append(p)
                elif p.suffix.lower() in {".yaml", ".yml", ".txt", ".json"}:
                    ignored.append(p)
    for p in ignored:
        problems.append(f"IGNORED FILE (loader only reads *.md): {p.relative_to(pkg)} "
                        f"-> rename to .md or it does NOTHING")
    types_present = set()
    for p in md_files:
        ok, info = parse_card(p)
        if not ok:
            problems.append(f"CARD WILL NOT LOAD: {p.relative_to(pkg)} -> {info}")
            continue
        loaded.append((p.relative_to(pkg), info))
        types_present.add(info["type"])
        if info["type"] == "action_result" and (not info["event"] or not info["tool"]):
            problems.append(f"action_result card {p.relative_to(pkg)} missing "
                            f"trigger.event/trigger.tool -> exemplar_bounce cannot match it")

    # 3) delivery — must be `- use: <deliverer>` schema and cover the card types
    delivery = man.get("delivery") or []
    used = set()
    delivery_cfg: dict[str, dict] = {}
    delivery_specs: list[tuple[str, dict, int, str | None]] = []
    for i, d in enumerate(delivery):
        if not isinstance(d, dict) or "use" not in d:
            problems.append(f"delivery[{i}] is not the correct schema: expected "
                            f"`- use: <deliverer>` with optional `cfg: {{...}}`, "
                            f"got keys {list(d.keys()) if isinstance(d,dict) else type(d).__name__} "
                            f"(NOTE: no `name`/`em_path`/`deliverer`/`filter`/`args` keys)")
            continue
        if d["use"] not in ALLOWED_DELIVERERS:
            problems.append(f"delivery[{i}] use='{d['use']}' not in {sorted(ALLOWED_DELIVERERS)}")
        used.add(d["use"])
        # cfg keys must be the deliverer's actual constructor params (the kernel does
        # cls(**cfg) — an unknown key CRASHES the package at load, not just silently)
        cfg = d.get("cfg") or {}
        if isinstance(cfg, dict):
            delivery_cfg[d["use"]] = cfg
            delivery_specs.append((
                str(d["use"]), cfg, i,
                str(d["at"]) if d.get("at") is not None else None,
            ))
        valid = _ctor_keys("DELIVERERS", d["use"])
        if valid is not None and isinstance(cfg, dict):
            bad = [k for k in cfg if k not in valid]
            if bad:
                hint = ""
                if "at" in bad:
                    # Three campaigns died on exactly this placement: the fix
                    # loop is steered by this error text, so the error text
                    # must teach the correct location, not just reject.
                    hint = (" NOTE: `at` is an ENTRY-LEVEL key -- a sibling of "
                            "`use:` and `cfg:` in the delivery list item -- "
                            "used to rebind the trigger event. Write "
                            "`- use: embedding_retrieval` / `  at: turn_start` "
                            "/ `  cfg: {...}`. MOVE it out of cfg; do NOT "
                            "delete the rebinding or the deliverer.")
                problems.append(f"delivery[{i}] use='{d['use']}' has INVALID cfg key(s) {bad} "
                                f"-> the ONLY valid cfg keys are the deliverer's constructor "
                                f"params {sorted(valid)}; unknown keys CRASH the package at load "
                                f"(cls(**cfg)). Do not invent cfg keys (em_filter/tags/intent_triggers …)."
                                + hint)
    # coverage: action_result needs exemplar_bounce; knowledge/procedure need need_driven_retrieval
    if "action_result" in types_present and "exemplar_bounce" not in used:
        problems.append("have action_result card(s) but no `- use: exemplar_bounce` in "
                        "delivery -> those cards never fire")
    if ({"knowledge", "procedure"} & types_present) and not (used & KN_DELIVERERS):
        warnings.append("have knowledge/procedure card(s) but no retrieval/standing "
                        f"deliverer ({sorted(KN_DELIVERERS)}) -> they may never surface")
    # 4) checkers — same `- use: <name>` + `cfg:` schema as delivery; fixed builtin set
    used_checkers: set[str] = set()
    checker_cfg: dict[str, dict] = {}
    for i, c in enumerate(man.get("checkers") or []):
        if not isinstance(c, dict) or "use" not in c:
            keys = list(c.keys()) if isinstance(c, dict) else type(c).__name__
            problems.append(f"checkers[{i}] wrong schema: expected `- use: <checker>` + "
                            f"optional `cfg:`, got {keys} (NOTE: it is `use:`/`cfg:`, NOT "
                            f"`type:`/`config:`)")
            continue
        if c["use"] not in ALLOWED_CHECKERS:
            problems.append(f"checkers[{i}] use='{c['use']}' not a builtin "
                            f"{sorted(ALLOWED_CHECKERS)} — you SELECT a builtin checker, you "
                            f"cannot add a custom one (that needs kernel code = out of scope)")
            continue
        used_checkers.add(str(c["use"]))
        ccfg = c.get("cfg") or {}
        if isinstance(ccfg, dict):
            checker_cfg[str(c["use"])] = ccfg
        cvalid = _ctor_keys("CHECKERS", c["use"])
        if cvalid is not None and isinstance(ccfg, dict):
            cbad = [k for k in ccfg if k not in cvalid]
            if cbad:
                problems.append(f"checkers[{i}] use='{c['use']}' has INVALID cfg key(s) "
                                f"{cbad} -> valid keys are {sorted(cvalid)} (unknown keys "
                                f"crash at load). Do not invent cfg keys.")

    # 5) wm + grounding — names must be in the builtin registries
    for section, key, reg in [("wm", "entry_kind", "ENTRY_KINDS"),
                              ("wm", "manager", "MANAGERS"),
                              ("grounding", "matcher", "MATCHERS")]:
        sec = man.get(section) or {}
        val = sec.get(key) if isinstance(sec, dict) else None
        if val is None:
            continue
        names = _registry_names(reg)
        if names is not None and val not in names:
            problems.append(f"{section}.{key}='{val}' not a builtin {sorted(names)} — "
                            f"select an existing name, do not invent")

    # 5b) carrier reachability — type coverage alone is insufficient.  A card
    # can be syntactically valid yet impossible to surface because its selected
    # carrier cannot observe the declared event/tool (the v5 silent-no-op bug).
    entry_kind_name = str((man.get("wm") or {}).get("entry_kind") or "generic_item")
    tool_aware_wm = entry_kind_preserves_tool(entry_kind_name)
    tool_capabilities: dict[str, dict[str, object]] = {}
    if args.domain:
        try:
            if args.benchmark == "tb21":
                # The terminal tool face is a single bash_command
                # write tool), so the TB2.1 capability provider applies.
                tool_capabilities = tb21_tool_capabilities(args.domain)
            else:
                tool_capabilities = tau2_tool_capabilities(args.domain)
        except Exception as exc:
            problems.append(
                f"cannot inspect tool capabilities for domain {args.domain!r}: {exc}"
            )

    def _em_types(cfg: dict, default: tuple[str, ...]) -> set[str]:
        raw = cfg.get("em_types", default)
        if isinstance(raw, (list, tuple)):
            return {str(value) for value in raw}
        return set()

    def _scoped_retrieval_reaches(info: dict, cfg: dict) -> tuple[bool, str]:
        if not cfg.get("scope_by_tool", False):
            return True, ""
        trigger_tool = str(info.get("tool") or "")
        if trigger_tool == "*":
            return True, ""
        if not trigger_tool:
            return False, "scoped retrieval requires a card trigger.tool"
        if not tool_aware_wm:
            return False, (
                f"wm.entry_kind={entry_kind_name!r} drops the tool field required "
                "by scope_by_tool"
            )
        if tool_capabilities:
            capability = tool_capabilities.get(trigger_tool)
            if capability is None:
                return False, f"trigger.tool={trigger_tool!r} is not a domain tool"
            if capability.get("mutates_state") is not True:
                return False, (
                    f"trigger.tool={trigger_tool!r} is non-mutating and never enters "
                    "the write-tool ledger observed by scoped retrieval"
                )
        return True, ""

    # Constructor-valid values can still create deterministic no-ops.
    for use, cfg, index, _binding_at in delivery_specs:
        if use in {"need_driven_retrieval", "embedding_retrieval"}:
            top_k = cfg.get("top_k", 1 if use == "need_driven_retrieval" else 2)
            if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
                problems.append(
                    f"delivery[{index}] use={use!r} requires integer top_k > 0; "
                    f"got {top_k!r}, which cannot deliver a card"
                )
            raw_types = cfg.get("em_types", ("knowledge", "procedure"))
            if (not isinstance(raw_types, (list, tuple)) or
                    not set(map(str, raw_types)).issubset(VALID_TYPES)):
                problems.append(
                    f"delivery[{index}] use={use!r} cfg.em_types must be a list/tuple "
                    f"drawn from {sorted(VALID_TYPES)}"
                )
        if use == "embedding_retrieval":
            min_sim = cfg.get("min_sim", 0.30)
            if (isinstance(min_sim, bool) or not isinstance(min_sim, (int, float)) or
                    float(min_sim) > 1.0):
                problems.append(
                    f"delivery[{index}] embedding_retrieval cfg.min_sim must be numeric "
                    f"and <= 1.0; got {min_sim!r}"
                )

    for rel, info in loaded:
        carriers: list[str] = []
        unreachable_reasons: list[str] = []
        for use, cfg, index, binding_at in delivery_specs:
            if use == "exemplar_bounce":
                em_type = str(cfg.get("em_type") or "action_result")
                if (info["type"] == em_type and info["event"] == "pre_write" and
                        bool(info["tool"])):
                    carriers.append(f"delivery[{index}]:exemplar_bounce")
            elif use == "standing_inject":
                em_type = str(cfg.get("em_type") or "knowledge")
                if info["type"] == em_type and info["event"] == "turn_start":
                    carriers.append(f"delivery[{index}]:standing_inject")
            elif use in {"need_driven_retrieval", "embedding_retrieval"}:
                default_types = ("knowledge", "procedure")
                top_k = cfg.get("top_k", 1 if use == "need_driven_retrieval" else 2)
                top_k_ok = (
                    isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0
                )
                if info["type"] in _em_types(cfg, default_types) and top_k_ok:
                    reachable, reason = _scoped_retrieval_reaches(info, cfg)
                    if reachable:
                        carriers.append(f"delivery[{index}]:{use}")
                    elif reason:
                        unreachable_reasons.append(
                            f"delivery[{index}]:{use}: {reason}"
                        )
            elif use == "state_reminder":
                doc_ids = {
                    str(cfg.get("unconfirmed_doc") or "confirm_before_execute"),
                    str(cfg.get("authorized_doc") or "authorized_execute_check"),
                }
                if info["id"] in doc_ids:
                    carriers.append(f"delivery[{index}]:state_reminder")
            elif use == "boundary_inject":
                event = str(cfg.get("at") or "turn_start")
                tool = str(cfg.get("tool") or "*")
                dispatch_event = binding_at or event
                if (event != "pre_write" and dispatch_event == event and
                        info["event"] == event and
                        (info["tool"] == tool or info["tool"] == "*")):
                    carriers.append(f"delivery[{index}]:boundary_inject")

        if "anti_escalation" in used_checkers:
            transfer_tool = str(
                checker_cfg.get("anti_escalation", {}).get(
                    "transfer_tool", "transfer_to_human_agents"
                )
            )
            if info["event"] == "draft_ready" and info["tool"] == transfer_tool:
                carriers.append("checker:anti_escalation")

        if not carriers:
            detail = (
                "; ".join(sorted(set(unreachable_reasons)))
                if unreachable_reasons else
                "no configured delivery/checker matches this card's type/event/tool"
            )
            problems.append(
                f"CARD HAS NO REACHABLE CARRIER: {rel} "
                f"[type={info['type']} event={info['event']} tool={info['tool']}] "
                f"-> {detail}"
            )

    # 6) manifest structural keys — the kernel reads a FIXED key set per section;
    #    an invented or misplaced key is silently ignored (or crashes at load),
    #    so both are lint problems (mirrors src/recuris/skillmemory.py).
    SECTION_KEYS = {
        "wm": {"entry_kind", "manager", "manager_cfg", "schema"},
        "gate": {"lines"},
        "board": {"notice_in_wm", "status_board", "stay_notice", "stay_notice_text"},
        "feasibility": {"oracle", "cfg"},
        "grounding": {"matcher", "cfg"},
    }
    SCHEMA_KEYS = {"binding_key", "collection_key", "machine_id_pattern",
                   "max_entries", "board_marker"}
    for section, keys in SECTION_KEYS.items():
        sec = man.get(section)
        if isinstance(sec, dict):
            for k in sec:
                if k not in keys:
                    problems.append(f"{section}.{k} is NOT a kernel key -> silently ignored "
                                    f"(kernel reads only {sorted(keys)}); if you meant a "
                                    f"schema field it belongs under wm.schema.<field>")
    wm_schema = ((man.get("wm") or {}).get("schema") or {})
    for k in (wm_schema if isinstance(wm_schema, dict) else {}):
        if k not in SCHEMA_KEYS:
            problems.append(f"wm.schema.{k} is NOT a kernel key -> silently ignored "
                            f"(valid: {sorted(SCHEMA_KEYS)})")
    gate_lines = (man.get("gate") or {}).get("lines")
    if gate_lines is not None and gate_lines not in {"all_pending", "authorized_only"}:
        problems.append(f"gate.lines='{gate_lines}' invalid -> must be all_pending or "
                        f"authorized_only")
    matcher_val = (man.get("grounding") or {}).get("matcher")
    if matcher_val == "receipt_binding_match" and "binding_key" not in (
            wm_schema if isinstance(wm_schema, dict) else {}):
        problems.append("grounding.matcher=receipt_binding_match REQUIRES wm.schema."
                        "binding_key (the kernel raises ProfileError without it — "
                        "note: schema fields live under wm.schema, not wm)")

    # report
    print(f"=== ma_lint {pkg} ===")
    print(f"cards that WILL load ({len(loaded)}):")
    for rel, info in loaded:
        print(f"  OK  {rel}  [type={info['type']} event={info['event']} tool={info['tool']}]")
    print(f"delivery strategies: {sorted(used) or '(none)'}")
    if warnings:
        print("\nWARNINGS:")
        for w in warnings:
            print("  ! " + w)
    if problems:
        print(f"\nFAIL — {len(problems)} problem(s) (these make cards inert):")
        for pb in problems:
            print("  X " + pb)
        sys.exit(1)
    print("\nPASS — every card will load and is covered by a delivery strategy.")

if __name__ == "__main__":
    main()
