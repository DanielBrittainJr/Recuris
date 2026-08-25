"""Execution-completion checkers — treat under-execution at draft_ready.

Both attack the airline read-subset failures (autopsy 2026-07-05): 80% of
"no-write" failures are the agent stopping ONE action short of the write.
These are TYPE-3 (control-flow) diseases — you cannot teach your way out of
them, so the mechanism is a structural gate on the agent's OWN turn (upstream
of the terminal gate, which the user's confirm+STOP bundling defeats).

Both are Checkers (SPI ⑤) bound at draft_ready; they bounce a stalling draft
into a rewrite via the checkpoint primitive (system_correction) — replay-safe,
customer never sees the rejected draft.
"""

from __future__ import annotations

import re
from typing import Optional

from recuris.checkpoint import Bounce
from recuris.events import Event
from recuris.spi import TriggerContext

# affirmative go-ahead in the customer's own words (broad — the gate only fires
# when an executable AUTHORIZED entry already exists, so false positives are cheap)
_GOAHEAD = re.compile(
    r"\b(yes|yeah|yep|sure|go ahead|proceed|please (process|do|go|book|cancel|update|change|"
    r"modify)|confirm|do it|sounds good|that works|okay|ok|perfect|correct)\b",
    re.I,
)

_EXEC_CORRECTION = (
    "\n\n=== EXECUTION GATE — your previous draft was REJECTED (the customer never "
    "saw it) ===\nRejected draft: {excerpt}\nProblem: {reason}\n"
    "The customer has already authorized this and the parameters are established. "
    "Do NOT restate, re-confirm, or ask again. Respond in THIS turn with the write "
    "tool call itself — execute now, then report the result."
)

_ESC_CORRECTION = (
    "\n\n=== ANTI-ESCALATION GATE — your previous draft was REJECTED (the customer "
    "never saw it) ===\nRejected draft: {excerpt}\nProblem: {reason}\n"
    "Do NOT transfer to a human yet. Try the in-policy path first if one exists "
    "(see the usage guide just delivered); if the request is NOT permitted by "
    "policy, refuse it yourself in plain words instead of transferring — never "
    "invent a workaround that violates policy. Only transfer if the customer has "
    "a need that genuinely requires a human."
)

# A/B autopsy 2026-07-06 (task32 4 deaths / task35 6 deaths): when the oracle
# BLOCKs every pending entry, pending() empties and the old gate disarmed —
# letting the transfer through at exactly the moment the right move is to
# refuse IN PERSON (a policy denial is in-scope work: explain it, then ask
# what else you can do). This correction handles the blocked-only state.
_ESC_BLOCKED_CORRECTION = (
    "\n\n=== ANTI-ESCALATION GATE — your previous draft was REJECTED (the customer "
    "never saw it) ===\nRejected draft: {excerpt}\nProblem: {reason}\n"
    "Do NOT transfer to a human. Requests that policy does not permit are still "
    "YOUR job to handle: state plainly that the request is not permitted and WHY "
    "(the reason is on the ledger), then ask whether there is anything else you "
    "can help with. Transfer only if the customer has a separate need that "
    "genuinely requires a human agent."
)


class ExecutionGate:
    """B-exec: forbid ending a turn with a bare confirmation/restatement when an
    AUTHORIZED, executable, unexecuted entry exists — the write must fire NOW.

    Fires on the agent's own turn, so it is immune to the user bundling
    confirmation with ###STOP### (which denies the terminal gate its turn)."""

    name = "execution_gate"
    events = (Event.DRAFT_READY.value,)

    def inspect(self, ctx: TriggerContext, wm_view, draft_text: str,
                draft_has_calls: bool) -> Optional[Bounce]:
        if draft_has_calls:
            return None  # already acting -> fine
        # executable = has a write tool bound AND authorized by the customer
        ready = [
            e for e in wm_view.pending()
            if getattr(e, "tool", "") and getattr(e, "auth", None)
        ]
        if not ready:
            return None
        # only when the customer has signalled go-ahead this turn OR earlier
        # (auth already IS a verified go-ahead quote, so its presence suffices)
        reason = (
            f"{len(ready)} authorized request(s) are executable and NOT executed "
            f"(e.g. '{ready[0].description[:60]}') but the draft is text, not the tool call"
        )
        return Bounce(
            checker="execution_gate", reason=reason, style="system_correction",
            correction=_EXEC_CORRECTION.format(excerpt=(draft_text or "")[:200], reason=reason),
        )


class AntiEscalationGate:
    """B-deflect: forbid transfer_to_human_agents while a feasible (non-BLOCKED)
    pending request the tools CAN serve is on the ledger. Bounces the transfer
    into an in-policy attempt (the matching EM procedure was delivered alongside)."""

    name = "anti_escalation"
    events = (Event.DRAFT_READY.value,)

    def __init__(self, transfer_tool: str = "transfer_to_human_agents"):
        self.transfer_tool = transfer_tool
        self._em = None  # injected by runtime via set_em (content stays in md)

    def set_em(self, em) -> None:
        """runtime hands the package's EM store here so the bounce can deliver
        the matching in-policy procedures (content = data, mechanism = checker)."""
        self._em = em

    def _guides(self) -> str:
        if self._em is None:
            return ""
        hits = self._em.query(event=Event.DRAFT_READY.value, tool=self.transfer_tool)
        if not hits:
            return ""
        return "\n\nIn-policy guides:\n" + "\n\n".join(h.body for h in hits)

    def inspect(self, ctx: TriggerContext, wm_view, draft_text: str,
                draft_has_calls: bool) -> Optional[Bounce]:
        # the draft's tool calls are exposed via ctx.extra (runtime supplies them
        # at draft_ready for checkers that need them)
        calls = ctx.extra.get("draft_calls") or []
        transferring = any(getattr(c, "name", "") == self.transfer_tool for c in calls)
        if not transferring:
            return None
        # feasible pending = NOT_YET entries the oracle did NOT block
        feasible = [e for e in wm_view.pending()]  # pending() already excludes BLOCKED
        if feasible:
            reason = (
                f"draft transfers to a human while {len(feasible)} in-policy request(s) "
                f"remain (e.g. '{feasible[0].description[:60]}') that your tools can serve"
            )
            return Bounce(
                checker="anti_escalation", reason=reason, style="system_correction",
                correction=_ESC_CORRECTION.format(excerpt=(draft_text or "")[:200], reason=reason)
                + self._guides(),
            )
        # blocked-only state (audit fix, airline A/B 2026-07-06): a policy denial is
        # in-scope work — refuse in person, don't hand the customer off. The old
        # early-return here disarmed the gate the moment the oracle blocked
        # everything, which was exactly when transfers spiked (task32/task35).
        blocked = list(getattr(wm_view, "blocked", lambda: [])())
        if not blocked:
            return None  # truly nothing on the ledger -> transfer may be legitimate
        reason = (
            f"draft transfers to a human while {len(blocked)} request(s) are policy-"
            f"denied (e.g. '{blocked[0].description[:60]}') — denials are handled by "
            f"refusing in person, not by transferring"
        )
        return Bounce(
            checker="anti_escalation", reason=reason, style="system_correction",
            correction=_ESC_BLOCKED_CORRECTION.format(
                excerpt=(draft_text or "")[:200], reason=reason)
            + self._guides(),
        )
