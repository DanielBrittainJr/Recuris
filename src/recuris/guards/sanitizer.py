"""Trusted-channel sanitizer (invariant 3).

The model imitates every trusted format it has seen and will FORGE statuses —
"a lie wearing the trusted channel's uniform". Any harness-exclusive display
format must be paired with a sanitizer that strips model-authored imitations on
EVERY outgoing path (the fork initially missed the terminal-gate forced-round
path; the agent-side strip is the final defense).
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


@dataclass(frozen=True)
class SanitizeResult:
    text: str
    stripped: bool


class Sanitizer:
    def __init__(self, marker: str, fallback_text: str = "(status update follows)"):
        self.marker = marker
        self.fallback_text = fallback_text

    def strip(self, text: str | None) -> SanitizeResult:
        """Cut everything from the first marker occurrence onward.

        Empty results are replaced by the fallback (an empty assistant message
        violates the tau2 message protocol).
        """
        if not text or self.marker not in text:
            return SanitizeResult(text=text or "", stripped=False)
        cut = text.index(self.marker)
        logger.warning("[skfw.sanitizer] stripped model-imitated trusted block")
        return SanitizeResult(text=text[:cut].rstrip() or self.fallback_text, stripped=True)
