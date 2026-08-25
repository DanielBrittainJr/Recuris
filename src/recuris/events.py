"""Named events — the fixed inspection points of the invariant loop.

Profiles bind deliveries (EM entries) and checkers (gates) to these events in
profile.yaml; turning a mechanism on/off is a config change, never a code change.
"""

from __future__ import annotations

from enum import Enum


class Event(str, Enum):
    TURN_START = "turn_start"                  # incoming message ingested
    INTENT_RECORDED = "intent_recorded"        # ledger updated from a customer turn
    DRAFT_READY = "draft_ready"                # a draft reply exists, not yet committed
    PRE_WRITE = "pre_write"                    # committed draft contains write tool-calls
    MESSAGE_OUT = "message_out"                # final message about to leave the agent
    TERMINAL_BOUNDARY = "terminal_boundary"    # conversation ending (harness-side gate)
    AGENT_DECLARES_DONE = "agent_declares_done"  # agent claims completion (TB profile)
    PRE_ANSWER = "pre_answer"                  # about to commit a final answer
