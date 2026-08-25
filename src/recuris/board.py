"""Customer-facing status board — HARNESS-authored only (invariant 3).

Rendered from the ledger: descriptions come from the model's own extraction,
status marks come from harness grounding. The model never writes this text;
the paired Sanitizer strips model imitations of the marker format.

Self-check: every rendered line must match the acceptance whitelist
`^\\d+\\. .+ \\.{6} (DONE|NOT YET EXECUTED)$` — the same regex the batch
validator uses, so a drifting renderer fails loudly here, not at audit time.
"""

from __future__ import annotations

import re

from recuris.wm.ledger import Ledger
from recuris.wm.schema import EntryState

_LINE_WHITELIST = re.compile(
    r"^\d+\. .+ \.{6} (DONE|NOT YET EXECUTED|NOT PERMITTED BY POLICY)$")


DEFAULT_STAY_NOTICE = (
    "! The items marked NOT YET EXECUTED are not completed — your confirmation "
    "alone does not execute them. Please stay on the line until I report the "
    "execution result before ending our conversation."
)
_FOOTER_WHITELIST = re.compile(r"^! .+$")


class BoardRenderer:
    def __init__(self, marker: str, stay_notice: bool = False,
                 stay_notice_text: str = DEFAULT_STAY_NOTICE):
        self.marker = marker
        self.stay_notice = stay_notice
        self.stay_notice_text = stay_notice_text

    def render(self, ledger: Ledger) -> str | None:
        """Board text, or None when the ledger has nothing to show.

        stay_notice (proposed 2026-07-05; user-side mitigation for the
        termination trap): when pending
        items exist, append a harness-authored footer asking the customer to
        stay until execution is reported — the trusted-channel twin of V3's
        agent-side disclosure. Footer text is package DATA (a communication-class skill memory)."""
        visible = ledger.visible()
        if not visible:
            return None
        lines = [f"{self.marker} ---"]
        for i, e in enumerate(visible, 1):
            # A/B autopsy 2026-07-06 (task35): BLOCKED used to render as
            # "NOT YET EXECUTED" — telling the customer a policy-denied request
            # was still coming, while the stay-notice vanished. Render denials
            # honestly; the whitelist and the scorecard board audit know the mark.
            if e.state is EntryState.DONE:
                mark = "DONE"
            elif e.state is EntryState.BLOCKED:
                mark = "NOT PERMITTED BY POLICY"
            else:
                mark = "NOT YET EXECUTED"
            line = f"{i}. {e.description[:80]} ...... {mark}"
            if not _LINE_WHITELIST.match(line):
                raise ValueError(f"board line failed whitelist self-check: {line!r}")
            lines.append(line)
        if self.stay_notice and ledger.pending():
            note = self.stay_notice_text
            if not _FOOTER_WHITELIST.match(note):
                raise ValueError(f"board footer failed whitelist: {note!r}")
            lines.append(note)
        return "\n".join(lines)
