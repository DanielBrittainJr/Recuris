"""Official Terminus-2 with a thin Recuris phase adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harbor.agents.terminus_2.terminus_2 import Terminus2

from recuris.adapters.tb21.runtime_bridge import TerminalSkillBridge

FINGERPRINT_FILE = "recuris_fingerprint.json"

# Terminus treats any feedback containing "ERROR:" as a parser failure and
# skips command execution for that turn.  Checker corrections quote untrusted
# text (the model's own reply, the draft, WM requirements seeded from the task
# instruction), so a bounce can carry that substring and silently cost a turn.
_PARSER_ERROR_TOKEN = "ERROR:"
_PARSER_ERROR_SAFE = "ERROR∶"  # ratio colon: reads the same, never matches


class RecurisTerminus2(Terminus2):
    """Keep the official agent loop and attach Recuris at existing hooks.

    ``BRIDGE`` names the phase bridge to construct.  It is a class attribute
    only so a sibling terminal benchmark on the same harness can subclass this
    agent with its own (relabelled) bridge; the default is the bridge TB2.1
    always constructed, so TB2.1's behaviour is unchanged.
    """

    BRIDGE = TerminalSkillBridge

    def __init__(
        self,
        *args: Any,
        skill_memory: str,
        skill_memory_digest: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._recuris = self.BRIDGE(skill_memory, skill_memory_digest)
        self._recuris_instruction = ""

    def version(self) -> str | None:
        return "2.0.0-recuris"

    def _reset_per_run_state(self) -> None:
        super()._reset_per_run_state()
        if hasattr(self, "_recuris"):
            self._recuris.reset()

    async def _handle_llm_interaction(self, chat, prompt: str, *args, **kwargs):
        # Harbor changed this signature between releases (0.7.x carried a
        # logging_paths argument that 0.20 dropped). Only ``prompt`` is ours to
        # rewrite, so take it by name and forward everything else untouched;
        # the override then works against either version.
        if not self._recuris.instruction:
            original = kwargs.get("original_instruction")
            if not original:
                original = next((a for a in args if isinstance(a, str)), "")
            self._recuris.start(original or self._recuris_instruction)
        recuris_prompt = self._recuris.prepare_prompt(prompt)
        result = await super()._handle_llm_interaction(
            chat, recuris_prompt, *args, **kwargs
        )
        commands, is_complete, feedback, analysis, plan, llm_response = result
        if is_complete and "ERROR:" not in feedback:
            bounce = self._recuris.check_completion(commands, llm_response.content)
            if bounce is not None:
                is_complete = False
                warning = bounce.correction.replace(
                    _PARSER_ERROR_TOKEN, _PARSER_ERROR_SAFE
                )
                feedback = (
                    f"{feedback}\nWARNINGS: {warning}"
                    if feedback
                    else f"WARNINGS: {warning}"
                )
        return commands, is_complete, feedback, analysis, plan, llm_response

    async def _execute_commands(self, commands, session):
        timed_out, terminal_output = await super()._execute_commands(commands, session)
        self._recuris.observe_commands(
            commands,
            terminal_output,
            timed_out=timed_out,
        )
        return timed_out, terminal_output

    async def run(self, instruction, environment, context) -> None:
        self._recuris_instruction = instruction
        try:
            await super().run(instruction, environment, context)
        finally:
            self._recuris.dump(Path(self.logs_dir) / FINGERPRINT_FILE)


__all__ = ["RecurisTerminus2"]
