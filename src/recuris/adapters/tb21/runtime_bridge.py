"""Small phase bridge between Terminus-2 and a standard Skill Memory."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Sequence

from recuris.events import Event
from recuris.grounding import ToolReceipt
from recuris.runtime import TurnRuntime
from recuris.spi import TriggerContext
from recuris.wm import render as wmrender
from recuris.wm.schema import Writer

# Error shapes worth reacting to in terminal output.  Used both to harvest
# the lines that describe a failure and to gate error-triggered delivery.
ERROR_LINE_RE = re.compile(
    r"(?i)(error|traceback|exception|fail(?:ed|ure)?|fatal"
    r"|command not found|no such file|permission denied"
    r"|non-zero exit|segmentation fault|syntax)"
)
_TOKEN_RE = re.compile(r"[a-z0-9_#]+")
# Tokens shared by nearly every error line and every card; excluded from the
# overlap score so "error" alone never selects a guide.
_STOP_TOKENS = {
    "error", "errors", "fail", "failed", "failure", "fatal", "exception",
    "the", "and", "for", "not", "with", "this", "that", "when", "before",
}
# A card must share this many informative tokens with the error lines.
ERROR_MATCH_MIN_OVERLAP = 3
# At most this many error-triggered deliveries per episode.
ERROR_DELIVERY_CAP = 3
# The literal that opens (and, with END, closes) the per-turn prefix this
# bridge prepends to Terminus-2's prompt.  Exported so a downstream shim can
# grep a trajectory for it without re-spelling the string.
PROMPT_PREFIX_MARKER = "[RECURIS INTERNAL CONTEXT]"
DEFAULT_TURN_SOFT_CAP_ENV = "RECURIS_TB21_TURN_SOFT_CAP"


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(str(text).lower())) - _STOP_TOKENS


def _turn_soft_cap(env_var: str = DEFAULT_TURN_SOFT_CAP_ENV) -> int:
    return int(os.getenv(env_var, "150"))


class TerminalSkillBridge:
    """Dispatch E/W/rho/C at the three hooks exposed by Terminus-2.

    Everything below is bound to Terminus-2 (its three hooks, its single
    ``bash_command`` write tool) rather than to Terminal-Bench 2.1, so a
    sibling terminal benchmark on the same harness reuses this class as-is.
    The three attributes that do name a benchmark are LABELS ONLY — the
    fingerprint's sim tag, the receipt call-id prefix, and the environment
    variable naming the long-run soft cap.  A subclass overrides them; the
    defaults are byte-for-byte the literals TB2.1 always used, so TB2.1's own
    behaviour is unchanged by their extraction.
    """

    SIM_TAG = "terminal-bench:2.1"
    RECEIPT_PREFIX = "tb21"
    SOFT_CAP_ENV = DEFAULT_TURN_SOFT_CAP_ENV

    def __init__(self, skill_memory: str | Path, package_digest: str) -> None:
        self.skill_memory = Path(skill_memory).resolve()
        self.package_digest = package_digest
        self.runtime = TurnRuntime.load(str(self.skill_memory), {"bash_command"})
        self.reset()

    def reset(self) -> None:
        self.state = self.runtime.new_state(self.SIM_TAG)
        self.instruction = ""
        self.receipts: list[ToolReceipt] = []
        self.completion_blocks = 0
        self._pending_notes: list[str] = []
        self._last_wm_digest = ""
        self._error_deliveries = 0
        self._error_delivered_ids: set[str] = set()

    def start(self, instruction: str) -> None:
        self.instruction = str(instruction or "").strip()
        manager = self.runtime.sm.manager
        seed_requests = getattr(manager, "seed_requests", None)
        requests = (
            seed_requests(self.instruction)
            if callable(seed_requests)
            else [{"description": self.instruction}]
        )
        accepted = self.state.ledger.apply_model_update(
            requests,
            self.runtime.write_tools,
        )
        self.state.fp.count("ledger_update_ok")
        self.state.fp.event("wm_seeded", accepted=accepted)

    def prepare_prompt(self, prompt: str) -> str:
        if not self.instruction:
            raise RuntimeError("TerminalSkillBridge.start() must run before inference")

        self.state.turn += 1
        context = TriggerContext(
            event=Event.TURN_START.value,
            turn=self.state.turn,
            incoming_kind="user" if self.state.turn == 1 else "tool",
            incoming_text=self.instruction if self.state.turn == 1 else str(prompt),
            extra={"delivered_docs": self.state.delivered_docs},
        )
        notes = list(self.runtime._deliver(
            Event.TURN_START.value,
            context,
            self.state,
            _NoopPort(),
        ) or [])
        if self._pending_notes:
            notes.extend(self._pending_notes)
            self._pending_notes = []

        soft_cap = _turn_soft_cap(self.SOFT_CAP_ENV)
        if soft_cap > 0 and self.state.turn > soft_cap:
            # Long-task brake: past the soft cap our per-turn prefix stops
            # compounding (the 207-step build-pov-ray grind burned 20M prompt
            # tokens against bare's 0.8M).  Delivered notes still pass.
            self.state.fp.count("soft_cap_prefix_minimized")
            memory_context = (
                f"[turn {self.state.turn} exceeds soft cap {soft_cap}: "
                "wrap up — validate the actual artifact and finish]"
            )
            if notes:
                memory_context += "\n\n" + "\n\n".join(notes)
            return (
                f"{PROMPT_PREFIX_MARKER}\n"
                f"{memory_context}\n"
                "[END RECURIS INTERNAL CONTEXT]\n\n"
                f"{prompt}"
            )

        rendered = wmrender.render_wm(
            self.state.ledger,
            board_notice=self.runtime.sm.board_notice,
        )
        digest = hashlib.sha1(rendered.encode("utf-8")).hexdigest()
        if self.state.turn > 1 and digest == self._last_wm_digest:
            # The ledger did not change since the last turn (in this runtime
            # it only changes at completion), and Terminus keeps the full
            # task instruction in its own prompt — re-reading the seeded
            # instruction every turn doubled prompt cost per step for zero
            # information.  Emit a one-line marker instead.
            memory_context = (
                f"[working memory unchanged: "
                f"{len(self.state.ledger.pending())} pending]"
            )
            self.state.fp.count("wm_render_skipped_unchanged")
        else:
            memory_context = rendered
            self._last_wm_digest = digest
            self.state.fp.count("wm_render")
        if notes:
            memory_context += "\n\n" + "\n\n".join(notes)
        return (
            f"{PROMPT_PREFIX_MARKER}\n"
            f"{memory_context}\n"
            "[END RECURIS INTERNAL CONTEXT]\n\n"
            f"{prompt}"
        )

    def observe_commands(
        self,
        commands: Sequence[Any],
        terminal_output: str,
        *,
        timed_out: bool,
    ) -> None:
        for index, command in enumerate(commands, start=1):
            receipt = ToolReceipt(
                call_id=f"{self.RECEIPT_PREFIX}-{self.state.turn}-{index}",
                tool="bash_command",
                args={"keystrokes": str(getattr(command, "keystrokes", ""))},
                error=timed_out,
                content=str(terminal_output or ""),
            )
            self.receipts.append(receipt)
            self.state.fp.event(
                "terminal_receipt",
                call_id=receipt.call_id,
                error=receipt.error,
                turn=self.state.turn,
            )
        if commands:
            self._dispatch_on_retrieval(str(terminal_output or ""))
            self._error_triggered_delivery(str(terminal_output or ""))

    def _dispatch_on_retrieval(self, terminal_output: str) -> None:
        """Raise the on-retrieval event after each command observation.

        Retrieval deliverers default to `intent_recorded`, which this runtime
        never raised — packages carrying default-bound retrieval were silent
        no-ops here.  Dispatching it at the observation point makes those
        default bindings live without any manifest rebinding; selection stays
        WM-driven, and delivered_docs dedup prevents re-delivery loops.  Notes
        are buffered into the next prompt prefix."""
        context = TriggerContext(
            event=Event.INTENT_RECORDED.value,
            turn=self.state.turn,
            incoming_kind="tool",
            incoming_text=terminal_output[-1500:],
            extra={"delivered_docs": self.state.delivered_docs},
        )
        notes = self.runtime._deliver(
            Event.INTENT_RECORDED.value,
            context,
            self.state,
            _NoopPort(),
        )
        self.state.fp.count("on_retrieval_dispatch")
        if notes:
            self._pending_notes.extend(notes)
            self.state.fp.event(
                "on_retrieval_delivery",
                notes=len(notes),
                turn=self.state.turn,
            )

    def _error_triggered_delivery(self, terminal_output: str) -> None:
        """Deliver the best-matching guide card when the terminal errors.

        The machine's retrieval carriers select by working-memory entries, so
        terminal failures cannot steer them.  This adapter-local step matches
        harvested error lines against the card library lexically and injects
        the single best card — the terminal domain's version of treating the
        symptom when it appears instead of standing medication every turn."""
        if self._error_deliveries >= ERROR_DELIVERY_CAP:
            return
        error_lines = [
            line for line in terminal_output.splitlines()
            if ERROR_LINE_RE.search(line)
        ][-8:]
        if not error_lines:
            return
        query = _tokens(" ".join(error_lines))
        if not query:
            return
        best_score, best_card = 0, None
        for card in self.runtime.sm.em.entries:
            if card.type not in {"knowledge", "procedure"}:
                continue
            if card.id in self._error_delivered_ids:
                continue
            score = len(query & _tokens(f"{card.id} {card.body[:400]}"))
            if score > best_score:
                best_score, best_card = score, card
        if best_card is None or best_score < ERROR_MATCH_MIN_OVERLAP:
            return
        self._error_deliveries += 1
        self._error_delivered_ids.add(best_card.id)
        self._pending_notes.append(
            "[ERROR-TRIGGERED GUIDE — the last command failed and this card "
            f"matches the failure]\n### {best_card.id}\n{best_card.body}"
        )
        self.state.fp.event(
            "error_triggered_delivery",
            card=best_card.id,
            overlap=best_score,
            turn=self.state.turn,
        )

    def check_completion(self, commands: Sequence[Any], draft_text: str):
        context = TriggerContext(
            event=Event.DRAFT_READY.value,
            turn=self.state.turn,
            incoming_kind="tool",
            incoming_text="",
            extra={
                "completion_blocks": self.completion_blocks,
                "draft_calls": tuple(commands),
                "terminal_receipts": tuple(self.receipts),
            },
        )
        for checker in self.runtime.checker_bindings.get(Event.DRAFT_READY.value, []):
            try:
                bounce = checker.inspect(
                    context,
                    self.state.ledger,
                    draft_text,
                    bool(commands),
                )
            except Exception as exc:
                self.state.fp.event(
                    "checker_error",
                    checker=getattr(checker, "name", type(checker).__name__),
                    error=type(exc).__name__,
                    turn=self.state.turn,
                )
                continue
            if bounce is not None:
                self.completion_blocks += 1
                self.state.fp.event(
                    "checker_bounce",
                    checker=bounce.checker,
                    reason=bounce.reason[:160],
                    turn=self.state.turn,
                )
                return bounce

        evidence_id = next(
            (receipt.call_id for receipt in reversed(self.receipts) if not receipt.error),
            "",
        )
        if evidence_id:
            for entry in list(self.state.ledger.pending()):
                self.state.ledger.mark_done(
                    entry.id,
                    evidence_id,
                    writer=Writer.HARNESS,
                )
        else:
            self.state.fp.count("completion_allowed_without_evidence")
        self.state.fp.count("completion_allowed")
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "package_digest": self.package_digest,
            "counters": dict(self.state.fp.counters),
            "events": list(self.state.fp.events),
            "working_memory": self.state.ledger.snapshot(),
        }

    def dump(self, path: str | Path) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )


class _NoopPort:
    pass


__all__ = ["PROMPT_PREFIX_MARKER", "TerminalSkillBridge"]

