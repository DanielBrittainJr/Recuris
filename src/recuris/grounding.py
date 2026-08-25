"""Grounding — the ONLY path from evidence to ledger state (invariants 1/2/4).

A ledger entry becomes DONE exclusively here, from a REAL environment receipt.
Two independent defenses against fake-DONE (the costliest bug of the old stack):

1. **Synthetic-id registry** (first-class): every ToolMessage the agent
   fabricates internally (write-review bounce, system-check nudge) registers its
   call id; receipts whose id is registered are rejected outright.
2. **Content markers** (defense in depth, inherited from the fork's fix at
   `_harness_cover` L245): receipts whose content carries a known synthetic
   marker are rejected even if the registry missed them.

Incident this rule comes from: the pre-write review bounced a synthetic
ToolMessage with error=False; the landing logic only inspected the error field
and recorded "not executed" as "executed". The resulting false DONE brought
back fabrication, early termination and dropped safeguards at once, while the
run still looked healthy - which made it very hard to spot.

No-fallback rule (invariant 4): an executed write matching no pending entry is
counted + logged, NEVER fabricated into the ledger.
Incident this rule comes from: a scope change broke entry matching, and the
fallback path pushed a raw tool-call signature into the customer-visible status
board as if it were the customer's request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dc_field

from loguru import logger

from recuris.fingerprint import Fingerprint
from recuris.wm.ledger import Ledger, LedgerEntry
from recuris.wm.schema import Writer

# Content markers that identify agent-internal synthetic messages (fork parity).
SYNTHETIC_CONTENT_MARKERS = ("[WRITE REVIEW", "[SYSTEM CHECK")
SYNTHETIC_CONTENT_SUBSTRINGS = ("Not executed in this round",)


def is_synthetic_content(content: str) -> bool:
    c = content or ""
    if any(c.startswith(m) for m in SYNTHETIC_CONTENT_MARKERS):
        return True
    return any(s in c for s in SYNTHETIC_CONTENT_SUBSTRINGS)


@dataclass(frozen=True)
class ToolReceipt:
    """One tool result as evidence. `synthetic` is set by the adapter from the
    runtime's synthetic-id registry; content markers are checked here anyway."""

    call_id: str
    tool: str
    args: dict
    error: bool
    synthetic: bool = False
    content: str = ""


@dataclass
class GroundingReport:
    covered: list[tuple[str, str]] = dc_field(default_factory=list)  # (entry_id, call_id)
    rejected_synthetic: int = 0
    rejected_error: int = 0
    unmatched: int = 0


@dataclass(frozen=True)
class DeliveryReceipt:
    """Harness-born evidence: 'doc X was delivered for entry Y' (retrieval).

    Created only by the kernel's own delivery executor, so it needs no
    synthetic screening — but it still flows through the same grounding
    kernel so every DONE keeps an evidence trail."""

    receipt_id: str
    entry_id: str
    doc_id: str


class ReceiptBindingMatcher:
    """τ²-family matcher (fork `_cover_one` parity): binding-key scoring."""

    name = "receipt_binding_match"

    def __init__(self, match_policy, write_tools: set[str]):
        self.m = match_policy
        self.write_tools = set(write_tools)

    def relevant(self, r) -> bool:
        return isinstance(r, ToolReceipt) and r.tool in self.write_tools

    def _score(self, entry: LedgerEntry, r: ToolReceipt) -> int:
        m = self.m
        if entry.tool and entry.tool != r.tool:
            return -1
        p = entry.params or {}
        bind = str(r.args.get(m.binding_key) or "")
        ebind = str(p.get(m.binding_key) or "")
        if bind and ebind and bind != ebind:
            return -1  # atomic entries: conflicting binding key excludes the match
        s = m.tool_match_bonus if entry.tool == r.tool else 0
        if bind and ebind == bind:
            s += m.binding_exact_bonus
        elif bind and (bind in entry.description or bind in json.dumps(p)):
            s += m.binding_soft_bonus
        call_items = {str(x) for x in (r.args.get(m.collection_key) or [])}
        entry_items = {str(x) for x in (p.get(m.collection_key) or [])}
        if call_items and entry_items & call_items:
            s += m.collection_overlap_bonus  # same items -> survives scope edits
        elif call_items and any(it in entry.description for it in call_items):
            s += m.collection_soft_bonus
        return s

    def match(self, r: ToolReceipt, pending) -> str | None:
        scored = sorted(((self._score(e, r), e) for e in pending), key=lambda x: -x[0])
        if scored and scored[0][0] >= self.m.min_score:
            return scored[0][1].id
        return None


class DeliveryReceiptMatcher:
    """Retrieval grounding: a delivery receipt settles exactly its entry."""

    name = "delivery_receipt"

    def relevant(self, r) -> bool:
        return isinstance(r, DeliveryReceipt)

    def match(self, r: DeliveryReceipt, pending) -> str | None:
        return r.entry_id if any(e.id == r.entry_id for e in pending) else None


class GroundingKernel:
    """Evidence ADMISSION + state writing — the discipline surface.

    Matchers (SPI ④) only decide WHICH entry admitted evidence settles;
    they are never consulted about admissibility, so no package code can
    ever turn a synthetic/error receipt into a DONE (invariant 2).
    """

    def __init__(self, ledger: Ledger, matcher, fingerprint: Fingerprint):
        self.ledger = ledger
        self.matcher = matcher
        self.fp = fingerprint

    def _evidence_id(self, r) -> str:
        return r.call_id if isinstance(r, ToolReceipt) else r.receipt_id

    def _admissible(self, r, report: GroundingReport) -> bool:
        if isinstance(r, DeliveryReceipt):
            return True  # kernel-born; no model-forgeable surface
        if r.synthetic or is_synthetic_content(r.content):
            report.rejected_synthetic += 1
            self.fp.count("ground_rejected_synthetic")
            return False
        if r.error:
            report.rejected_error += 1
            return False
        return True

    def ground(self, receipts: list, consumed: set[str]) -> GroundingReport:
        """Settle admissible evidence into the ledger. Mutates `consumed`."""
        report = GroundingReport()
        for r in receipts:
            if not self.matcher.relevant(r):
                continue
            eid = self._evidence_id(r)
            if eid in consumed:
                continue
            # consume once, BEFORE the admissibility test: a synthetic/error
            # receipt's verdict is fixed per call_id, so re-grounding the same
            # message every turn must not re-count it (else ground_rejected_
            # synthetic inflates linearly with the remaining turns).
            consumed.add(eid)
            if not self._admissible(r, report):
                continue
            entry_id = self.matcher.match(r, self.ledger.pending())
            if entry_id is not None:
                self.ledger.mark_done(entry_id, eid, writer=Writer.HARNESS)
                report.covered.append((entry_id, eid))
                self.fp.count("ground_covered")
                logger.info(f"[skfw.ground] covered entry {entry_id} via {eid}")
            else:
                # Invariant 4: no fallback fabrication — log + count only.
                report.unmatched += 1
                self.fp.count("ground_unmatched")
                logger.info(
                    f"[skfw.ground] evidence {eid} matched no pending entry "
                    "(not recorded as a ledger entry)"
                )
        return report


class Grounder:
    """Back-compat facade: τ² receipt grounding as one object (pre-SPI API)."""

    def __init__(self, ledger: Ledger, write_tools: set[str], fingerprint: Fingerprint):
        self._kernel = GroundingKernel(
            ledger,
            ReceiptBindingMatcher(ledger.schema.match, write_tools),
            fingerprint,
        )

    def ground(self, receipts: list[ToolReceipt], consumed: set[str]) -> GroundingReport:
        return self._kernel.ground(receipts, consumed)
