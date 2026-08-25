"""The intent ledger — WM engine with structural write permissions.

Hybrid grounding discipline (invariant 1), made architectural:
- the MODEL's only write channel is :meth:`apply_model_update`, which can only
  rewrite the NOT_YET set (descriptions/tools/params of pending work);
- DONE is set only via :meth:`mark_done` with ``writer=Writer.HARNESS``;
- BLOCKED only via :meth:`mark_blocked` with ``writer`` in {HARNESS, ORACLE};
- any other writer raises :class:`WritePermissionError` — the violation cannot
  ship silently (in the old stack this discipline lived in comments).

Incident this rule comes from: a model's belief that it had finished used to
land in the ledger verbatim, which in turn disabled the termination gate, the
truth protocol and the status board in a single chain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from recuris.errors import WritePermissionError
from recuris.wm.schema import EntryState, WMSchema, Writer


@dataclass
class LedgerEntry:
    """One atomic unit of work (invariant 7): one actionable/knowable unit.

    Kind-specific payload lives in ``fields`` (declared by the EntryKind SPI);
    ``tool``/``params`` remain as read properties because the service_request
    kind is the engine's most exercised shape (τ² family, matcher, truth gate).
    """

    id: str
    description: str
    state: EntryState = EntryState.NOT_YET
    evidence: list[str] = field(default_factory=list)   # evidence ids that grounded DONE
    blocked_reason: str = ""
    fields: dict = field(default_factory=dict)          # EntryKind payload
    # customer authorization (a half-state field): model PROPOSES a verbatim quote in
    # fields["confirm_proposal"]; only the confirmation grounder may set this,
    # after verifying the quote against the real transcript (writer=HARNESS).
    auth: dict | None = None

    @property
    def tool(self) -> str:
        return self.fields.get("tool", "")

    @property
    def params(self) -> dict:
        return self.fields.get("params", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "state": self.state.value,
            "evidence": list(self.evidence),
            "blocked_reason": self.blocked_reason,
            "auth": self.auth,
            **self.fields,
        }


class Ledger:
    """Schema-driven intent ledger for one conversation."""

    def __init__(self, schema: WMSchema):
        self.schema = schema
        self.entries: list[LedgerEntry] = []
        self._seq = 0

    @property
    def entry_kind(self):
        kind = self.schema.entry_kind
        if kind is None:  # lazy default: the τ² family shape
            from recuris.builtin.entrykinds import ServiceRequestKind

            kind = ServiceRequestKind()
            object.__setattr__(self.schema, "entry_kind", kind)
        return kind

    # ── queries ──────────────────────────────────────────────────────────
    def pending(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.state is EntryState.NOT_YET]

    def done(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.state is EntryState.DONE]

    def blocked(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.state is EntryState.BLOCKED]

    def visible(self) -> list[LedgerEntry]:
        """Entries rendered to the model / board (OBSOLETE is audit-only)."""
        return [e for e in self.entries if e.state is not EntryState.OBSOLETE]

    def pending_json(self) -> str:
        # key order (id, description, *fields) matches the pre-SPI service
        # shape byte-for-byte: {"id", "description", "tool", "params"}.
        return json.dumps(
            {
                "requests": [
                    {"id": e.id, "description": e.description, **e.fields}
                    for e in self.pending()
                ]
            },
            ensure_ascii=False,
        )

    # ── MODEL channel (the only one) ─────────────────────────────────────
    def apply_model_update(
        self,
        requests: Iterable[Any],
        write_tools: set[str] | None = None,
        *,
        ctx: dict | None = None,
    ) -> int:
        """Replace the NOT_YET set with the model's re-derived pending list.

        Kernel law (kind-independent, invariants 1/7):
        - DONE / BLOCKED entries kept verbatim (model has no authority);
        - previous NOT_YET entries become OBSOLETE (audit trail), superseded
          by re-keyed fresh entries;
        - the schema cap; atomic granularity (one entry = one unit).
        Content validation is delegated to the EntryKind (SPI ①):
        ``parse_proposal(raw, ctx) -> fields | None``.

        Returns the number of accepted pending entries.
        """
        parse_ctx = dict(ctx or {})
        if write_tools is not None:
            parse_ctx.setdefault("write_tools", set(write_tools))
        kind = self.entry_kind

        kept = [
            e
            for e in self.entries
            if e.state in (EntryState.DONE, EntryState.BLOCKED)
        ]
        audit = [e for e in self.entries if e.state is EntryState.OBSOLETE]
        for e in self.entries:
            if e.state is EntryState.NOT_YET:
                e.state = EntryState.OBSOLETE
                audit.append(e)

        budget = max(0, self.schema.max_entries - len(kept))
        fresh: list[LedgerEntry] = []
        for raw in list(requests)[:budget]:
            fields = kind.parse_proposal(raw, parse_ctx)
            if not fields:
                continue
            desc = fields.pop("description")
            self._seq += 1
            fresh.append(
                LedgerEntry(id=f"req-{self._seq}", description=desc, fields=fields)
            )
        self.entries = kept + fresh + audit
        return len(fresh)

    # ── HARNESS / ORACLE channels ────────────────────────────────────────
    def mark_done(self, entry_id: str, evidence_id: str, *, writer: Writer) -> None:
        """DONE from a REAL environment receipt. HARNESS-only (invariant 1)."""
        if writer is not Writer.HARNESS:
            raise WritePermissionError(
                f"state.done requires Writer.HARNESS, got {writer!r} — "
                "the model can never mark its own work done"
            )
        for e in self.entries:
            if e.id == entry_id and e.state is EntryState.NOT_YET:
                e.state = EntryState.DONE
                if evidence_id and evidence_id not in e.evidence:
                    e.evidence.append(evidence_id)
                return

    def mark_authorized(self, entry_id: str, evidence: dict, *, writer: Writer) -> None:
        """AUTHORIZED = customer explicitly consented, verified by quote.

        HARNESS-only (the confirmation grounder). The model's claim alone can
        never set this: a mere belief that the user consented would otherwise
        drive unauthorized execution."""
        if writer is not Writer.HARNESS:
            raise WritePermissionError(
                f"auth requires Writer.HARNESS, got {writer!r}"
            )
        for e in self.entries:
            if e.id == entry_id and e.state is EntryState.NOT_YET:
                e.auth = dict(evidence)
                return

    def mark_blocked(self, entry_id: str, reason: str, *, writer: Writer) -> None:
        """BLOCKED = infeasible per policy/oracle ruling. Never model-set."""
        if writer not in (Writer.HARNESS, Writer.ORACLE):
            raise WritePermissionError(
                f"state.blocked requires HARNESS or ORACLE, got {writer!r}"
            )
        for e in self.entries:
            if e.id == entry_id and e.state is EntryState.NOT_YET:
                e.state = EntryState.BLOCKED
                e.blocked_reason = reason
                return

    # ── snapshots (refinement-loop raw material) ─────────────────────────
    def snapshot(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.entries]
