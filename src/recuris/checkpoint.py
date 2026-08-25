"""Checkpoint primitive — internal bounce-and-rewrite (invariant 5).

One primitive, two instances in the verified stack:

- **system_correction** (truth protocol): the rejected draft is DISCARDED (never
  enters agent state); regeneration runs with an amended system prompt. The
  customer never sees the draft.
- **synthetic_tool_result** (write review): the draft WITH its tool calls stays
  in agent state, joined by fabricated ToolMessages carrying the review text;
  regeneration then answers the review. Every fabricated message id MUST be
  registered as synthetic so grounding can never mistake it for a real receipt.

Replay safety: whichever instance runs, only the FINAL message is returned to
the harness — the official trajectory never contains a bounced (unexecuted)
mutating call, because the evaluator re-executes every mutating call on replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Bounce:
    """A checker's verdict that the draft must be rewritten."""

    checker: str
    reason: str
    style: Literal["system_correction", "synthetic_tool_result"]
    # system_correction: text appended to the system prompt for the rewrite
    correction: str = ""
    # synthetic_tool_result: call_id -> synthetic result content
    synthetic_results: dict[str, str] = field(default_factory=dict)


@dataclass
class BounceRecord:
    """Fingerprint payload for one bounce (what/why, for autopsies)."""

    checker: str
    reason: str
    rejected_excerpt: str
