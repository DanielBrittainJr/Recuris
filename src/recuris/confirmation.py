"""Confirmation grounder — verbatim-quote verification of customer authorization.

Discipline twin of the execution grounder: the model PROPOSES ("the customer
authorized this, here is their exact sentence"), the harness VERIFIES against
the objective transcript, and only then writes the semi-state field
(`entry.auth`, writer=HARNESS). A paraphrase, an invented quote, or a quote
sourced from a synthetic [SYSTEM CHECK message never authorizes anything.

Self-healing property: entries are re-keyed on every ledger rewrite, so the
model must RE-ASSERT authorization each turn — after a scope change the new
entries return to unauthorized until the customer confirms the new scope.
"""

from __future__ import annotations

from recuris.fingerprint import Fingerprint
from recuris.wm.ledger import Ledger
from recuris.wm.schema import Writer

MIN_QUOTE_LEN = 8  # bare "yes" is too ambiguous without turn anchoring


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def ground_confirmations(
    ledger: Ledger, genuine_user_texts: list[str], fp: Fingerprint
) -> None:
    """Verify each pending entry's confirm_proposal; set auth on success."""
    corpus = [_norm(t) for t in genuine_user_texts if t and not t.startswith("[SYSTEM CHECK")]
    verified_by_quote: dict[str, list[str]] = {}
    for e in ledger.pending():
        prop = (e.fields or {}).get("confirm_proposal")
        if not prop:
            continue
        quote = str(prop.get("quote") or "") if isinstance(prop, dict) else str(prop)
        nq = _norm(quote)
        if len(nq) < MIN_QUOTE_LEN:
            fp.event("confirm_rejected", reason="too_short", entry=e.id)
            continue
        # newest-first: confirmations normally reference the latest user turn
        hit = next(
            (i for i in range(len(corpus) - 1, -1, -1) if nq in corpus[i]), None
        )
        if hit is None:
            fp.event("confirm_rejected", reason="quote_not_found", entry=e.id)
            continue
        ledger.mark_authorized(
            e.id, {"quote": quote[:200], "src_user_msg": hit}, writer=Writer.HARNESS
        )
        fp.count("confirm_verified")
        verified_by_quote.setdefault(nq, []).append(e.id)
    # Observability only (B7, 2026-07-06): one customer sentence authorizing
    # several distinct entries is usually legitimate bulk consent ("cancel them
    # all, I don't care about refunds") — but it is also the loophole through
    # which a false AUTHORIZED could feed the authorized_only gate and the
    # execution_gate. The match itself stays UNBOUND (champion semantics);
    # this event only measures how often sharing happens so the A/B autopsy
    # can decide whether binding is ever needed. Zero behavior change.
    for nq, ids in verified_by_quote.items():
        if len(ids) >= 2:
            fp.event("confirm_quote_shared", n=len(ids),
                     entries=",".join(ids[:6]), quote=nq[:80])
