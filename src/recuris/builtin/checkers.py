"""Builtin Checkers — gates producing bounce requests (SPI ⑤)."""

from __future__ import annotations

from typing import Optional

from recuris.checkpoint import Bounce
from recuris.events import Event
from recuris.guards.truth import TruthPatterns, TruthProtocol
from recuris.spi import TriggerContext


class TruthProtocolChecker:
    """The Iter9 champion draft gate, as a bindable strategy."""

    name = "truth_protocol"
    events = (Event.DRAFT_READY.value,)

    def __init__(self, level: str = "full", patterns: Optional[dict] = None):
        self.init_cfg = {"level": level, **({"patterns": patterns} if patterns else {})}
        pat = TruthPatterns(**patterns) if patterns else TruthPatterns()
        self.tp = TruthProtocol(pat, level=level)  # type: ignore[arg-type]

    def inspect(self, ctx: TriggerContext, wm_view, draft_text: str,
                draft_has_calls: bool) -> Optional[Bounce]:
        incoming = ctx.incoming_text if ctx.incoming_kind == "user" else None
        return self.tp.inspect(wm_view, draft_text, draft_has_calls, incoming)
