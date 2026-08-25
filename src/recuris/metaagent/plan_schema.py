# -*- coding: utf-8 -*-
"""Diagnosis-plan schema + deterministic validator.

Phase D of every driver round produces ONE plan.json: the full checklist of
this round's problems, each attributed to a component and carrying concrete
fix actions. The driver validates the plan HERE before any file is touched —
an illegal plan is bounced back to the model with the exact errors, it never
reaches the package.

Hard rules encoded (from the blood-lesson list):
  - components and every referenced primitive come from the fixed builtin menus
  - fix actions must match the diagnosed component (no checkers on an E diagnosis)
  - checker and carrier wiring must be reachable through the disclosed runtime
  - evidence tasks must be real failing tasks of this round (no invented evidence)
  - card-type <-> delivery pairing must close (action_result => exemplar_bounce)
  - exact (component, action, target) combos already REJECTED with regressions
    in the ledger are blocked (do-not-repeat)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PATCHABLE_COMPONENTS = {"E", "W", "RHO", "C"}
NON_PATCH_OWNERS = {"MODEL", "HARNESS", "BENCHMARK"}
OPPORTUNITY_STATES = {"present", "absent", "unknown"}
PATCHABILITY_STATES = {"memory", "external", "unknown"}
FAILURE_MODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# CEILING remains a supported legacy spelling for a non-patchable diagnosis.
# Deterministic pre-classification should prefer an explicit owner above.
COMPONENTS = PATCHABLE_COMPONENTS | NON_PATCH_OWNERS | {"CEILING"}
EM_TYPES = {"knowledge", "procedure", "action_result"}
# The formal Tau2 Meta-Agent runs use TurnRuntime.  Its role-specific dispatch
# surface is intentionally narrower than the framework-wide Event enum.
DELIVERER_TRIGGER_EVENTS = {"turn_start", "intent_recorded", "pre_write"}
CHECKER_TRIGGER_EVENTS = {"draft_ready"}
TRIGGER_EVENTS = DELIVERER_TRIGGER_EVENTS | CHECKER_TRIGGER_EVENTS
DELIVERERS = {"exemplar_bounce", "state_reminder", "need_driven_retrieval",
              "embedding_retrieval", "standing_inject", "boundary_inject"}
ENTRY_KINDS = {"service_request", "generic_item", "service_request_auth", "knowledge_need"}
MANAGERS = {"self_maintain_each_turn", "blueprint_then_delta"}
MATCHERS = {"receipt_binding_match", "delivery_receipt"}

# Cross-component requirements the kernel enforces at load time.  A plan that
# selects the mechanism on the left without providing the wm.schema field on
# the right puts the patch stage in an unwinnable state: adding the field
# violates plan conformance (undeclared edit) while omitting it violates
# ma_lint (the kernel raises ProfileError).  The validator therefore rejects
# such a plan here, before any package is touched.  Shared with the driver's
# conformance check so both referees read one rule table.
KERNEL_COUPLINGS: dict[str, tuple[str, str]] = {
    "receipt_binding_match": (
        "binding_key",
        "grounding.matcher='receipt_binding_match' REQUIRES wm.schema.binding_key "
        "(the kernel raises ProfileError without it). The plan must either keep "
        "the base package's binding_key or include a W/set_wm fix with "
        "field='binding_key'.",
    ),
}


def _builtin_cfg_keys(registry_name: str, name: str) -> set[str] | None:
    """Valid cfg keys = the builtin's constructor params (the same source of
    truth ma_lint uses).  Returns None when the framework cannot be imported;
    cfg-key validation is then skipped here and ma_lint stays the backstop."""
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

# legal fix actions per diagnosed component
ACTIONS_BY_COMPONENT = {
    "E": {"add_card", "edit_card", "remove_card"},
    "RHO": {"set_delivery", "edit_delivery", "remove_delivery"},
    "W": {"set_wm", "set_board", "set_grounding"},
    "C": {"set_checker", "remove_checker", "set_gate", "set_feasibility"},
}

# the spec text embedded verbatim into the Phase-D prompt
PLAN_SPEC_MD = """\
## plan.json — required format (STRICT, validated by a program)

Write EXACTLY one JSON object:

```json
{
  "round": <int>,
  "clusters": [
    {
      "cluster_id": "c1",
      "evidence_tasks": ["<task id>", ...],
      "symptom": "<1-2 sentences: what goes wrong, observable>",
      "failing_step": "<the exact step/action where the chain breaks>",
      "failure_mode": "<short snake_case description of the causal mechanism>",
      "component": "E | W | RHO | C | MODEL | HARNESS | BENCHMARK | CEILING",
      "action_opportunity": "present | absent | unknown",
      "opportunity_evidence": "<cite the turn/call proving O; say why unknown if unresolved>",
      "patchability": "memory | external | unknown",
      "evidence": "<cite the trace, policy, evaluator, or verifier record that supports it>",
      "fixes": [ { "action": "...", ... } ]
    }
  ]
}
```

Rules (a validator enforces every one of these — violations bounce the plan):
- `evidence_tasks` must be task ids that actually FAILED this round (listed in evidence.md).
- Use a non-patch owner (`MODEL`, `HARNESS`, or `BENCHMARK`) only when the
  supplied facts independently establish it. A terminal marker or failed score
  is not itself a causal label. These components require `"fixes": []`.
  `CEILING` remains accepted as the legacy spelling when no admissible memory
  repair exists; it also carries no fixes.
- O (`action_opportunity`) and P (`patchability`) are independent. A memory
  fix is legal only for O=`present` AND P=`memory`. `absent` or either
  `unknown` requires abstention (`fixes: []`). Cite concrete opportunity
  evidence from the failed trace; do not infer O from the final score or from a
  turn that appears only in a successful counterfactual. A hypothetical next
  actor is not evidence. If `post_terminal_assistant_event: absent`, do not
  claim a checker, draft, or write occurs after that event; cite a concrete
  earlier failed-trace turn or leave O unresolved.
- E/W/RHO/C require P=`memory`; MODEL/HARNESS/BENCHMARK require P=`external`.
  `CEILING` may use P=`external` or `unknown`, but never carries a fix.
- Legal `action` values depend on `component`:
    E   -> add_card / edit_card / remove_card
    RHO -> set_delivery / edit_delivery / remove_delivery
    W   -> set_wm / set_board / set_grounding
    C   -> set_checker / remove_checker / set_gate / set_feasibility
- Each cluster owns exactly one component and contains only that component's
  actions. A connected repair spanning components must use linked clusters
  that cite the same evidence (for example E/add_card plus RHO/set_delivery).
- Fix fields:
    add_card / edit_card: {"action", "em_type": "knowledge|procedure|action_result",
        "card_id": "<snake_case>", "sketch": "<what the card will teach, 2-4 sentences>",
        "trigger_event": "<exact framework event, REQUIRED for procedure and action_result; also for event-matched injection>",
        "trigger_tool": "<exact tool; non-wildcard for procedure/action_result>"}
    remove_card: {"action", "card_id"}
    set_delivery / edit_delivery: {"action", "deliverer": "<builtin name>",
        "cfg": {<exact constructor-key: JSON value, ...>},
        "at": "<runtime event, OPTIONAL — rebinds the deliverer's trigger
        event when the runtime does not raise its default (e.g. retrieval
        deliverers on a runtime that only raises turn_start). In the built
        manifest `at:` is an ENTRY-LEVEL key beside `use:`/`cfg:`, NEVER a
        cfg key>"}
    remove_delivery: {"action", "deliverer"}
    set_checker: {"action", "checker": "<builtin name>", "cfg": {<exact cfg>}}
    remove_checker: {"action", "checker": "<builtin name>"}
    set_wm: {"action", "field": "entry_kind|manager|binding_key|max_entries|machine_id_pattern",
        "value": "<...>"}
    set_board / set_gate / set_grounding / set_feasibility:
        {"action", "value": {<exact replacement object>}}
- If any add_card has em_type=action_result, the plan (or the current package)
  must include delivery `exemplar_bounce`; knowledge/procedure cards need a
  retrieval-family deliverer (need_driven_retrieval / embedding_retrieval /
  standing_inject / state_reminder), except a checker-routed card explicitly
  supported by the current adapter capability contract.
- If this plan adds a procedure card and configures `need_driven_retrieval` or
  `embedding_retrieval`,
  its cfg must include `scope_by_tool: true`, `max_per_episode: 1`, and
  `settle: false` (additional constructor fields are allowed).
- Carrier timing must match the intervention event stated by the disclosed
  capability contract. Tool-scoped retrieval additionally requires a
  tool-preserving WM entry kind and a mutating domain tool. A non-mutating tool
  can never be routed through the write-tool ledger.
- The formal TurnRuntime dispatches deliverers only at `turn_start`,
  `intent_recorded`, or `pre_write`, and checkers only at `draft_ready`.
  Framework-wide event names outside these four are not reachable in this run.
- If this plan adds an action_result card and configures `exemplar_bounce`,
  its cfg must include `exact_only: true`; unmatched write tools stay inert.
- standing_inject has HIGH regression cost — only for a card every task needs.
- A knowledge card carried by standing_inject needs `trigger.event=turn_start`;
  a boundary-injected card needs the configured event/tool metadata. Merely
  configuring a carrier does not make a metadata-free card reachable.
- Cards must be distilled from the domain policy + the correct action sequence,
  written with PLACEHOLDER ids only. All evidence you see is already masked;
  never invent an id that looks real.

### Worked example — valid wiring for a MIXED plan (copy this shape)

Plans that add several card types must provision one carrier per card type.
This composite passes every pairing rule above; adapt ids/tools, keep the shape:

- c1 E/add_card: em_type=action_result, trigger_event=pre_write,
  trigger_tool=<exact mutating tool>
- c2 E/add_card: em_type=procedure, trigger_event=intent_recorded,
  trigger_tool=<exact tool>
- c3 E/add_card: em_type=knowledge, trigger_event=turn_start, trigger_tool="*"
- c4 RHO/set_delivery: deliverer=exemplar_bounce, cfg={"exact_only": true}
  (carries c1)
- c5 RHO/set_delivery: deliverer=need_driven_retrieval,
  cfg={"scope_by_tool": true, "max_per_episode": 1, "settle": false}
  (carries c2; scoped retrieval can NEVER carry a trigger-free knowledge card)
- c6 RHO/set_delivery: deliverer=boundary_inject,
  cfg={"at": "turn_start", "when": "first_turn", "em_type": "knowledge",
  "tool": "*"} (carries c3 — boundary_inject is the low-regression unscoped
  route for knowledge cards; standing_inject also works but injects every turn)
- If any W/set_grounding sets matcher=receipt_binding_match, the wm schema must
  keep a binding_key (retain the base value or add a W/set_wm fix for it).

Final checklist before emitting plan.json — verify each line or the referee
bounces the plan without any behavioural evaluation:
1. every action_result card -> exemplar_bounce provisioned (plan or package);
2. every knowledge card -> trigger_event=turn_start plus an unscoped carrier
   (boundary_inject / standing_inject), OR full trigger_event+trigger_tool
   matching a configured scoped carrier;
3. every procedure card -> retrieval carrier with the mandated cfg triple;
4. receipt_binding_match -> binding_key retained.
"""


def _fix_target(fix: dict) -> str:
    return str(fix.get("card_id") or fix.get("deliverer") or fix.get("checker")
               or fix.get("field") or fix.get("sketch", ""))[:60]


def ledger_key(component: str, action: str, target: str) -> tuple:
    return (component, action, target)


def validate_plan(plan: dict, failing_tasks: set, blocked_keys: set,
                  pkg_deliverers: set | None = None,
                  expected_round: int | None = None,
                  forced_owners: dict[str, str] | None = None,
                  forced_opportunities: dict[str, str] | None = None,
                  current_entry_kind: str | None = None,
                  tool_aware_entry_kinds: set[str] | None = None,
                  known_tools: set[str] | None = None,
                  mutating_tools: set[str] | None = None,
                  pkg_checkers: set[str] | None = None,
                  checker_capabilities: dict[str, dict[str, object]] | None = None,
                  current_grounding_matcher: str | None = None,
                  current_wm_schema: dict | None = None,
                  ) -> tuple[list, list]:
    """Return (errors, warnings). Empty errors = plan is legal.
    blocked_keys: ledger keys (component, action, target) rejected with
    regressions before — exact repeats are blocked."""
    errors, warnings = [], []
    checker_caps = checker_capabilities or {}
    if not isinstance(checker_caps, dict) or any(
            not isinstance(name, str) or not isinstance(capability, dict)
            for name, capability in checker_caps.items()):
        errors.append("checker_capabilities must map checker names to objects")
        checker_caps = {}
    normalized_forced_owners: dict[str, str] = {}
    if forced_owners is not None:
        if not isinstance(forced_owners, dict):
            errors.append("forced_owners must be a task_id -> owner object")
        else:
            valid_forced_owners = PATCHABLE_COMPONENTS | NON_PATCH_OWNERS
            for task_id, owner in forced_owners.items():
                task_key = str(task_id)
                owner_name = str(owner)
                if not task_key.strip():
                    errors.append("forced_owners contains an empty task id")
                    continue
                if owner_name not in valid_forced_owners:
                    errors.append(
                        f"forced_owners[{task_key!r}]={owner_name!r} is invalid; "
                        f"must be one of {sorted(valid_forced_owners)}"
                    )
                    continue
                if task_key in normalized_forced_owners:
                    errors.append(
                        f"forced_owners contains duplicate normalized task id {task_key!r}"
                    )
                    continue
                normalized_forced_owners[task_key] = owner_name
    normalized_forced_opportunities: dict[str, str] = {}
    if forced_opportunities is not None:
        if not isinstance(forced_opportunities, dict):
            errors.append("forced_opportunities must be a task_id -> opportunity object")
        else:
            for task_id, status in forced_opportunities.items():
                task_key = str(task_id)
                status_name = str(status)
                if not task_key.strip():
                    errors.append("forced_opportunities contains an empty task id")
                    continue
                if status_name not in OPPORTUNITY_STATES:
                    errors.append(
                        f"forced_opportunities[{task_key!r}]={status_name!r} is invalid; "
                        f"must be one of {sorted(OPPORTUNITY_STATES)}"
                    )
                    continue
                normalized_forced_opportunities[task_key] = status_name
    if not isinstance(plan, dict):
        return ["plan.json top level must be a JSON object"], []
    if not isinstance(plan.get("round"), int) or isinstance(plan.get("round"), bool):
        errors.append("plan.round must be an integer")
    elif expected_round is not None and plan["round"] != expected_round:
        errors.append(f"plan.round={plan['round']} does not match current round {expected_round}")
    clusters = plan.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        return ["plan.clusters must be a non-empty list"], []

    planned_deliverers = set(pkg_deliverers or set())
    planned_checkers = set(pkg_checkers or set())
    planned_ar_cards, planned_kn_cards = [], []
    newly_added_procedure_cards: list[dict] = []
    newly_added_action_result_cards: list[dict] = []
    newly_added_knowledge_cards: list[dict] = []
    configured_scoped_retrieval: list[tuple[str, dict]] = []
    configured_exemplar_bounce: list[tuple[str, dict]] = []
    retrieval_cfgs_by_name: dict[str, list[dict]] = {}
    configured_checker_cfgs: dict[str, list[dict]] = {}
    planned_entry_kind = str(current_entry_kind or "generic_item")
    planned_grounding_matcher: str | None = None
    planned_wm_values: dict[str, str] = {}
    seen_targets: dict[tuple[str, str], str] = {}

    cluster_ids: set[str] = set()
    for ci, cl in enumerate(clusters):
        tag = f"clusters[{ci}]"
        if not isinstance(cl, dict):
            errors.append(f"{tag} must be an object")
            continue
        cluster_id = str(cl.get("cluster_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", cluster_id):
            errors.append(f"{tag}.cluster_id is required and must be a safe identifier")
        elif cluster_id in cluster_ids:
            errors.append(f"{tag}.cluster_id={cluster_id!r} is duplicated")
        else:
            cluster_ids.add(cluster_id)
        comp = str(cl.get("component") or "")
        if comp not in COMPONENTS:
            errors.append(f"{tag}.component='{comp}' invalid; must be one of {sorted(COMPONENTS)}")
            continue
        fm = str(cl.get("failure_mode") or "")
        if not FAILURE_MODE_RE.fullmatch(fm):
            errors.append(
                f"{tag}.failure_mode={fm!r} must be a short snake_case label"
            )
        opportunity = str(cl.get("action_opportunity") or "")
        if opportunity not in OPPORTUNITY_STATES:
            errors.append(
                f"{tag}.action_opportunity={opportunity!r} invalid; "
                f"must be one of {sorted(OPPORTUNITY_STATES)}"
            )
        patchability = str(cl.get("patchability") or "")
        if patchability not in PATCHABILITY_STATES:
            errors.append(
                f"{tag}.patchability={patchability!r} invalid; "
                f"must be one of {sorted(PATCHABILITY_STATES)}"
            )
        ev = cl.get("evidence_tasks")
        if not isinstance(ev, list) or not ev:
            errors.append(f"{tag}.evidence_tasks is empty — every cluster needs real evidence")
            ev = []
        elif len({str(task) for task in ev}) != len(ev):
            errors.append(f"{tag}.evidence_tasks contains duplicates")
        fake = [t for t in ev if str(t) not in failing_tasks]
        if fake:
            errors.append(f"{tag}.evidence_tasks {fake} did NOT fail this round — "
                          f"failing tasks are {sorted(failing_tasks, key=lambda x: int(x) if str(x).isdigit() else 0)}")
        for task in ev:
            forced_owner = normalized_forced_owners.get(str(task))
            if forced_owner is not None and comp != forced_owner:
                errors.append(
                    f"{tag}.component={comp!r} conflicts with deterministic owner "
                    f"{forced_owner!r} for evidence task {str(task)!r}"
                )
            forced_opportunity = normalized_forced_opportunities.get(str(task))
            if forced_opportunity is not None and opportunity != forced_opportunity:
                errors.append(
                    f"{tag}.action_opportunity={opportunity!r} conflicts with deterministic "
                    f"opportunity {forced_opportunity!r} for evidence task {str(task)!r}"
                )
        for fld in ("symptom", "failing_step", "opportunity_evidence", "evidence"):
            if not str(cl.get(fld) or "").strip():
                errors.append(f"{tag}.{fld} is required")

        fixes = cl.get("fixes")
        if comp in PATCHABLE_COMPONENTS:
            if opportunity != "present":
                errors.append(
                    f"{tag} component {comp} requires action_opportunity='present'"
                )
            if patchability != "memory":
                errors.append(f"{tag} component {comp} requires patchability='memory'")
        elif comp in NON_PATCH_OWNERS and patchability != "external":
            errors.append(f"{tag} component {comp} requires patchability='external'")
        elif comp == "CEILING" and patchability == "memory":
            errors.append(f"{tag} CEILING cannot claim patchability='memory'")
        if fixes and (opportunity != "present" or patchability != "memory"):
            errors.append(
                f"{tag} fixes require action_opportunity='present' and patchability='memory'"
            )
        if comp == "CEILING":
            if fixes:
                errors.append(f"{tag} is CEILING but has fixes — CEILING means no memory fix exists")
            continue
        if comp in NON_PATCH_OWNERS:
            if fixes != []:
                errors.append(f"{tag} is {comp} and must have fixes=[] (non-patchable owner)")
            continue
        if not isinstance(fixes, list) or not fixes:
            errors.append(f"{tag} (component {comp}) has no fixes")
            continue

        legal = ACTIONS_BY_COMPONENT[comp]
        for fi, fx in enumerate(fixes):
            ft = f"{tag}.fixes[{fi}]"
            if not isinstance(fx, dict):
                errors.append(f"{ft} must be an object")
                continue
            act = str(fx.get("action") or "")
            if act not in legal:
                errors.append(f"{ft}.action='{act}' illegal for component {comp}; "
                              f"legal: {sorted(legal)} (component-match rule)")
                continue
            if act in {"add_card", "edit_card", "remove_card"}:
                resource = ("card", str(fx.get("card_id") or ""))
            elif act in {"set_delivery", "edit_delivery", "remove_delivery"}:
                resource = ("delivery", str(fx.get("deliverer") or ""))
            elif act in {"set_checker", "remove_checker"}:
                resource = ("checker", str(fx.get("checker") or ""))
            elif act == "set_wm":
                resource = ("wm", str(fx.get("field") or ""))
            else:
                resource = ("manifest", act.removeprefix("set_"))
            previous = seen_targets.get(resource)
            if previous is not None:
                errors.append(f"{ft} conflicts with another {previous} fix for {resource}")
            else:
                seen_targets[resource] = act
            key = ledger_key(comp, act, _fix_target(fx))
            if key in blocked_keys:
                errors.append(f"{ft} repeats a combo already REJECTED with regressions: {key} "
                              f"— propose a different approach (do-not-repeat)")
            if act in ("add_card", "edit_card"):
                et = str(fx.get("em_type") or "")
                if et not in EM_TYPES:
                    errors.append(f"{ft}.em_type='{et}' must be one of {sorted(EM_TYPES)}")
                if not str(fx.get("card_id") or "").strip():
                    errors.append(f"{ft}.card_id is required")
                if et in {"procedure", "action_result"}:
                    trigger_event = str(fx.get("trigger_event") or "").strip()
                    if not trigger_event:
                        errors.append(f"{ft} {et} card requires trigger_event")
                    elif trigger_event not in TRIGGER_EVENTS:
                        errors.append(
                            f"{ft}.trigger_event={trigger_event!r} is not a framework "
                            f"event {sorted(TRIGGER_EVENTS)}"
                        )
                    trigger_tool = str(fx.get("trigger_tool") or "").strip()
                    if not trigger_tool:
                        errors.append(f"{ft} {et} card requires trigger_tool")
                    elif (trigger_tool == "*" or
                          not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", trigger_tool)):
                        errors.append(
                            f"{ft}.trigger_tool must be one exact safe tool name, not a wildcard"
                        )
                if et == "action_result":
                    planned_ar_cards.append(fx.get("card_id"))
                    if act == "add_card":
                        newly_added_action_result_cards.append({
                            "card_id": str(fx.get("card_id") or ""),
                            "event": str(fx.get("trigger_event") or ""),
                            "tool": str(fx.get("trigger_tool") or ""),
                            "where": ft,
                        })
                elif et:
                    planned_kn_cards.append(fx.get("card_id"))
                    if et == "procedure" and act == "add_card":
                        newly_added_procedure_cards.append({
                            "card_id": str(fx.get("card_id") or ""),
                            "event": str(fx.get("trigger_event") or ""),
                            "tool": str(fx.get("trigger_tool") or ""),
                            "where": ft,
                        })
                    elif et == "knowledge" and act == "add_card":
                        newly_added_knowledge_cards.append({
                            "card_id": str(fx.get("card_id") or ""),
                            "tool": str(fx.get("trigger_tool") or "").strip(),
                            "where": ft,
                        })
                if not str(fx.get("sketch") or "").strip():
                    errors.append(f"{ft}.sketch is required")
            elif act == "remove_card":
                if not str(fx.get("card_id") or "").strip():
                    errors.append(f"{ft}.card_id is required")
            elif act in ("set_delivery", "edit_delivery", "remove_delivery"):
                dl = str(fx.get("deliverer") or "")
                if dl not in DELIVERERS:
                    errors.append(f"{ft}.deliverer='{dl}' not a builtin {sorted(DELIVERERS)}")
                if act != "remove_delivery":
                    planned_deliverers.add(dl)
                    if not isinstance(fx.get("cfg"), dict):
                        errors.append(f"{ft}.cfg must be an exact JSON object")
                    else:
                        if dl in {"need_driven_retrieval", "embedding_retrieval"}:
                            configured_scoped_retrieval.append((ft, fx["cfg"]))
                            retrieval_cfgs_by_name.setdefault(dl, []).append(
                                fx["cfg"]
                            )
                        elif dl == "exemplar_bounce":
                            configured_exemplar_bounce.append((ft, fx["cfg"]))
                        if dl in DELIVERERS:
                            cfg_keys = _builtin_cfg_keys("DELIVERERS", dl)
                            if cfg_keys is not None:
                                unknown = sorted(set(fx["cfg"]) - cfg_keys)
                                if unknown:
                                    hint = ""
                                    if "at" in unknown:
                                        hint = (
                                            " NOTE: `at` is not a cfg key — put"
                                            " it as a top-level fix field "
                                            '("at": "<event>") and, in the '
                                            "manifest, as an entry-level "
                                            "sibling of `use:`/`cfg:`."
                                        )
                                    errors.append(
                                        f"{ft}.cfg key(s) {unknown} are invalid for "
                                        f"deliverer '{dl}' -> valid keys are "
                                        f"{sorted(cfg_keys)} (unknown keys crash at "
                                        "load). Do not invent cfg keys." + hint
                                    )
                    rebind = fx.get("at")
                    if rebind is not None:
                        if str(rebind) not in DELIVERER_TRIGGER_EVENTS:
                            errors.append(
                                f"{ft}.at='{rebind}' is not a dispatchable "
                                f"deliverer event {sorted(DELIVERER_TRIGGER_EVENTS)}"
                            )
                else:
                    planned_deliverers.discard(dl)
                if dl == "standing_inject":
                    warnings.append(f"{ft} standing_inject has high regression cost — "
                                    f"gate will judge, but consider a targeted deliverer")
            elif act in ("set_checker", "remove_checker"):
                ck = str(fx.get("checker") or "")
                if ck not in checker_caps:
                    errors.append(
                        f"{ft}.checker='{ck}' is not exposed by the current adapter; "
                        f"available: {sorted(checker_caps)}"
                    )
                if act == "set_checker" and not isinstance(fx.get("cfg"), dict):
                    errors.append(f"{ft}.cfg must be an exact JSON object")
                if act == "set_checker":
                    planned_checkers.add(ck)
                    if isinstance(fx.get("cfg"), dict):
                        configured_checker_cfgs.setdefault(ck, []).append(fx["cfg"])
                        if ck in checker_caps:
                            cfg_keys = _builtin_cfg_keys("CHECKERS", ck)
                            if cfg_keys is not None:
                                unknown = sorted(set(fx["cfg"]) - cfg_keys)
                                if unknown:
                                    errors.append(
                                        f"{ft}.cfg key(s) {unknown} are invalid for "
                                        f"checker '{ck}' -> valid keys are "
                                        f"{sorted(cfg_keys)} (unknown keys crash at "
                                        "load). Do not invent cfg keys."
                                    )
                else:
                    planned_checkers.discard(ck)
            elif act == "set_wm":
                fld = str(fx.get("field") or "")
                allowed_fields = {"entry_kind", "manager", "binding_key", "max_entries",
                                  "machine_id_pattern"}
                if fld not in allowed_fields:
                    errors.append(f"{ft}.field={fld!r} must be one of {sorted(allowed_fields)}")
                if fx.get("value") is None or str(fx.get("value")).strip() == "":
                    errors.append(f"{ft}.value is required")
                if fld == "entry_kind" and str(fx.get("value")) not in ENTRY_KINDS:
                    errors.append(f"{ft} entry_kind value must be one of {sorted(ENTRY_KINDS)}")
                elif fld == "entry_kind":
                    planned_entry_kind = str(fx.get("value"))
                if fld == "manager" and str(fx.get("value")) not in MANAGERS:
                    errors.append(f"{ft} manager value must be one of {sorted(MANAGERS)}")
                if fld in allowed_fields and fx.get("value") is not None:
                    planned_wm_values[fld] = str(fx.get("value"))
            elif act in {"set_board", "set_gate", "set_grounding", "set_feasibility"}:
                if not isinstance(fx.get("value"), dict):
                    errors.append(f"{ft}.value must be an exact JSON object")
                elif act == "set_grounding":
                    matcher = str(fx["value"].get("matcher") or "")
                    if matcher not in MATCHERS:
                        errors.append(
                            f"{ft}.value.matcher={matcher!r} must be one of "
                            f"{sorted(MATCHERS)}"
                        )
                    else:
                        planned_grounding_matcher = matcher

    # kernel-coupling closure: the projected package after this plan must not
    # select a mechanism whose kernel-required companion field is neither kept
    # from the base package nor declared by a fix in this plan.
    projected_matcher = planned_grounding_matcher or str(current_grounding_matcher or "")
    coupling = KERNEL_COUPLINGS.get(projected_matcher)
    if coupling is not None:
        required_field, coupling_message = coupling
        base_schema = current_wm_schema if isinstance(current_wm_schema, dict) else {}
        declared = required_field in planned_wm_values
        kept_from_base = bool(base_schema.get(required_field))
        if not declared and not kept_from_base:
            errors.append(coupling_message)

    # scoped-retrieval coupling closure: the kernel keys tool-scoped retrieval
    # on the card's trigger tool, so a knowledge card without trigger_tool can
    # never be selected by a scope_by_tool carrier.  When every projected
    # carrier that could serve knowledge cards is a retrieval deliverer this
    # plan itself configures as tool-scoped, such cards are inert — the defect
    # ma_lint later rejects as CARD HAS NO REACHABLE CARRIER.  (Carriers kept
    # from the base package with unknown cfg suppress this check; ma_lint
    # remains the backstop.)
    untooled_knowledge = [
        card for card in newly_added_knowledge_cards if not card["tool"]
    ]
    if untooled_knowledge:
        soft_carriers = planned_deliverers & {
            "standing_inject", "boundary_inject", "state_reminder",
        }
        projected_retrieval = planned_deliverers & {
            "need_driven_retrieval", "embedding_retrieval",
        }
        all_projected_scoped = bool(projected_retrieval) and all(
            name in retrieval_cfgs_by_name and all(
                cfg.get("scope_by_tool") is True
                for cfg in retrieval_cfgs_by_name[name]
            )
            for name in projected_retrieval
        )
        if not soft_carriers and all_projected_scoped:
            ids = [card["card_id"] for card in untooled_knowledge]
            errors.append(
                f"plan adds knowledge card(s) {ids} without trigger_tool, but "
                "every projected carrier is tool-scoped retrieval "
                "(scope_by_tool=true) and no standing_inject/boundary_inject/"
                "state_reminder carrier exists — those cards can never fire "
                "(ma_lint rejects them as CARD HAS NO REACHABLE CARRIER). "
                "Declare trigger_event+trigger_tool on each card, or plan an "
                "unscoped carrier for them."
            )

    # pairing closure
    if planned_ar_cards and "exemplar_bounce" not in planned_deliverers:
        errors.append(f"plan adds action_result card(s) {planned_ar_cards} but neither the "
                      f"package nor the plan provides `exemplar_bounce` delivery — those "
                      f"cards would never fire (pairing rule)")
    kn_deliverers = {"need_driven_retrieval", "embedding_retrieval", "standing_inject",
                     "state_reminder", "boundary_inject"}
    def checker_route_tools(checker_name: str) -> tuple[str, set[str]] | None:
        route = checker_caps.get(checker_name, {}).get("card_route")
        if not isinstance(route, dict):
            return None
        event = str(route.get("event") or "")
        tool_cfg = str(route.get("tool_cfg") or "")
        default_tool = str(route.get("default_tool") or "")
        cfgs = configured_checker_cfgs.get(checker_name) or [{}]
        tools = {
            str(cfg.get(tool_cfg) or default_tool)
            for cfg in cfgs
            if str(cfg.get(tool_cfg) or default_tool)
        }
        return event, tools

    checker_guides = {
        card["card_id"]
        for card in newly_added_procedure_cards
        for checker_name in planned_checkers
        if (
            (route := checker_route_tools(checker_name)) is not None
            and card["event"] == route[0]
            and card["tool"] in route[1]
        )
    }
    uncovered_kn_cards = [
        card_id for card_id in planned_kn_cards if card_id not in checker_guides
    ]
    if uncovered_kn_cards and not (planned_deliverers & kn_deliverers):
        warnings.append(
            f"plan adds knowledge/procedure card(s) {uncovered_kn_cards} without a "
            "retrieval-family deliverer — they may never surface"
        )
    retrieval_procedures = [
        card for card in newly_added_procedure_cards
        if card["event"] == "intent_recorded"
    ]
    retrieval_procedure_ids = [card["card_id"] for card in retrieval_procedures]
    if retrieval_procedures:
        for ft, cfg in configured_scoped_retrieval:
            if cfg.get("scope_by_tool") is not True:
                errors.append(
                    f"{ft}.cfg.scope_by_tool must be true when adding procedure card(s) "
                    f"{retrieval_procedure_ids}"
                )
            max_per_episode = cfg.get("max_per_episode")
            if (isinstance(max_per_episode, bool) or
                    not isinstance(max_per_episode, int) or max_per_episode != 1):
                errors.append(
                    f"{ft}.cfg.max_per_episode must be integer 1 when adding procedure "
                    f"card(s) {retrieval_procedure_ids}"
                )
            if cfg.get("settle") is not False:
                errors.append(
                    f"{ft}.cfg.settle must be false when adding procedure card(s) "
                    f"{retrieval_procedure_ids}"
                )
    if newly_added_action_result_cards:
        action_result_ids = [
            card["card_id"] for card in newly_added_action_result_cards
        ]
        for ft, cfg in configured_exemplar_bounce:
            if cfg.get("exact_only") is not True:
                errors.append(
                    f"{ft}.cfg.exact_only must be true when adding action_result card(s) "
                    f"{action_result_ids}"
                )

    # Formal runs pass a closed-world tool capability profile.  This catches a
    # plan whose card/delivery pairing looks valid on paper but can never obtain
    # the tool signal required by the selected carrier.
    known = set(known_tools) if known_tools is not None else None
    mutating = set(mutating_tools) if mutating_tools is not None else None
    tool_aware = (
        set(tool_aware_entry_kinds)
        if tool_aware_entry_kinds is not None else None
    )
    scoped_retrieval_selected = any(
        cfg.get("scope_by_tool") is True
        for _, cfg in configured_scoped_retrieval
    )
    for card in newly_added_action_result_cards:
        where, event, tool = card["where"], card["event"], card["tool"]
        if event and event != "pre_write":
            errors.append(
                f"{where} action_result cards require trigger_event='pre_write'; "
                f"got {event!r}"
            )
        if known is not None and tool and tool not in known:
            errors.append(f"{where}.trigger_tool={tool!r} is not a domain tool")
        elif mutating is not None and tool and tool not in mutating:
            errors.append(
                f"{where}.trigger_tool={tool!r} is non-mutating, so the pre_write "
                "carrier cannot observe it"
            )

    if scoped_retrieval_selected:
        for card in retrieval_procedures:
            where, tool = card["where"], card["tool"]
            if tool_aware is not None and planned_entry_kind not in tool_aware:
                errors.append(
                    f"{where} uses tool-scoped retrieval, but projected "
                    f"wm.entry_kind={planned_entry_kind!r} drops tool metadata; "
                    "add a W/set_wm entry_kind fix or choose a different carrier"
                )
            if known is not None and tool and tool not in known:
                errors.append(f"{where}.trigger_tool={tool!r} is not a domain tool")
            elif mutating is not None and tool and tool not in mutating:
                errors.append(
                    f"{where}.trigger_tool={tool!r} is non-mutating and cannot enter "
                    "the write-tool ledger used by scoped retrieval; use a carrier "
                    "at the actual decision event (for example draft_ready)"
                )
    for card in newly_added_procedure_cards:
        if card["event"] not in CHECKER_TRIGGER_EVENTS:
            continue
        matching_routes = [
            (checker_name, route)
            for checker_name in planned_checkers
            if (route := checker_route_tools(checker_name)) is not None
            and route[0] == card["event"]
        ]
        if not matching_routes:
            errors.append(
                f"{card['where']} declares trigger_event={card['event']!r} but no "
                "configured adapter checker can surface that guide"
            )
            continue
        reachable_tools = set().union(*(route[1] for _, route in matching_routes))
        if card["tool"] not in reachable_tools:
            errors.append(
                f"{card['where']}.trigger_tool={card['tool']!r} does not match the "
                f"configured adapter checker route {sorted(reachable_tools)}"
            )
    return errors, warnings


def load_and_validate(plan_path: Path, failing_tasks: set, blocked_keys: set,
                      pkg_deliverers: set | None = None,
                      expected_round: int | None = None,
                      forced_owners: dict[str, str] | None = None,
                      forced_opportunities: dict[str, str] | None = None,
                      current_entry_kind: str | None = None,
                      tool_aware_entry_kinds: set[str] | None = None,
                      known_tools: set[str] | None = None,
                      mutating_tools: set[str] | None = None,
                      pkg_checkers: set[str] | None = None,
                      checker_capabilities: dict[str, dict[str, object]] | None = None,
                      current_grounding_matcher: str | None = None,
                      current_wm_schema: dict | None = None):
    """Returns (plan|None, errors, warnings)."""
    if not plan_path.exists():
        return None, [f"{plan_path.name} was not written"], []
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as e:
        return None, [f"{plan_path.name} is not valid JSON: {e}"], []
    errors, warnings = validate_plan(
        plan, failing_tasks, blocked_keys, pkg_deliverers,
        expected_round=expected_round, forced_owners=forced_owners,
        forced_opportunities=forced_opportunities,
        current_entry_kind=current_entry_kind,
        tool_aware_entry_kinds=tool_aware_entry_kinds,
        known_tools=known_tools,
        mutating_tools=mutating_tools,
        pkg_checkers=pkg_checkers,
        checker_capabilities=checker_capabilities,
        current_grounding_matcher=current_grounding_matcher,
        current_wm_schema=current_wm_schema,
    )
    return plan, errors, warnings
