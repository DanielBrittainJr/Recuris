"""Builtin WMManagers — how the ledger gets maintained (SPI ②).

The kernel keeps the law (cap/atomicity/permissions via apply_model_update and
fail-safe keep-old-ledger on parse errors); managers own cadence + prompt.
"""

from __future__ import annotations

from recuris.spi import TriggerContext
from recuris.wm.selfmaintain import build_instruction, parse_model_reply


class SelfMaintainEachTurn:
    """τ² champion cadence: full re-derivation on every genuine user turn.

    Rationale (settled 2026-07-04): weak models' "should update but don't" is
    the same disease as "should execute but don't" — the framework's
    deterministic rhythm covers it.
    """

    name = "self_maintain_each_turn"

    def __init__(self, template: str = "", temperature: float = 0.0):
        self.template = template  # "" -> EntryKind default at build time
        self.temperature = temperature

    def should_update(self, ctx: TriggerContext) -> bool:
        return ctx.incoming_kind == "user" and bool(ctx.incoming_text)

    def build_instruction(self, ledger, env: dict) -> str:
        template = self.template or ledger.entry_kind.maintain_template()
        return build_instruction(
            template,
            ledger.pending_json(),
            ledger.schema.max_entries,
            sorted(env.get("write_tools") or []),
        )

    def parse_reply(self, raw_reply: str) -> list:
        return parse_model_reply(raw_reply)


class BlueprintThenDelta(SelfMaintainEachTurn):
    """Phase cadence for knowledge-heavy tasks: a full blueprint on the first
    genuine turn,
    then updates only when the incoming turn brings new information.

    v1 heuristic for "new information": any genuine user/tool turn after the
    blueprint triggers a light re-derivation ONLY if pending entries exist
    (no pending -> nothing to revise). Cheaper than each-turn full rewrites."""

    name = "blueprint_then_delta"

    def __init__(self, template: str = "", temperature: float = 0.0):
        super().__init__(template, temperature)
        self._blueprinted_states: set[int] = set()

    def should_update(self, ctx: TriggerContext) -> bool:
        if ctx.incoming_kind not in ("user", "tool"):
            return False
        if ctx.turn <= 1:
            return True  # blueprint
        return bool(ctx.extra.get("has_pending", True))
