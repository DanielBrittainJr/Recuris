"""Task-neutral W and C strategies for the TerminalBench 2.1 seed memory."""

from __future__ import annotations

import re

from recuris.checkpoint import Bounce
from recuris.events import Event
from recuris.wm.selfmaintain import parse_model_reply


class TerminalBlueprintOnce:
    """Build one compact acceptance checklist at the start of a terminal task."""

    name = "terminal_blueprint_once"
    temperature = 0.0

    def should_update(self, ctx) -> bool:
        return ctx.turn == 1 and ctx.incoming_kind == "user"

    def build_instruction(self, ledger, env: dict) -> str:
        return f"""[INTERNAL WORKING-MEMORY UPDATE]
Read the task instruction and terminal state in the conversation. Extract the
smallest complete list of observable acceptance requirements that must hold
before the task can be declared complete.

Current checklist:
{ledger.pending_json()}

Output ONLY this JSON shape:
{{"requests": [{{"id": "req-1", "description": "one atomic acceptance requirement"}}]}}

Rules:
- Record required artifacts, behavior, constraints, and explicit validation commands.
- Do not propose implementation steps or invent hidden grader requirements.
- Keep at most {ledger.schema.max_entries} concise items.
"""

    def parse_reply(self, raw_reply: str) -> list:
        return parse_model_reply(raw_reply)

    def seed_requests(self, instruction: str) -> list[dict]:
        compact = " ".join(str(instruction or "").split())
        return [{"description": compact[:2000]}] if compact else []


_MUTATION_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|mv|cp|mkdir|touch|chmod|chown|ln|install|"
    r"apt(?:-get)?\s+(?:install|remove)|pip\s+install|npm\s+(?:install|i)|"
    r"yarn\s+(?:add|install)|pnpm\s+(?:add|install)|cargo\s+build|"
    r"make(?!\s+(?:test|check))|cmake|git\s+(?:apply|commit|checkout)|"
    r"sed\s+-i|perl\s+-pi)\b|"
    r"(?:^|[;&|]\s*)(?:cat|echo|printf)\b[^\n]*(?:>>?|\|\s*tee\b)",
    re.IGNORECASE,
)
_VERIFY_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:pytest|"
    r"python(?:3(?:\.\d+)?)?\s+(?:-m\s+(?:pytest|unittest)|"
    r"(?:\S*/)?[\w.-]*(?:test|check|verify)[\w.-]*\.py)\b|npm\s+test|"
    r"yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|make\s+(?:test|check)|"
    r"ctest|test\s|diff\s|cmp\s|grep\s|rg\s|find\s|ls\s|stat\s|file\s|"
    r"head\s|tail\s|cat\s)",
    re.IGNORECASE,
)
_FAILURE_RE = re.compile(
    r"traceback|command not found|no such file|permission denied|\bfailed\b|\berror:",
    re.IGNORECASE,
)
_SUCCESS_OVERRIDE_RE = re.compile(
    r"\b0 failed\b|all tests passed|tests? passed|\bok\b",
    re.IGNORECASE,
)


class TerminalCompletionChecker:
    """Bounded gate requiring observed post-change verification before finish."""

    name = "terminal_completion"
    events = (Event.DRAFT_READY.value,)

    def __init__(self, max_blocks: int = 2) -> None:
        self.max_blocks = max(0, int(max_blocks))

    @staticmethod
    def _command(receipt) -> str:
        return str((getattr(receipt, "args", {}) or {}).get("keystrokes") or "")

    @staticmethod
    def _requirements(wm_view) -> str:
        items = [entry.description for entry in wm_view.pending()]
        return "\n".join(f"- {item}" for item in items) or "- Recheck the task instruction."

    def _bounce(self, reason: str, wm_view) -> Bounce:
        correction = f"""The completion declaration was paused: {reason}
Before declaring completion again, inspect the actual environment, run the
strongest task-provided or project-native validation available after the latest
change, and read its output. Check these acceptance requirements:
{self._requirements(wm_view)}
Do not merely state that verification was done; execute it in the terminal."""
        return Bounce(
            checker=self.name,
            reason=reason,
            style="system_correction",
            correction=correction,
        )

    def inspect(self, ctx, wm_view, draft_text: str, draft_has_calls: bool):
        if ctx.event != Event.DRAFT_READY.value:
            return None
        if int(ctx.extra.get("completion_blocks") or 0) >= self.max_blocks:
            return None
        if draft_has_calls:
            return self._bounce(
                "the final command batch has not executed yet, so its result is not evidence",
                wm_view,
            )

        receipts = list(ctx.extra.get("terminal_receipts") or [])
        if not receipts:
            return self._bounce("no terminal action has been observed", wm_view)

        latest_mutation = -1
        latest_verification = -1
        verification_output = ""
        for index, receipt in enumerate(receipts):
            command = self._command(receipt)
            if _MUTATION_RE.search(command):
                latest_mutation = index
                continue
            if _VERIFY_RE.search(command):
                latest_verification = index
                verification_output = str(getattr(receipt, "content", "") or "")

        if latest_verification < latest_mutation:
            return self._bounce(
                "no observed validation was run after the latest environment change",
                wm_view,
            )
        if latest_verification < 0:
            return self._bounce("no observable validation command was found", wm_view)
        if (
            _FAILURE_RE.search(verification_output)
            and not _SUCCESS_OVERRIDE_RE.search(verification_output)
        ):
            return self._bounce("the latest validation output still shows a failure", wm_view)
        return None
