"""Offline mechanism-ignition probe for generated Skill Memory packages.

The probe executes the real builtin deliverer/checker objects against a
synthetic ledger.  It calls no language model and reads no task answer.  Its
only claim is wiring: whether each card can physically reach model context
through at least one configured carrier.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from recuris import builtin
from recuris.builtin.embed_retrieval import EmbeddingRetrieval
from recuris.em.store import EMStore
from recuris.events import Event
from recuris.metaagent.reachability import (
    dispatched_deliverer_events_for,
    tool_capabilities_for,
)
from recuris.runtime import DELIVERER_EVENTS, TurnRuntime
from recuris.skillmemory import load_skill_memory
from recuris.spi import (
    CoverWithDoc,
    ExemplarBounceAction,
    InjectNote,
    TriggerContext,
)
from recuris.wm.ledger import Ledger

_TOKEN_RE = re.compile(r"[A-Za-z0-9_#]+")


def _query_text(card) -> str:
    tokens = _TOKEN_RE.findall(f"{card.id} {card.body[:1200]}")
    return " ".join(tokens) or f"guide {card.id}"


def _ledger_for(sm, card, write_tools: set[str]) -> Ledger:
    ledger = Ledger(sm.wm_schema)
    tool = str(card.trigger_tool or "")
    if tool == "*" or not tool:
        tool = next(iter(sorted(write_tools)), "")
    ledger.apply_model_update(
        [{
            "description": _query_text(card),
            "tool": tool,
            "params": {},
            "function": card.type,
            "needs_external": True,
        }],
        write_tools,
    )
    return ledger


class _DeterministicEmbedder:
    """Small local hash embedder used only to exercise retrieval wiring."""

    dimension = 128

    def embed(self, texts):
        import numpy as np

        rows = []
        for text in texts:
            row = np.zeros(self.dimension, dtype=float)
            for token in _TOKEN_RE.findall(str(text).lower()):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                row[int.from_bytes(digest[:4], "big") % self.dimension] += 1.0
            if not row.any():
                row[0] = 1.0
            rows.append(row.tolist())
        return rows


def _prepare_embedding(deliverer: EmbeddingRetrieval, one_em: EMStore) -> None:
    import numpy as np

    docs = [entry for entry in one_em.entries
            if entry.type in deliverer.em_types]
    embedder = _DeterministicEmbedder()
    texts = [f"{entry.id}\n{entry.body[:deliverer.head_chars]}" for entry in docs]
    vectors = np.stack([np.asarray(row, float) for row in embedder.embed(texts)])
    vectors = vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)
    deliverer._embedder = embedder
    deliverer._mat = vectors
    deliverer._doc_ids = [entry.id for entry in docs]
    deliverer._doc_bodies = {entry.id: entry.body for entry in docs}
    deliverer._doc_tools = {
        entry.id: entry.trigger_tool for entry in docs
    }


def _delivery_contains_card(actions, card, event: str) -> bool:
    if event == Event.PRE_WRITE.value:
        for action in actions:
            if not isinstance(action, ExemplarBounceAction):
                continue
            if any(card.body in str(text)
                   for text in action.results_by_call.values()):
                return True
        return False
    for action in actions:
        if isinstance(action, CoverWithDoc) and action.doc_id == card.id:
            return True
        if isinstance(action, InjectNote) and card.body in action.text:
            return True
    return False


def _probe_deliverer(
    spec: dict,
    card,
    sm,
    one_em: EMStore,
    write_tools: set[str],
    dispatched_events: frozenset[str] | set[str] | None = None,
) -> dict:
    name = str(spec.get("use") or "")
    cfg = spec.get("cfg") or {}
    if dispatched_events is None:
        dispatched_events = frozenset(DELIVERER_EVENTS)
    try:
        deliverer = builtin.make_deliverer(name, cfg)
        binding_at = spec.get("at")
        events = (
            [str(binding_at)] if binding_at is not None
            else list(getattr(deliverer, "events", ()) or ())
        )
        if not events:
            return {"carrier": name, "activated": False,
                    "detail": "deliverer has no bound event"}
        undispatched = [
            event for event in events
            if event in DELIVERER_EVENTS and event not in dispatched_events
        ]
        if not any(
            event in DELIVERER_EVENTS and event in dispatched_events
            for event in events
        ):
            return {
                "carrier": name,
                "events": events,
                "activated": False,
                "detail": (
                    "bound only to events this benchmark's runtime never "
                    f"raises: {sorted(undispatched)}; rebind with `at:` to a "
                    "dispatched event"
                ),
            }
        one_ledger = _ledger_for(sm, card, write_tools)
        if (name == "state_reminder" and one_ledger.pending() and
                card.id == str(cfg.get("authorized_doc") or
                               "authorized_execute_check")):
            one_ledger.pending()[0].auth = {"quote": "synthetic yes"}
        if isinstance(deliverer, EmbeddingRetrieval):
            _prepare_embedding(deliverer, one_em)
        tool = str(card.trigger_tool or "")
        if tool == "*":
            tool = next(iter(sorted(write_tools)), "")
        call = SimpleNamespace(
            name=tool,
            id="synthetic-call-1",
            arguments={},
        )
        for event in events:
            if event not in DELIVERER_EVENTS or event not in dispatched_events:
                continue
            ctx = TriggerContext(
                event=event,
                turn=1,
                incoming_kind="user",
                incoming_text="synthetic activation probe",
                extra={
                    "draft_calls": [call],
                    "write_tools": set(write_tools),
                    "delivered_docs": set(),
                },
            )
            actions = deliverer.select(ctx, one_ledger, one_em, draft=None)
            if _delivery_contains_card(actions, card, event):
                return {
                    "carrier": name,
                    "event": event,
                    "activated": True,
                    "action_types": [
                        type(action).__name__ for action in actions
                    ],
                }
        return {
            "carrier": name,
            "events": events,
            "activated": False,
            "detail": "real selector produced no runtime-consumable action for this card",
        }
    except Exception as exc:
        return {
            "carrier": name,
            "activated": False,
            "detail": f"{type(exc).__name__}: {exc}",
        }


def _probe_checker(
    spec: dict,
    card,
    sm,
    one_em: EMStore,
    write_tools: set[str],
) -> dict:
    name = str(spec.get("use") or "")
    cfg = spec.get("cfg") or {}
    try:
        checker = builtin.make_checker(name, cfg)
        if hasattr(checker, "set_em"):
            checker.set_em(one_em)
        events = (
            [str(spec["at"])] if spec.get("at") is not None
            else list(getattr(checker, "events", ()) or ())
        )
        if Event.DRAFT_READY.value not in events:
            return {"carrier": f"checker:{name}", "activated": False,
                    "detail": "checker is not bound at draft_ready"}
        ledger = _ledger_for(sm, card, write_tools)
        call = SimpleNamespace(
            name=str(card.trigger_tool or ""),
            id="synthetic-call-1",
            arguments={},
        )
        ctx = TriggerContext(
            event=Event.DRAFT_READY.value,
            turn=1,
            incoming_kind="user",
            incoming_text="synthetic activation probe",
            extra={"draft_calls": [call]},
        )
        bounce = checker.inspect(
            ctx, ledger, "synthetic draft", draft_has_calls=True,
        )
        correction = str(getattr(bounce, "correction", "") or "")
        activated = bounce is not None and card.body in correction
        return {
            "carrier": f"checker:{name}",
            "event": Event.DRAFT_READY.value,
            "activated": activated,
            "action_types": ["Bounce"] if bounce is not None else [],
            "detail": (
                "" if activated else
                "checker did not bounce with this card in its correction"
            ),
        }
    except Exception as exc:
        return {
            "carrier": f"checker:{name}",
            "activated": False,
            "detail": f"{type(exc).__name__}: {exc}",
        }


def _probe_checker_mechanism(
    spec: dict,
    sm,
    write_tools: set[str],
) -> dict:
    """Ignite a checker itself, even when the package has no guide card."""
    name = str(spec.get("use") or "")
    cfg = spec.get("cfg") or {}
    try:
        checker = builtin.make_checker(name, cfg)
        if hasattr(checker, "set_em"):
            checker.set_em(sm.em)
        events = (
            [str(spec["at"])] if spec.get("at") is not None
            else list(getattr(checker, "events", ()) or ())
        )
        if Event.DRAFT_READY.value not in events:
            return {
                "kind": "checker",
                "name": name,
                "activated": False,
                "detail": "checker is not bound at draft_ready",
            }
        executable_tool = next(iter(sorted(write_tools)), "")
        probe = SimpleNamespace(
            id="checker_mechanism_probe",
            body="synthetic checker mechanism probe",
            trigger_tool=executable_tool,
            type="procedure",
        )
        ledger = _ledger_for(sm, probe, write_tools)
        calls = []
        incoming = "synthetic activation probe"
        draft = "It has been successfully completed."
        draft_has_calls = False
        if name == "anti_escalation":
            transfer_tool = str(
                cfg.get("transfer_tool") or "transfer_to_human_agents"
            )
            calls = [
                SimpleNamespace(
                    name=transfer_tool,
                    id="synthetic-call-1",
                    arguments={},
                )
            ]
            draft = "synthetic transfer draft"
            draft_has_calls = True
        elif name == "execution_gate":
            if ledger.pending():
                ledger.pending()[0].auth = {"quote": "synthetic yes"}
            draft = "Yes, I can do that."
        elif name == "truth_protocol" and str(cfg.get("level") or "full") == "v2only":
            incoming = "yes, please proceed"
            draft = "I will help."
        ctx = TriggerContext(
            event=Event.DRAFT_READY.value,
            turn=1,
            incoming_kind="user",
            incoming_text=incoming,
            extra={"draft_calls": calls},
        )
        bounce = checker.inspect(
            ctx, ledger, draft, draft_has_calls=draft_has_calls,
        )
        return {
            "kind": "checker",
            "name": name,
            "event": Event.DRAFT_READY.value,
            "activated": bounce is not None,
            "action_types": ["Bounce"] if bounce is not None else [],
            "detail": (
                "" if bounce is not None else
                "real checker produced no Bounce under its synthetic failure condition"
            ),
        }
    except Exception as exc:
        return {
            "kind": "checker",
            "name": name,
            "activated": False,
            "detail": f"{type(exc).__name__}: {exc}",
        }


def probe_package(
    package: str | Path, domain: str, benchmark: str = "tau2"
) -> dict:
    package = Path(package).resolve()
    manifest = yaml.safe_load(
        (package / "manifest.yaml").read_text(encoding="utf-8")
    ) or {}
    capabilities = tool_capabilities_for(benchmark, domain)
    write_tools = {
        name for name, record in capabilities.items()
        if record.get("mutates_state") is True
    }
    try:
        sm = load_skill_memory(package)
        # Instantiate the real runtime once so invalid role/event bindings fail
        # exactly as they would in a downstream episode.
        TurnRuntime(sm, write_tools)
    except Exception as exc:
        return {
            "schema_version": 1,
            "package": str(package),
            "domain": domain,
            "offline": True,
            "passed": False,
            "package_error": f"{type(exc).__name__}: {exc}",
            "cards": [],
        }

    dispatched_events = dispatched_deliverer_events_for(benchmark)
    card_records = []
    for card in sm.em.entries:
        one_em = EMStore([card])
        attempts = [
            _probe_deliverer(spec, card, sm, one_em, write_tools,
                             dispatched_events)
            for spec in (manifest.get("delivery") or [])
            if isinstance(spec, dict)
        ]
        attempts += [
            _probe_checker(spec, card, sm, one_em, write_tools)
            for spec in (manifest.get("checkers") or [])
            if isinstance(spec, dict)
        ]
        activated = any(attempt.get("activated") is True for attempt in attempts)
        card_records.append({
            "id": card.id,
            "type": card.type,
            "event": card.trigger_event,
            "tool": card.trigger_tool,
            "activated": activated,
            "attempts": attempts,
        })
    mechanism_records = [
        _probe_checker_mechanism(spec, sm, write_tools)
        for spec in (manifest.get("checkers") or [])
        if isinstance(spec, dict)
    ]
    return {
        "schema_version": 1,
        "package": str(package),
        "domain": domain,
        "offline": True,
        "network_calls": 0,
        "model_calls": 0,
        "write_tool_capabilities": sorted(write_tools),
        "dispatched_deliverer_events": sorted(dispatched_events),
        "passed": (
            all(record["activated"] for record in card_records)
            and all(record["activated"] for record in mechanism_records)
        ),
        "cards": card_records,
        "mechanisms": mechanism_records,
    }


def validate_retrieval_cases(
    spec: dict,
    expected_target_card_ids: set[str] | None = None,
) -> list[str]:
    """Validate the small, model-authored retrieval test contract."""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["activation_cases.json must contain one JSON object"]
    if spec.get("schema_version") != 1:
        errors.append("activation_cases.schema_version must be 1")

    raw_targets = spec.get("target_card_ids")
    if not isinstance(raw_targets, list) or not raw_targets:
        errors.append("activation_cases.target_card_ids must be a non-empty list")
        targets: list[str] = []
    else:
        targets = [str(value) for value in raw_targets]
        if len(set(targets)) != len(targets):
            errors.append("activation_cases.target_card_ids contains duplicates")
        for target in targets:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", target):
                errors.append(f"invalid target_card_id: {target!r}")
    if (expected_target_card_ids is not None and
            set(targets) != set(expected_target_card_ids)):
        errors.append(
            "activation_cases.target_card_ids must exactly match the reviewed "
            f"retrieval-card fixes: {sorted(expected_target_card_ids)}"
        )

    cases = spec.get("cases")
    if not isinstance(cases, list):
        return errors + ["activation_cases.cases must be a list"]
    if not 5 <= len(cases) <= 12:
        errors.append("activation_cases.cases must contain 5..12 cases")
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()
    positives = 0
    negatives = 0
    for index, case in enumerate(cases):
        tag = f"activation_cases.cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{tag} must be an object")
            continue
        query_id = str(case.get("query_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", query_id):
            errors.append(f"{tag}.query_id is invalid")
        elif query_id in seen_ids:
            errors.append(f"{tag}.query_id is duplicated")
        seen_ids.add(query_id)
        query = str(case.get("query") or "").strip()
        if not query or len(query) > 500:
            errors.append(f"{tag}.query must contain 1..500 characters")
        elif query in seen_queries:
            errors.append(f"{tag}.query is duplicated")
        seen_queries.add(query)
        expected = case.get("expected_activation")
        if not isinstance(expected, bool):
            errors.append(f"{tag}.expected_activation must be boolean")
        elif expected:
            positives += 1
        else:
            negatives += 1
    if positives < 2:
        errors.append("activation_cases requires at least two positive cases")
    if negatives < 3:
        errors.append("activation_cases requires at least three negative cases")
    return errors


def probe_retrieval_cases(
    package: str | Path, spec: dict, domain: str,
    benchmark: str = "tau2",
) -> dict:
    """Run positive and negative queries through real lexical retrieval."""
    package = Path(package).resolve()
    errors = validate_retrieval_cases(spec)
    report = {
        "schema_version": 1,
        "package": str(package),
        "offline": True,
        "network_calls": 0,
        "model_calls": 0,
        "probes": [],
    }
    if errors:
        report.update({"passed": False, "errors": errors})
        return report

    targets = set(str(value) for value in spec["target_card_ids"])
    try:
        manifest = yaml.safe_load(
            (package / "manifest.yaml").read_text(encoding="utf-8")
        ) or {}
        sm = load_skill_memory(package)
        capabilities = tool_capabilities_for(benchmark, domain)
        write_tools = {
            name for name, record in capabilities.items()
            if record.get("mutates_state") is True
        }
        TurnRuntime(sm, write_tools)
        existing_cards = {str(card.id) for card in sm.em.entries}
        missing = sorted(targets - existing_cards)
        if missing:
            raise ValueError(f"target cards are absent from the package: {missing}")
        delivery_specs = [
            item for item in (manifest.get("delivery") or [])
            if isinstance(item, dict) and item.get("use") == "need_driven_retrieval"
        ]
        if not delivery_specs:
            raise ValueError("package has no need_driven_retrieval carrier")
        dispatched_events = dispatched_deliverer_events_for(benchmark)
        binding_events: set[str] = set()
        for item in delivery_specs:
            if item.get("at") is not None:
                binding_events.add(str(item["at"]))
            else:
                binding_events.update(
                    builtin.make_deliverer(
                        "need_driven_retrieval", item.get("cfg") or {}
                    ).events or ()
                )
        live_events = sorted(
            event for event in binding_events
            if event in DELIVERER_EVENTS and event in dispatched_events
        )
        if not live_events:
            raise ValueError(
                "need_driven_retrieval carrier is bound only to events this "
                f"benchmark's runtime never raises: {sorted(binding_events)}; "
                "rebind with `at:` to a dispatched event"
            )
        probe_event = live_events[0]
        def run_cases(deliverers) -> list[dict]:
            probes = []
            for case in spec["cases"]:
                query = str(case["query"])
                ledger = Ledger(sm.wm_schema)
                ledger.apply_model_update([{"description": query}], set())
                ctx = TriggerContext(
                    event=probe_event,
                    turn=1,
                    incoming_kind="user",
                    incoming_text=query,
                    extra={"delivered_docs": set()},
                )
                selected: set[str] = set()
                for deliverer in deliverers:
                    for action in deliverer.select(ctx, ledger, sm.em, draft=None):
                        if isinstance(action, CoverWithDoc):
                            selected.add(str(action.doc_id))
                activated = bool(targets & selected)
                expected = bool(case["expected_activation"])
                probes.append({
                    "query_id": str(case["query_id"]),
                    "query": query,
                    "expected_activation": expected,
                    "selected": sorted(selected),
                    "activated": activated,
                    "passed": activated == expected,
                })
            return probes

        deliverers = [
            builtin.make_deliverer("need_driven_retrieval", item.get("cfg") or {})
            for item in delivery_specs
        ]
        report["probes"] = run_cases(deliverers)

        # A single lexical carrier has one direct precision knob. Show the
        # smallest passing value as deterministic feedback; the Meta-Agent still
        # owns the actual plan/package correction.
        if (not all(probe["passed"] for probe in report["probes"]) and
                len(delivery_specs) == 1):
            sweep = []
            for threshold in range(1, 9):
                cfg = copy.deepcopy(delivery_specs[0].get("cfg") or {})
                cfg["min_overlap"] = threshold
                probes = run_cases([
                    builtin.make_deliverer("need_driven_retrieval", cfg)
                ])
                sweep.append({
                    "min_overlap": threshold,
                    "passed": all(probe["passed"] for probe in probes),
                    "case_results": {
                        probe["query_id"]: probe["activated"] for probe in probes
                    },
                })
            report["min_overlap_sweep"] = sweep
            passing = [row["min_overlap"] for row in sweep if row["passed"]]
            if passing:
                report["suggested_min_overlap"] = min(passing)
    except Exception as exc:
        report.update({
            "passed": False,
            "errors": [f"{type(exc).__name__}: {exc}"],
        })
        return report

    failed = [probe for probe in report["probes"] if not probe["passed"]]
    report["passed"] = not failed
    report["errors"] = [
        f"{probe['query_id']}: expected_activation="
        f"{str(probe['expected_activation']).lower()}, selected={probe['selected']}, "
        f"query={probe['query']!r}"
        for probe in failed
    ]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--benchmark", default="tau2",
                        choices=["tau2", "tb21"])
    parser.add_argument("--out")
    args = parser.parse_args()
    report = probe_package(args.pkg, args.domain, args.benchmark)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if report.get("passed") else 1)


if __name__ == "__main__":
    main()
