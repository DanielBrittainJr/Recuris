"""WM schema — the declarative shape of a task's intent ledger.

The ledger ENGINE knows none of the domain concepts (order_id, refunds, seats).
Field semantics, write permissions, binding keys, and render policies are all
declared here and loaded from profile.yaml. Switching tau2-retail → tau2-airline
changes `binding_key: order_id` → `reservation_id` in data, not code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Writer(str, Enum):
    """Who is allowed to write a ledger field (invariant 1)."""

    MODEL = "model"      # subjective content: descriptions, tools, params
    HARNESS = "harness"  # objective state: DONE from real tool receipts
    ORACLE = "oracle"    # feasibility rulings: BLOCKED from policy computation


class EntryState(str, Enum):
    """The four canonical states of a ledger entry.

    DONE requires harness evidence; BLOCKED requires a harness/oracle ruling —
    the model can set neither (it would be self-assessment, the Mode-A root).
    """

    NOT_YET = "not_yet"
    DONE = "done"
    BLOCKED = "blocked"
    OBSOLETE = "obsolete"


@dataclass(frozen=True)
class MatchPolicy:
    """How real tool receipts are matched to pending ledger entries.

    Distilled from the reference agent's cover step: a conflicting
    binding-key value EXCLUDES a match (atomic entries), item overlap is a
    strong signal that survives scope edits.
    """

    # audit fix #1: NO domain defaults in the kernel - '' means disabled, and the
    # runtime refuses receipt_binding_match without an explicit binding_key
    # (a missing key silently degrades matching to "first same-tool entry wins",
    # which mis-grounds multi-request conversations with harness authority).
    binding_key: str = ""               # retail: order_id; airline: reservation_id
    collection_key: str = ""            # retail: item_ids
    tool_match_bonus: int = 1
    binding_exact_bonus: int = 2
    binding_soft_bonus: int = 1         # key value appears in description/params blob
    collection_overlap_bonus: int = 2
    collection_soft_bonus: int = 1      # member id appears in description
    min_score: int = 1                  # best score must be >= this to cover


@dataclass(frozen=True)
class RenderPolicy:
    """Anti-anchoring render rule (invariant 6, task-22 forensics).

    Machine-format identifiers (tool-sourced, where verbatim copy PREVENTS
    transcription errors) render with values; free-text fields (customer speech
    the model must normalize at write time) render name-only — echoing the raw
    value anchored the model to unnormalized input ("New York" vs "NY") and
    CAUSED wrong-arg failures.
    """

    machine_id_pattern: str = r".*_ids?"   # fullmatch against the param NAME
    free_text_label: str = "established-but-verify-format"
    empty_label: str = "params not yet established"


@dataclass(frozen=True)
class WMSchema:
    """Full declarative spec of one profile's working-memory ledger."""

    entry_noun: str = "service request"
    max_entries: int = 8
    match: MatchPolicy = field(default_factory=MatchPolicy)
    render: RenderPolicy = field(default_factory=RenderPolicy)
    board_marker: str = "--- Progress (system-verified)"
    # EntryKind instance (SPI ①). None -> lazily resolved to the builtin
    # service_request kind (keeps every pre-SPI call site working unchanged).
    entry_kind: object = None
    # field -> writer; the ledger engine consults this before every write.
    field_writers: dict[str, Writer] = field(
        default_factory=lambda: {
            "description": Writer.MODEL,
            "tool": Writer.MODEL,
            "params": Writer.MODEL,
            "state.done": Writer.HARNESS,
            "state.blocked": Writer.ORACLE,   # harness also accepted (see Ledger)
            "state.obsolete": Writer.MODEL,   # customer withdrew — model may drop
        }
    )
