"""Write-review text assembly — EM action_result entries delivered at call time.

Content comes from the EM store (md files); the marker sentence and composition
are engine (they carry the replay-safety and anti-flip-flop fuses). The marker
string must stay in sync with grounding.SYNTHETIC_CONTENT_MARKERS.
"""

from __future__ import annotations

from recuris.em.store import EMStore
from recuris.events import Event

REVIEW_MARKER = "[WRITE REVIEW CHECKPOINT"

NOT_EXECUTED_TEXT = "[Not executed in this round - re-submit after the review checkpoint.]"

GENERIC_FALLBACK = (
    "Verify every argument value against the tool description and the data you retrieved earlier "
    "in this conversation. Never invent identifier values."
)


REFUSAL_BRANCH_LINE = (
    "- If the REQUEST ITSELF violates policy (e.g. exchanging an item for the "
    "identical variant, a refund destination policy forbids), do NOT submit any "
    "tool call and do NOT transfer: reply to the customer in text, explaining "
    "the refusal and offering the closest allowed alternative.\n"
)


def build_review_text(
    em: EMStore, write_tool_names: list[str], *, refusal_branch: bool = False
) -> str:
    """Fork-parity composition (write_review.build_review_text).

    refusal_branch (audit D1; default off for byte-parity with the champion):
    the hard-task autopsy of 18t0
    showed the binary correct/corrected instruction leaves no exit for
    policy-invalid requests — the model escaped to transfer_to_human, burning
    the only pass path. Challenger packages opt in via exemplar_bounce cfg."""
    docs = []
    for name in write_tool_names:
        entry = em.for_tool(Event.PRE_WRITE.value, name)
        body = entry.body if entry else GENERIC_FALLBACK
        docs.append(f"## {name} usage guide\n{body}")
    return (
        f"{REVIEW_MARKER} - from the system, not the customer] This call was NOT executed. "
        "Before it runs, review your call against the usage guide below.\n"
        "- If your call is already correct, submit the SAME tool call again, completely unchanged.\n"
        "- If you spot a mistake, submit the corrected tool call instead.\n"
        + (REFUSAL_BRANCH_LINE if refusal_branch else "")
        + "Your next tool call will be executed directly.\n\n" + "\n\n".join(docs)
    )
