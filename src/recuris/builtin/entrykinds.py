"""Builtin EntryKinds — the WM entry shapes the engine ships with.

service_request: the τ² family (verbatim texts inherited from wm/render.py and
wm/selfmaintain.py so the byte-parity tests keep guarding them).
generic_item: the `_base` package's shape — plain items, no tools, no params.
"""

from __future__ import annotations

from typing import Any, Optional

from recuris.wm import render as sr_render
from recuris.wm.selfmaintain import DEFAULT_TEMPLATE as SR_TEMPLATE


class ServiceRequestKind:
    """Task-list entry: description + tool + params (binding-key discipline)."""

    name = "service_request"

    def parse_proposal(self, raw: dict, ctx: dict) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None
        desc = str(raw.get("description") or "").strip()
        if not desc:
            return None
        write_tools = ctx.get("write_tools") or set()
        tool = str(raw.get("tool") or "").strip()
        if tool and tool not in write_tools:
            tool = ""  # unknown tool -> not-yet-clear
        params = raw.get("params")
        if not isinstance(params, dict):
            params = {}
        return {"description": desc, "tool": tool, "params": params}

    def maintain_template(self) -> str:
        return SR_TEMPLATE

    def render_entry(self, entry: Any, index: int, schema: Any) -> str:
        return sr_render.entry_line(index, entry, schema.render)

    def render_texts(self) -> dict:
        return {
            "header": sr_render.WM_HEADER,
            "empty_body": sr_render.WM_EMPTY_BODY,
            "pending_count_fmt": "Pending (NOT EXECUTED) requests: {n}.",
            "discipline": sr_render.WM_DISCIPLINE,
            "board_notice": sr_render.WM_BOARD_NOTICE,
            "blocked_notice": sr_render.BLOCKED_NOTICE,
        }

    def pending_line(self, entry: Any, schema: Any) -> str:
        return (
            f"- {entry.description}  ->  "
            f"{entry.tool or '?'}({sr_render.render_params(entry.params, schema.render)})"
        )


_AUTH_JSON_OLD = '"params": {<tool arguments established so far; correct order_id per item>}}]}'
_AUTH_JSON_NEW = (
    '"params": {<tool arguments established so far; correct order_id per item>},\n'
    '               "confirmed_by": {"quote": "<the customer\'s EXACT sentence authorizing '
    'this request, copied VERBATIM; OMIT this field entirely if not yet clearly authorized>"}}]}'
)
_AUTH_EXTRA_RULE = (
    "- confirmed_by: include ONLY when the customer has EXPLICITLY authorized executing this\n"
    "  request. Copy their sentence VERBATIM — the system verifies it against the transcript\n"
    "  and REJECTS paraphrases. After a scope change, authorization must be given again.\n"
)


class ServiceRequestAuthKind(ServiceRequestKind):
    """service_request + customer-authorization column (v3 challenger).

    Adds: confirm_proposal extraction (model side), AUTHORIZED render marker,
    and the extended maintain template. Verification lives in
    recuris.confirmation (harness side) — this kind only carries proposals."""

    name = "service_request_auth"
    supports_authorization = True

    def maintain_template(self) -> str:
        t = SR_TEMPLATE.replace(_AUTH_JSON_OLD, _AUTH_JSON_NEW)
        return t + _AUTH_EXTRA_RULE

    def parse_proposal(self, raw: dict, ctx: dict):
        fields = super().parse_proposal(raw, ctx)
        if fields is None:
            return None
        cb = raw.get("confirmed_by")
        if isinstance(cb, dict) and cb.get("quote"):
            fields["confirm_proposal"] = {"quote": str(cb["quote"])}
        return fields

    def render_entry(self, entry: Any, index: int, schema: Any) -> str:
        line = super().render_entry(entry, index, schema)
        from recuris.wm.schema import EntryState

        if entry.state is EntryState.NOT_YET:
            if entry.auth:
                line += "  [AUTHORIZED by customer]"
            else:
                line += "  [AWAITING CUSTOMER CONFIRMATION — do not execute yet]"
        return line


GENERIC_TEMPLATE = """[INTERNAL WORKING-MEMORY UPDATE — not shown to anyone else]
You maintain your OWN item list for this task. You have the FULL context above, so use it
directly. Re-derive the up-to-date list of items that STILL need work.

Your current PENDING list (already-resolved items are tracked separately and omitted here):
%(pending)s

Output ONLY a JSON object, no prose, exactly this shape:
{"requests": [{"id": "req-1", "description": "<one line: one atomic item>"}]}

Rules:
- ATOMIC GRANULARITY: exactly ONE actionable/knowable unit per entry.
- Reflect the LATEST state: drop items that are resolved or abandoned.
- Keep at most %(cap)d items.
"""


KNOWLEDGE_NEED_TEMPLATE = """[INTERNAL WORKING-MEMORY UPDATE — pre-mortem blueprint]
PRE-MORTEM: imagine you have already solved this problem and gotten it WRONG. For a
problem of THIS kind, what are the specific steps, constraints, equilibria, edge cases,
or standard facts where a careless expert most often errs? These risk points — NOT the
obvious formulas you're sure of — are your knowledge needs: the things worth checking or
looking up BEFORE you commit.

(This premortem framing was validated 2026-07-05: on the questions doubao gets 8/8 wrong,
"list the knowledge you need" makes it recite the right facts yet still ignore the trap;
"where would you most likely be wrong" makes it name the actual trap — e.g. Ksp on a
hydroxide-dissolution problem, or S/N ∝ √flux on a detectability problem.)

Your current OPEN risk points (resolved ones tracked separately, omitted here):
%(pending)s

Output ONLY a JSON object, no prose, exactly this shape:
{"requests": [{"id": "need-1",
               "description": "<one line: the risk/trap — the step or constraint you might get wrong>",
               "function": "knowledge | procedure",
               "needs_external": true}]}

Rules:
- Focus on where you might be WRONG, not what is obvious. One distinct risk per entry.
- Reflect the LATEST understanding: drop risks that are resolved or no longer relevant.
- Keep at most %(cap)d risk points.
"""


class KnowledgeNeedKind:
    """Capability-need entry, for knowledge-heavy tasks: the ledger holds what
    the agent still needs to know rather than what it still needs to do
    (description + function[knowledge|procedure] + needs_external). Retrieval
    keys off the description; function/needs_external route delivery."""

    name = "knowledge_need"

    def parse_proposal(self, raw: dict, ctx: dict):
        if not isinstance(raw, dict):
            return None
        desc = str(raw.get("description") or "").strip()
        if not desc:
            return None
        fn = str(raw.get("function") or "knowledge").strip().lower()
        if fn not in ("knowledge", "procedure", "action"):
            fn = "procedure"  # unknown -> procedure (CapabilityNeed coercion parity)
        return {"description": desc, "function": fn,
                "needs_external": bool(raw.get("needs_external", True))}

    def maintain_template(self) -> str:
        return KNOWLEDGE_NEED_TEMPLATE

    def render_entry(self, entry: Any, index: int, schema: Any) -> str:
        mark = {"done": "COVERED", "blocked": "UNRESOLVABLE"}.get(entry.state.value, "OPEN")
        fn = (entry.fields or {}).get("function", "knowledge")
        return f"{index}. [{mark}] ({fn}) {entry.description}"

    def render_texts(self) -> dict:
        return {
            "header": "=== WORKING MEMORY: KNOWLEDGE BLUEPRINT (your solving plan) ===",
            "empty_body": "(no knowledge needs identified yet)",
            "pending_count_fmt": "Open knowledge needs: {n}.",
            "discipline": (
                "A need is COVERED only when the required knowledge is actually in front "
                "of you (retrieved or recalled). Retrieved material is delivered below the "
                "blueprint; integrate it before committing to an answer."
            ),
            "board_notice": "",
            "blocked_notice": "",
        }

    def pending_line(self, entry: Any, schema: Any) -> str:
        return f"- {entry.description}"


class GenericItemKind:
    """Generic entry: description only - the `_base` package's native shape."""

    name = "generic_item"

    def parse_proposal(self, raw: dict, ctx: dict) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None
        desc = str(raw.get("description") or "").strip()
        if not desc:
            return None
        return {"description": desc}

    def maintain_template(self) -> str:
        return GENERIC_TEMPLATE

    def render_entry(self, entry: Any, index: int, schema: Any) -> str:
        mark = {
            "done": "RESOLVED",
            "blocked": "BLOCKED",
        }.get(entry.state.value, "OPEN")
        return f"{index}. [{mark}] {entry.description}"

    def render_texts(self) -> dict:
        return {
            "header": "=== WORKING MEMORY: ITEM LIST (system-side) ===",
            "empty_body": "(no items recorded yet)",
            "pending_count_fmt": "Open items: {n}.",
            "discipline": (
                "Discipline: an item is RESOLVED only when verified evidence "
                "says so; your own belief does not resolve items."
            ),
            "board_notice": "",
            "blocked_notice": "Entries marked BLOCKED are not achievable; do not pursue them.",
        }

    def pending_line(self, entry: Any, schema: Any) -> str:
        return f"- {entry.description}"
