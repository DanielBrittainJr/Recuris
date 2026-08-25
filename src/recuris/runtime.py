"""TurnRuntime — the invariant main loop, dispatching bound strategies.

The ORDER of phases below is engine law (identical to the parity-verified
v0.1 sequence); WHAT runs at each phase comes from the Skill Memory package's
bindings. Discipline surfaces (evidence admission, synthetic-id registration,
replay safety, sanitation, cap/permissions) are executed HERE — strategies
only propose.

    ground -> [turn_start deliverers] -> WM manager -> [intent_recorded
    deliverers] -> render -> draft -> [draft_ready checkers] -> commit ->
    [pre_write deliverers] -> re-ground -> sanitize -> out
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Protocol, Sequence

from loguru import logger

from recuris.board import BoardRenderer
from recuris.errors import ProfileError
from recuris.events import Event
from recuris.fingerprint import Fingerprint
from recuris.grounding import (
    DeliveryReceipt,
    DeliveryReceiptMatcher,
    GroundingKernel,
    ReceiptBindingMatcher,
    ToolReceipt,
)
from recuris.guards.sanitizer import Sanitizer
from recuris.skillmemory import SkillMemory, load_skill_memory
from recuris.spi import (
    CoverWithDoc,
    ExemplarBounceAction,
    InjectNote,
    TriggerContext,
)
from recuris.wm import render as wmrender
from recuris.wm.ledger import Ledger


class HarnessPort(Protocol):
    """Environment plumbing the adapter implements (unchanged from v0.1)."""

    def incoming_kind(self, incoming: Any) -> str: ...
    def user_text(self, incoming: Any) -> str: ...
    def extract_receipts(self) -> list[ToolReceipt]: ...
    def genuine_user_texts(self) -> list[str]: ...
    def read_tool_results(self) -> dict: ...  # {tool_name: [parsed_json,...]} for the oracle
    def ledger_llm(self, instruction: str) -> str: ...
    def llm_draft(self, wm_text: str) -> Any: ...
    def redraft_with_correction(self, wm_text: str, correction: str) -> Any: ...
    def bounce_and_redraft(self, draft: Any, results_by_call: dict) -> Any: ...
    def commit(self, draft: Any) -> None: ...
    def draft_content(self, draft: Any) -> str: ...
    def draft_tool_calls(self, draft: Any) -> Sequence[Any]: ...
    def set_draft_content(self, draft: Any, text: str) -> None: ...


@dataclass
class RuntimeState:
    ledger: Ledger
    fp: Fingerprint
    consumed: set = field(default_factory=set)
    synthetic_ids: set = field(default_factory=set)
    delivered_docs: set = field(default_factory=set)   # (entry_id, doc_id)
    turn: int = 0


# events the invariant loop actually dispatches (audit fix #4: binding to any
# other event is a startup ProfileError, not a silent no-op)
DISPATCHED_EVENTS = {
    Event.TURN_START.value,
    Event.INTENT_RECORDED.value,
    Event.DRAFT_READY.value,
    Event.PRE_WRITE.value,
}
# dispatch is ROLE-SPECIFIC (bug fix): run_turn dispatches deliverers only at
# turn_start / intent_recorded / pre_write, and checkers only at draft_ready.
# Validating both roles against the union above let a checker bound to pre_write
# (or a deliverer bound to draft_ready) pass startup yet never fire — precisely
# the silent no-op the startup guard is supposed to eliminate.
DELIVERER_EVENTS = {
    Event.TURN_START.value,
    Event.INTENT_RECORDED.value,
    Event.PRE_WRITE.value,
}
CHECKER_EVENTS = {Event.DRAFT_READY.value}


def _build_matcher(sm: SkillMemory, write_tools: set[str]):
    """Audit fix #1/#3: matcher is a real SPI slot (builtin or local:), and
    receipt_binding_match REQUIRES an explicit binding_key (no retail default)."""
    name = sm.matcher_name
    if name == "receipt_binding_match":
        if not sm.wm_schema.match.binding_key:
            raise ProfileError(
                f"{sm.name}: matcher 'receipt_binding_match' requires an explicit "
                "wm.schema.binding_key (kernel has no domain default)"
            )
        return ReceiptBindingMatcher(sm.wm_schema.match, write_tools)
    if name == "delivery_receipt":
        return DeliveryReceiptMatcher()
    if name.startswith("local:"):
        from recuris.skillmemory import _load_local

        inst = _load_local(sm.root, name, None)
        if not callable(getattr(inst, "match", None)) or not callable(
            getattr(inst, "relevant", None)
        ):
            raise ProfileError(f"{sm.name}: local matcher lacks match()/relevant()")
        return inst
    raise ProfileError(f"unknown grounding matcher '{name}'")


class TurnRuntime:
    def __init__(self, sm: SkillMemory, write_tools: set[str]):
        self.sm = sm
        self.write_tools = set(write_tools)
        self.sanitizer = Sanitizer(sm.wm_schema.board_marker)
        self.board = BoardRenderer(
            sm.wm_schema.board_marker,
            stay_notice=getattr(sm, "stay_notice", False),
            stay_notice_text=(getattr(sm, "stay_notice_text", "") or
                              __import__("recuris.board", fromlist=["DEFAULT_STAY_NOTICE"]).DEFAULT_STAY_NOTICE),
        )
        self.matcher = _build_matcher(sm, self.write_tools)
        self.oracle = getattr(sm, "oracle", None)  # ⑥ optional feasibility oracle
        # bindings: event -> strategies (manifest `at` overrides strategy default)
        self.deliverer_bindings: dict[str, list] = defaultdict(list)
        for d, at in sm.deliverers:
            for ev in ([at] if at else list(getattr(d, "events", ()) or ())):
                if ev not in DELIVERER_EVENTS:
                    raise ProfileError(
                        f"{sm.name}: deliverer {d.name} bound to event '{ev}' that "
                        f"never dispatches a deliverer (valid: {sorted(DELIVERER_EVENTS)})"
                    )
                self.deliverer_bindings[ev].append(d)
        self.checker_bindings: dict[str, list] = defaultdict(list)
        for c, at in sm.checkers:
            if hasattr(c, "set_em"):  # checkers that deliver EM on bounce
                c.set_em(sm.em)
            for ev in ([at] if at else list(getattr(c, "events", ()) or ())):
                if ev not in CHECKER_EVENTS:
                    raise ProfileError(
                        f"{sm.name}: checker {c.name} bound to event '{ev}' that "
                        f"never dispatches a checker (valid: {sorted(CHECKER_EVENTS)})"
                    )
                self.checker_bindings[ev].append(c)

    @classmethod
    def load(cls, skill_memory_path: str, write_tools: set[str]) -> "TurnRuntime":
        """A new task means a new path (the user-facing loading contract)."""
        return cls(load_skill_memory(skill_memory_path), write_tools)

    # ── state factory / harness-facing views (unchanged surface) ─────────
    def new_state(self, sim_tag: str = "") -> RuntimeState:
        return RuntimeState(ledger=Ledger(self.sm.wm_schema), fp=Fingerprint(sim_tag))

    def pending_lines(self, rs: RuntimeState) -> list[str]:
        """Terminal-gate view. gate_lines=authorized_only (v3): the gate can
        only push work the customer verifiably consented to."""
        kind = rs.ledger.entry_kind
        entries = rs.ledger.pending()
        if getattr(self.sm, "gate_lines", "all_pending") == "authorized_only":
            kept = [e for e in entries if getattr(e, "auth", None)]
            if len(kept) < len(entries):
                # Direct measurement of a structural risk: the gate suppressed a
                # pending entry for lack of authorization. When the user confirms
                # and hangs up in the same turn, authorization can never be
                # verified in time, so this counter is the missed-rescue point.
                rs.fp.event(
                    "gate_suppressed_unauthorized",
                    n=len(entries) - len(kept), turn=rs.turn,
                )
            entries = kept
        return [kind.pending_line(e, rs.ledger.schema) for e in entries]

    def board_text(self, rs: RuntimeState) -> Optional[str]:
        # audit fix #14: runtime is the single owner of the board switch - a
        # harness env flag alone can never surface a board the package disabled.
        if not self.sm.status_board:
            return None
        return self.board.render(rs.ledger)

    # ── the invariant turn ───────────────────────────────────────────────
    def run_turn(self, rs: RuntimeState, port: HarnessPort, incoming: Any) -> Any:
        rs.turn += 1
        kind_in = port.incoming_kind(incoming)
        text_in = port.user_text(incoming)

        def ctx(event: str, **extra: Any) -> TriggerContext:
            return TriggerContext(
                event=event, turn=rs.turn, incoming_kind=kind_in,
                incoming_text=text_in, extra=extra,
            )

        # 1) harness grounding first.
        self._ground(rs, port, tag="turn_start")
        if kind_in == "system_check":
            rs.fp.count("gate_nudge_received")

        notes: list[str] = []
        notes += self._deliver(Event.TURN_START.value, ctx(Event.TURN_START.value), rs, port)

        # 2) WM maintenance (manager decides cadence; kernel applies law).
        if self.sm.manager.should_update(
            ctx(Event.TURN_START.value, has_pending=bool(rs.ledger.pending()))
        ):
            self._update_ledger(rs, port)
            notes += self._deliver(
                Event.INTENT_RECORDED.value,
                ctx(Event.INTENT_RECORDED.value, delivered_docs=rs.delivered_docs),
                rs, port,
            )

        # 3) per-turn context recompilation.
        wm_text = wmrender.render_wm(rs.ledger, board_notice=self.sm.board_notice)
        if notes:
            wm_text = wm_text + "\n\n" + "\n\n".join(notes)

        # 4) draft.
        draft = port.llm_draft(wm_text)

        # 5) draft_ready checkers — checkpoint ①: rejected draft never commits.
        for checker in self.checker_bindings.get(Event.DRAFT_READY.value, []):
            try:  # audit fix #11
                bounce = checker.inspect(
                    ctx(Event.DRAFT_READY.value,
                        draft_calls=list(port.draft_tool_calls(draft))),
                    rs.ledger,
                    port.draft_content(draft),
                    bool(port.draft_tool_calls(draft)),
                )
            except Exception as e:
                logger.warning(f"[skfw.check] {checker.name} raised: {e}")
                rs.fp.event("checker_error", name=checker.name, turn=rs.turn)
                continue
            if bounce is not None:
                logger.info(f"[skfw.check] draft rejected: {bounce.reason[:80]}")
                rs.fp.event(
                    "truth_bounce" if bounce.checker.startswith("truth") else "checker_bounce",
                    checker=bounce.checker, reason=bounce.reason[:120], turn=rs.turn,
                )
                self._debug_dump(rs, f"bounce:{bounce.checker}")
                draft = port.redraft_with_correction(wm_text, bounce.correction)
                break  # one rewrite per turn (v0.1 parity)

        # 6) commit the final draft.
        port.commit(draft)

        # 7) pre_write deliverers — checkpoint ②: bounce inside agent state.
        draft = self._pre_write(rs, port, draft, ctx)

        # 8) trusted-channel sanitation on EVERY outgoing path.
        res = self.sanitizer.strip(port.draft_content(draft))
        if res.stripped:
            rs.fp.event("sanitizer_strip", turn=rs.turn)
            port.set_draft_content(draft, res.text)

        return draft

    # ── internals (discipline surfaces) ──────────────────────────────────
    def _ground(self, rs: RuntimeState, port: HarnessPort, tag: str) -> None:
        receipts = port.extract_receipts()
        # invariant 2, centrally enforced: registered ids are ALWAYS synthetic.
        receipts = [
            replace(r, synthetic=True) if r.call_id in rs.synthetic_ids else r
            for r in receipts
        ]
        report = GroundingKernel(rs.ledger, self.matcher, rs.fp).ground(
            receipts, rs.consumed
        )
        if report.covered:
            self._debug_dump(rs, f"after_cover:{tag}")

    def _deliver(self, event: str, ctx: TriggerContext, rs: RuntimeState,
                 port: HarnessPort, draft: Any = None) -> list[str]:
        notes: list[str] = []
        em_ids = {e.id: e for e in self.sm.em.entries}
        for d in self.deliverer_bindings.get(event, []):
            try:  # audit fix #11: one bad strategy must not kill the sim
                actions = d.select(ctx, rs.ledger, self.sm.em, draft)
            except Exception as e:
                logger.warning(f"[skfw.deliver] {d.name} raised: {e}")
                rs.fp.event("deliverer_error", name=d.name, turn=rs.turn)
                continue
            for action in actions:
                if isinstance(action, InjectNote):
                    # audit fix #6: injected text passes the trusted-format
                    # sanitizer — packages cannot smuggle board-marker blocks.
                    res = self.sanitizer.strip(action.text)
                    if res.stripped:
                        rs.fp.count("inject_note_sanitized")
                    notes.append(res.text)
                    # Instrumentation (2026-08-13): count -> event. Fingerprint.event
                    # still does counters[kind] += 1, so counting semantics are
                    # bit-identical; it additionally records which deliverer fired
                    # and on which turn, which is what the invocation metrics need.
                    # Successful delivery used to be a bare counter that could not
                    # be recovered from the trajectory after the fact.
                    rs.fp.event(
                        "deliver_inject", deliverer=d.name, turn=rs.turn,
                        trigger=event, chars=len(res.text),
                    )
                elif isinstance(action, CoverWithDoc):
                    # audit fix #5 (critical): delivery receipts are evidence of a
                    # REAL EM delivery — entry must be pending, doc must exist in
                    # THIS package's EM, and content must be that doc's body.
                    entry = next(
                        (e for e in rs.ledger.pending() if e.id == action.entry_id), None
                    )
                    doc = em_ids.get(action.doc_id)
                    if entry is None or doc is None or action.content != doc.body:
                        rs.fp.event(
                            "cover_rejected", deliverer=d.name,
                            entry=action.entry_id, doc=action.doc_id, turn=rs.turn,
                        )
                        continue
                    dedup_key = (action.doc_id, entry.description)
                    if dedup_key in rs.delivered_docs:  # audit fix #12: stable key
                        continue
                    notes.append(
                        f"[RETRIEVED — for your pending item {action.entry_id}]\n{doc.body}"
                    )
                    # settle=False (bank smoke fix 2026-07-06): deliver-only.
                    # Knowledge delivery must not be terminal evidence for
                    # service ledgers — settling emptied pending(), orphaned
                    # real write receipts (11+4 unmatched) and let the board
                    # show DONE for work that never executed.
                    if getattr(action, "settle", True):
                        receipt = DeliveryReceipt(
                            receipt_id=f"dlv:{action.entry_id}:{action.doc_id}",
                            entry_id=action.entry_id,
                            doc_id=action.doc_id,
                        )
                        GroundingKernel(rs.ledger, DeliveryReceiptMatcher(), rs.fp).ground(
                            [receipt], rs.consumed
                        )
                    else:
                        rs.fp.event(
                            "retrieval_injected_only", deliverer=d.name,
                            entry=action.entry_id, doc=action.doc_id, turn=rs.turn,
                        )
                    rs.delivered_docs.add(dedup_key)
                    # As above: count -> event. Counting is unchanged; the event
                    # adds which card, which entry and which turn.
                    rs.fp.event(
                        "retrieval_delivered", deliverer=d.name,
                        entry=action.entry_id, doc=action.doc_id, turn=rs.turn,
                        trigger=event,
                    )
                # ExemplarBounceAction outside pre_write is a package bug: ignore+count
                elif isinstance(action, ExemplarBounceAction):
                    rs.fp.count("misplaced_bounce_action")
        return notes

    def _pre_write(self, rs: RuntimeState, port: HarnessPort, draft: Any, ctxf) -> Any:
        calls = list(port.draft_tool_calls(draft))
        if not calls:
            return draft
        for d in self.deliverer_bindings.get(Event.PRE_WRITE.value, []):
            try:  # audit fix #11
                actions = d.select(
                    ctxf(Event.PRE_WRITE.value, draft_calls=calls, write_tools=self.write_tools),
                    rs.ledger, self.sm.em, draft,
                )
            except Exception as e:
                logger.warning(f"[skfw.pre_write] {d.name} raised: {e}")
                rs.fp.event("deliverer_error", name=d.name, turn=rs.turn)
                continue
            for a in actions:
                if not isinstance(a, ExemplarBounceAction):
                    continue
                # register BEFORE the synthetic messages exist (invariant 2).
                rs.synthetic_ids.update(a.results_by_call.keys())
                rs.fp.event("write_review_bounce", tools=list(a.tools), turn=rs.turn)
                reviewed = port.bounce_and_redraft(draft, a.results_by_call)
                orig = [(tc.name, tc.arguments) for tc in calls]
                new = [(tc.name, tc.arguments) for tc in port.draft_tool_calls(reviewed)]
                if not new:
                    rs.fp.count("write_review_abandoned_to_text")
                elif orig != new:
                    rs.fp.count("write_review_changed")
                else:
                    rs.fp.count("write_review_unchanged")
                self._ground(rs, port, tag="post_review")
                return reviewed  # one bounce per turn (v0.1 parity)
        return draft

    def _update_ledger(self, rs: RuntimeState, port: HarnessPort) -> None:
        mgr = self.sm.manager
        instruction = mgr.build_instruction(rs.ledger, {"write_tools": self.write_tools})
        try:
            raw = port.ledger_llm(instruction)
            requests = mgr.parse_reply(raw)
        except Exception as e:  # fail-safe: keep old ledger (pitfall 8)
            logger.warning(f"[skfw.wm] ledger extraction failed ({e}); keeping previous ledger")
            rs.fp.count("ledger_update_failed")
            return
        accepted = rs.ledger.apply_model_update(requests, self.write_tools)
        rs.fp.count("ledger_update_ok")
        # v3: authorization proposals -> harness verification (quote grounding)
        if getattr(rs.ledger.entry_kind, "supports_authorization", False):
            from recuris.confirmation import ground_confirmations

            try:
                ground_confirmations(rs.ledger, port.genuine_user_texts(), rs.fp)
            except Exception as e:
                logger.warning(f"[skfw.confirm] grounding failed: {e}")
                rs.fp.count("confirm_ground_error")

        # airline: feasibility oracle -> BLOCKED rulings (writer=ORACLE).
        # Domain policy lives in the package plugin; the kernel provides only the
        # call site and the ORACLE-only write permission.
        if self.oracle is not None:
            try:
                from recuris.wm.schema import Writer

                evidence = port.read_tool_results() if hasattr(port, "read_tool_results") else {}
                for rul in self.oracle.rule(rs.ledger.pending(), evidence):
                    rs.ledger.mark_blocked(rul.entry_id, rul.reason, writer=Writer.ORACLE)
                    rs.fp.event("oracle_blocked", entry=rul.entry_id, reason=rul.reason[:80])
            except Exception as e:
                logger.warning(f"[skfw.oracle] {getattr(self.oracle,'name','?')} raised: {e}")
                rs.fp.count("oracle_error")
        logger.info(
            f"[skfw.wm] ledger: {len(rs.ledger.pending())} pending, "
            f"{len(rs.ledger.done())} done ({accepted} accepted)"
        )
        self._debug_dump(rs, "after_extract")

    def _debug_dump(self, rs: RuntimeState, tag: str) -> None:
        path = os.getenv("RECURIS_WM_DEBUG")
        if not path:
            return
        try:
            snap = {"tag": tag, "sim": rs.fp.sim_tag, "needs": rs.ledger.snapshot()}
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        except Exception:
            pass
