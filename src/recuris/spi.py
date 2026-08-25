"""The five SPI contracts — the slots a Skill Memory package fills.

Design charter (aligned 2026-07-04 with the previous owner):
- Packages fill STRATEGY holes; they can never touch the DISCIPLINE surface.
  Deliverers only PROPOSE actions (the kernel executes them and does synthetic-id
  registration); matchers only MATCH (evidence admission lives in the kernel);
  managers only PROPOSE updates (cap/atomicity/write-permission enforced by the
  ledger). The seven hard-won invariants therefore hold regardless of package code.
- Implementations differ per task; signatures never do. Switching tasks =
  `TurnRuntime.load(<other package path>)`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from recuris.checkpoint import Bounce

# ─────────────────────────────────────────────────────────────────────────
# shared views handed to strategies (read-only by convention)
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TriggerContext:
    """What a strategy may look at when deciding to act.

    This is the framework's 'listening' surface over the agent↔environment
    interaction: the incoming message kind/text, the turn index, and (via
    wm_view) the harness-verified ledger state. Strategies combine these to
    fire at the agent's blind spots.
    """

    event: str                      # events.Event value
    turn: int
    incoming_kind: str = ""         # user | system_check | tool | other
    incoming_text: str = ""
    extra: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────
# ① EntryKind — what a WM entry looks like (declarative + parse/render)
# ─────────────────────────────────────────────────────────────────────────


@runtime_checkable
class EntryKind(Protocol):
    """Shape of one ledger entry: fields, parsing of model proposals, rendering.

    The LEDGER engine stays ignorant of any of this; states/permissions/cap
    are kernel law, everything content-shaped is EntryKind."""

    name: str

    def parse_proposal(self, raw: dict, ctx: dict) -> Optional[dict]:
        """Validate one model-proposed entry -> normalized fields dict
        (stored on LedgerEntry.fields), or None to skip. Must be tolerant:
        malformed input returns None, never raises."""
        ...

    def maintain_template(self) -> str:
        """Default self-maintain instruction template (profile may override).
        %-style placeholders: pending / cap / write_tools (kind-specific ok)."""
        ...

    def render_entry(self, entry: Any, index: int, schema: Any) -> str:
        """One WM line for this entry (anti-anchoring rules live here)."""
        ...

    def render_texts(self) -> dict:
        """Block texts: header / empty_body / pending_count_fmt / discipline /
        board_notice / blocked_notice. Engine composes them in the invariant
        order; empty strings mean 'omit'."""
        ...

    def pending_line(self, entry: Any, schema: Any) -> str:
        """Terminal-gate view line for a pending entry."""
        ...


# ─────────────────────────────────────────────────────────────────────────
# ② WMManager — how the WM gets maintained
# ─────────────────────────────────────────────────────────────────────────


class WMManager(Protocol):
    name: str

    def should_update(self, ctx: TriggerContext) -> bool: ...

    def build_instruction(self, wm_view: Any, profile: Any) -> str:
        """The maintenance prompt sent through the port's ledger_llm channel."""
        ...

    def parse_reply(self, raw_reply: str) -> list:
        """Model reply -> list of raw proposals (raises on malformation; the
        kernel catches and keeps the previous ledger — pitfall 8)."""
        ...


# ─────────────────────────────────────────────────────────────────────────
# ③ Deliverer — how EM reaches the model's eyes (proposals only)
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InjectNote:
    """Append text to this turn's WM block (system-side note)."""

    text: str
    tag: str = ""


@dataclass(frozen=True)
class ExemplarBounceAction:
    """Bounce the committed draft with synthetic results (write-review shape).
    The KERNEL executes: appends results, registers synthetic ids, redrafts."""

    results_by_call: dict          # call_id -> synthetic result content
    tools: tuple = ()              # write tool names (fingerprint payload)


@dataclass(frozen=True)
class CoverWithDoc:
    """Retrieval outcome: put doc content under the model's nose AND (when
    settle=True) mark the need covered via a delivery receipt.

    settle=False = deliver-only (banking smoke autopsy 2026-07-06): for
    service_request-style ledgers, knowledge delivery is NOT terminal
    evidence — settling there created fake DONEs that emptied pending(),
    orphaned real write receipts and disarmed the terminal gate. Coverage
    semantics remain correct for knowledge_need ledgers, whose
    entries ARE satisfied by the document itself."""

    entry_id: str
    doc_id: str
    content: str
    settle: bool = True


DeliveryAction = Any  # InjectNote | ExemplarBounceAction | CoverWithDoc


class Deliverer(Protocol):
    name: str
    events: tuple  # which events this deliverer wants to hear

    def select(self, ctx: TriggerContext, wm_view: Any, em: Any, draft: Any = None) -> Sequence[DeliveryAction]:
        ...


# ─────────────────────────────────────────────────────────────────────────
# ④ GroundingMatcher — which entry a piece of evidence settles
# ─────────────────────────────────────────────────────────────────────────


class GroundingMatcher(Protocol):
    """MATCHING only. Evidence admission (synthetic/error rejection) is done
    by the kernel BEFORE match() is ever called — packages cannot bypass it."""

    name: str

    def match(self, evidence: Any, pending: Sequence[Any]) -> Optional[str]:
        """Return the entry_id the (already admitted) evidence settles, or None."""
        ...


# ─────────────────────────────────────────────────────────────────────────
# ⑤ Checker — gates producing bounce requests
# ─────────────────────────────────────────────────────────────────────────


class Checker(Protocol):
    name: str
    events: tuple

    def inspect(self, ctx: TriggerContext, wm_view: Any, draft_text: str,
                draft_has_calls: bool) -> Optional[Bounce]:
        ...


# ─────────────────────────────────────────────────────────────────────────
# ⑥ FeasibilityOracle — policy-driven BLOCKED rulings (optional, domain code)
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BlockRuling:
    """An oracle's verdict that a pending entry is policy-infeasible."""

    entry_id: str
    reason: str


class FeasibilityOracle(Protocol):
    """Reads OBJECTIVE domain state (from real READ-tool receipts, supplied by
    the harness) and rules which pending entries are policy-forbidden. The
    ruling is written as BLOCKED via the kernel with writer=ORACLE — the model
    can never self-set feasibility (otherwise over-reach and self-deception
    regress).

    Domain policy = domain code -> this lives in the package's local: plugin;
    the kernel only provides the call site and the ORACLE-only write gate."""

    name: str

    def rule(self, pending: Sequence[Any], evidence: dict) -> Sequence[BlockRuling]:
        """`evidence` = {tool_name: [parsed_json_result, ...]} from READ receipts
        (e.g. get_reservation_details, get_user_details). Return BlockRulings for
        entries that violate policy; empty for all-feasible."""
        ...
