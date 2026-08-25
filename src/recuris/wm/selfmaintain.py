"""Ledger self-maintenance — the model re-derives its own pending set each turn.

Architecture decision (2026-07-04, settled): no separate bookkeeper persona, no
truncated window, no fallback entry fabrication. The update rides on the agent's
real system prompt + FULL history. The instruction template is profile DATA
(profiles may reword it); the parse/rebuild logic here is engine.

JSON robustness (pitfall 8): strip code fences, tolerate failure by KEEPING the
previous ledger — one bad parse must never nuke the account book.
"""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)

# Verbatim from the reference agent's LEDGER_SELF_MAINTAIN,
# fork 832767e). %-style placeholders: pending / cap / write_tools.
DEFAULT_TEMPLATE = """[INTERNAL LEDGER UPDATE — not shown to the customer]
You (the agent) maintain your OWN request ledger for this conversation. You have the FULL conversation
above, so use it directly — do not rely on any summary. Re-derive the up-to-date list of the customer's
CONCRETE service requests that STILL require a system change (cancellations, modifications, exchanges,
returns, address / payment changes). Questions that only need information do NOT go on the ledger.

Your current PENDING ledger (already-executed requests are tracked separately and omitted here):
%(pending)s

Output ONLY a JSON object, no prose, exactly this shape:
{"requests": [{"id": "req-1",
               "description": "<one line; include the SPECIFIC order id / item ids / amounts>",
               "tool": "<the write tool that will execute it, from the list below; \"\" if unclear>",
               "params": {<tool arguments established so far; correct order_id per item>}}]}

Rules:
- ATOMIC GRANULARITY: exactly ONE write-tool call per entry. Different orders = different entries,
  each with its OWN correct order_id (e.g. returning items that live in two orders = two entries).
- Reflect the customer's LATEST intent: if they changed scope (e.g. from one item to all items),
  rewrite the entries to the new scope with the correct per-item order ids.
- "params" values must come from the conversation or tool results — NEVER invent ids.
- Drop requests the customer cancelled or abandoned.
- Keep at most %(cap)d requests. Write tools available: %(write_tools)s
"""


def build_instruction(
    template: str, pending_json: str, cap: int, write_tools: list[str]
) -> str:
    return template % {
        "pending": pending_json,
        "cap": cap,
        "write_tools": ", ".join(write_tools) or "(none)",
    }


def parse_model_reply(raw_reply: str) -> list[dict[str, Any]]:
    """Parse the model's ledger JSON. Raises on any malformation.

    The CALLER decides what failure means (keep old ledger + fingerprint count);
    this function only parses, so the fail-safe policy stays in one place.
    """
    raw = _JSON_FENCE_RE.sub("", (raw_reply or "").strip())
    data = json.loads(raw)
    requests = data.get("requests")
    if not isinstance(requests, list):
        raise ValueError("'requests' missing or not a list")
    return requests
