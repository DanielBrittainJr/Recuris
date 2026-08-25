"""WM rendering — per-turn context recompilation with anti-anchoring.

Text templates are engine defaults carrying the hybrid-grounding discipline
voice; profiles may override them via profile.yaml (they are data). The param
rendering rule (invariant 6) is schema-driven, never hard-coded.
"""

from __future__ import annotations

import json
import re

from recuris.wm.ledger import Ledger, LedgerEntry
from recuris.wm.schema import EntryState, RenderPolicy

WM_HEADER = "=== WORKING MEMORY: INTENT LEDGER (system-side; invisible to the customer) ==="

WM_EMPTY_BODY = (
    "(no service requests recorded yet)\n"
    "Discipline: a request is complete ONLY when its write tool has been executed "
    "successfully. Confirming with the customer does NOT execute anything."
)

WM_DISCIPLINE = (
    "Discipline: a request is complete ONLY when its write tool has been called and "
    "succeeded. Presenting details or receiving the customer's confirmation does NOT "
    "execute anything. Before the conversation can end, every confirmed request above "
    "must show EXECUTED — after the customer agrees, make the tool call immediately.\n"
    "TRUTHFUL PROGRESS REPORTING: never tell the customer that a change is done, "
    "processed, or taken care of unless its ledger entry above shows EXECUTED. When "
    "requests are still pending, say so explicitly and ask the customer to stay on the "
    "line until you report successful execution."
)

WM_BOARD_NOTICE = (
    "The system automatically appends a verified progress board to your messages for "
    "the customer. NEVER write, imitate, or reference drawing such a board yourself — "
    "any board-like block you write will be discarded."
)

BLOCKED_NOTICE = (
    "Entries marked BLOCKED are NOT allowed by policy: never execute them; explain the "
    "refusal and its reason to the customer instead."
)


def render_params(params: dict, policy: RenderPolicy) -> str:
    """Machine-format ids show values; free text shows names only (invariant 6)."""
    shown: list[str] = []
    established: list[str] = []
    for k, v in (params or {}).items():
        if re.fullmatch(policy.machine_id_pattern, str(k)):
            shown.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
        else:
            established.append(str(k))
    parts = shown[:]
    if established:
        parts.append(f"{policy.free_text_label}: " + ", ".join(established))
    return "; ".join(parts) if parts else policy.empty_label


def entry_line(i: int, e: LedgerEntry, policy: RenderPolicy) -> str:
    tool = e.tool or "?"
    if e.state is EntryState.DONE:
        mark = "EXECUTED"
    elif e.state is EntryState.BLOCKED:
        mark = f"BLOCKED ({e.blocked_reason})" if e.blocked_reason else "BLOCKED"
    else:
        mark = "NOT EXECUTED"
    return f"{i}. [{mark}] {e.description}  ->  {tool}({render_params(e.params, policy)})"


def render_wm(ledger: Ledger, *, board_notice: bool) -> str:
    """The full WM block appended to the system prompt every turn.

    Composition order is engine law; every text block and entry line comes
    from the ledger's EntryKind (SPI ①), so the service_request kind stays
    byte-identical to fork 832767e while other kinds bring their own voice.

    ``board_notice``: whether to include the "system appends a verified board"
    warning (the fork renders it unconditionally; parity profile keeps it on).
    """
    kind = ledger.entry_kind
    texts = kind.render_texts()
    visible = ledger.visible()
    if not visible:
        return f"{texts['header']}\n{texts['empty_body']}"
    lines = [kind.render_entry(e, i, ledger.schema) for i, e in enumerate(visible, 1)]
    parts = [
        texts["header"],
        "\n".join(lines),
        texts["pending_count_fmt"].format(n=len(ledger.pending())),
        texts["discipline"],
    ]
    if ledger.blocked() and texts.get("blocked_notice"):
        parts.append(texts["blocked_notice"])
    if board_notice and texts.get("board_notice"):
        parts.append(texts["board_notice"])
    return "\n".join(parts)


def pending_lines(ledger: Ledger) -> list[str]:
    """Terminal-gate interface: one line per confirmed-but-unexecuted entry."""
    kind = ledger.entry_kind
    return [kind.pending_line(e, ledger.schema) for e in ledger.pending()]
