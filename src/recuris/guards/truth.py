"""Truth protocol — draft-vs-ledger consistency checker (draft_ready event).

The flagship mechanism (Iter9 champion; Pareto-optimal: 80.2% / gate-dependence
34% / Mode-B 18). Three violations, checked against the HARNESS-verified ledger:

  V1 false-completion claim  — text claims done, ledger says NOT EXECUTED
  V2 confirmation stall      — customer action-confirmed an executable pending
                               request, draft is text instead of the tool call
  V3 missing disclosure      — executable pending exists, draft never says
                               "not executed yet" (closure-toned summaries are
                               the main cause of premature customer exits)

ENGINE = this flow. DATA = the four regex families, loaded from profile.yaml
(word lists are data, requirement C.6; the defaults below are the fork's
verbatim patterns, kept as fallback).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from recuris.checkpoint import Bounce
from recuris.wm.ledger import Ledger

# ── fork-verbatim default patterns (tau2 CS-English family) ─────────────
DEFAULT_CLAIM = (
    r"((has|have|had) been (successfully )?(processed|cancell?ed|returned|exchanged|"
    r"updated|modified|refunded|completed|changed|placed)|successfully (processed|"
    r"cancell?ed|returned|exchanged|updated|modified|refunded|completed)|is now "
    r"(cancell?ed|returned|updated|complete[d]?)|all set|taken care of|"
    r"(i|we) (have|'ve) (processed|cancell?ed|returned|exchanged|updated|refunded|completed))"
)
DEFAULT_CLAIM_NEG = r"\b(not|n't|will|won't|once|after|as soon as|pending|yet|going to|about to)\b"
DEFAULT_CONFIRM_ACT = (
    r"(go ahead|proceed|please (process|cancel|return|exchange|modify|update|change|do)|"
    r"i confirm|yes,? (please|i want|i'?d like|process|cancel|return|exchange|modify|update)|"
    r"do it|that'?s? (all )?correct)"
)
# The frozen tau2-Bench user simulator occasionally replies in Chinese. These
# three alternatives are "after executing", "not yet" and "not done yet";
# they are written as escaped code points so the source stays ASCII.
_ZH_STATUS_ACK = "\u6267\u884c\u540e|\u5c1a\u672a|\u8fd8\u6ca1"
DEFAULT_STATUS_ACK = (
    r"(not (yet )?(been )?(executed|processed|completed|done)|haven'?t (yet )?"
    r"(executed|processed|completed)|once you confirm|as soon as you confirm|"
    r"after (you|your) confirm|will (then )?(execute|process|proceed)|about to "
    r"(execute|process)|hold on|stay (on the line|with me)|right away and "
    r"(report|confirm)|" + _ZH_STATUS_ACK + r")"
)


@dataclass(frozen=True)
class TruthPatterns:
    claim: str = DEFAULT_CLAIM
    claim_neg: str = DEFAULT_CLAIM_NEG
    confirm_act: str = DEFAULT_CONFIRM_ACT
    status_ack: str = DEFAULT_STATUS_ACK


CORRECTION_TEMPLATE = (
    "\n\n=== INTERNAL DRAFT CHECK — your previous draft was REJECTED "
    "(the customer never saw it) ===\nRejected draft: {draft_excerpt}"
    "\nProblem: {reason}\n"
    "Rewrite this turn now. Rules: never state or imply that a request is "
    "done unless its ledger entry shows EXECUTED; if the customer already "
    "confirmed an executable request, respond with the write tool call "
    "immediately instead of more text; otherwise reply truthfully, e.g. "
    "'I have NOT executed <X> yet — I am doing it right now, please stay "
    "on the line until I confirm.'"
)


class TruthProtocol:
    def __init__(
        self,
        patterns: TruthPatterns = TruthPatterns(),
        level: Literal["full", "v2only"] = "full",
    ):
        self.level = level
        self._claim = re.compile(patterns.claim, re.I)
        self._claim_neg = re.compile(patterns.claim_neg, re.I)
        self._confirm_act = re.compile(patterns.confirm_act, re.I)
        self._status_ack = re.compile(patterns.status_ack, re.I)

    def inspect(
        self,
        ledger: Ledger,
        draft_text: str,
        draft_has_tool_calls: bool,
        incoming_user_text: Optional[str],
    ) -> Optional[Bounce]:
        """Return a Bounce if the draft misreports progress, else None."""
        if draft_has_tool_calls:  # acting is always allowed
            return None
        text = draft_text or ""
        if not text:
            return None
        pending = ledger.pending()
        if not pending:
            return None
        # V1: false completion claim about pending work
        if self.level != "v2only":
            for sent in re.split(r"(?<=[.!?])\s+", text):
                if self._claim.search(sent) and not self._claim_neg.search(sent):
                    return self._bounce(
                        "V1",
                        f"draft claims completion ('{sent.strip()[:80]}') but "
                        f"{len(pending)} ledger request(s) are NOT EXECUTED",
                        text,
                    )
        executable = [e for e in pending if e.tool]
        # V2: customer just action-confirmed an executable request, draft stalls
        if incoming_user_text and self._confirm_act.search(incoming_user_text):
            if executable:
                return self._bounce(
                    "V2",
                    "customer just confirmed; an executable pending request exists "
                    f"('{executable[0].description[:60]}') — this turn must be the tool call",
                    text,
                )
        # V3: mandatory progress disclosure
        if self.level == "v2only":
            return None
        if executable and not self._status_ack.search(text):
            return self._bounce(
                "V3",
                f"{len(executable)} executable request(s) are NOT EXECUTED but the draft "
                "does not say so — the customer may believe the work is done and leave. "
                "State explicitly that you have not executed them yet and that you will "
                "execute immediately upon confirmation and report back",
                text,
            )
        return None

    @staticmethod
    def _bounce(code: str, reason: str, draft_text: str) -> Bounce:
        return Bounce(
            checker=f"truth_{code}",
            reason=reason,
            style="system_correction",
            correction=CORRECTION_TEMPLATE.format(
                draft_excerpt=(draft_text or "")[:400], reason=reason
            ),
        )
