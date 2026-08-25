"""tau2-Bench adapter: plugs the Recuris runtime in as a tau2 agent.

The benchmark checkout is never modified. Registration happens at run time
(`registry.register_agent_factory`), and the orchestrator-side mechanisms --
the terminal working-memory gate and status-board injection -- attach through
the duck-typed `wm_pending_lines` / `wm_board_text` hooks this class exposes.

Fidelity contract: every LLM call site here reproduces the context composition
of tau2's own `LLMAgent.generate_next_message` exactly; see the HarnessPort
docstrings for the per-call-site correspondence. Any divergence would make the
bare and skill arms incomparable, which is the whole point of the pairing.
"""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any, List, Optional

from pydantic import ConfigDict
from tau2.agent.llm_agent import LLMAgent, LLMAgentState
from tau2.data_model.message import (
    AssistantMessage,
    MultiToolMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from tau2.environment.tool import Tool
from tau2.utils.llm_utils import generate

from recuris.grounding import ToolReceipt
from recuris.paths import skill_memory_root
from recuris.runtime import RuntimeState, TurnRuntime
from recuris.skillmemory import load_skill_memory
from recuris.writereview import NOT_EXECUTED_TEXT

# No default worth guessing: an arm that silently loaded a package the
# caller did not name would be a treatment nobody declared. The runner
# requires --skill-memory for the skill arm.
DEFAULT_SKILL_MEMORY = skill_memory_root() / "tau2_retail"
EMBED_FINGERPRINT_ENV = "RECURIS_EMBED_FINGERPRINT_V1"
EMBEDDED_FINGERPRINT_KEY = "recuris_fingerprint_v1"


def _embed_fingerprint_in_checkpoint(state, fingerprint) -> bool:
    """Attach one post-conversation fingerprint to a checkpointed message."""
    payload = {
        "sim_tag": fingerprint.sim_tag,
        "counters": dict(fingerprint.counters),
        "events": deepcopy(fingerprint.events),
    }
    for item in reversed(getattr(state, "messages", []) or []):
        raw = getattr(item, "raw_data", None)
        if isinstance(raw, dict):
            raw[EMBEDDED_FINGERPRINT_KEY] = payload
            return True
    return False


def _is_write_tool(tool) -> bool:
    """tau2 marks mutating tools via __mutates_state__ — harness introspection
    belongs to the ADAPTER, not to Skill Memory packages."""
    func = getattr(tool, "_func", None)
    if func is None:
        return False
    flag = getattr(func, "__mutates_state__", None)
    if flag is None:
        flag = getattr(getattr(func, "__func__", None), "__mutates_state__", None)
    return bool(flag)


class RecurisAgentState(LLMAgentState):
    """LLMAgentState + the framework's opaque per-conversation state."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    rs: Optional[RuntimeState] = None


class _Tau2Port:
    """HarnessPort bound to (agent, state) for one turn."""

    def __init__(self, agent: "RecurisAgent", state: RecurisAgentState):
        self.agent = agent
        self.state = state

    # ── incoming ─────────────────────────────────────────────────────────
    def incoming_kind(self, incoming: Any) -> str:
        if isinstance(incoming, UserMessage):
            c = incoming.content or ""
            if c.startswith("[SYSTEM CHECK"):
                return "system_check"
            return "user" if c else "other"
        if isinstance(incoming, (ToolMessage, MultiToolMessage)):
            return "tool"
        return "other"

    def user_text(self, incoming: Any) -> str:
        if isinstance(incoming, UserMessage):
            return incoming.content or ""
        return ""

    def genuine_user_texts(self) -> list[str]:
        """All real customer utterances so far (synthetic nudges excluded) —
        the confirmation grounder's verification corpus."""
        out = []
        for m in self.state.messages:
            if isinstance(m, UserMessage) and m.content and not m.content.startswith(
                "[SYSTEM CHECK"
            ):
                out.append(m.content)
        return out

    def read_tool_results(self) -> dict:
        """Parsed JSON of genuine READ-tool receipts, keyed by tool name — the
        feasibility oracle's objective-state source (get_reservation_details,
        get_user_details...). Synthetic/error receipts excluded; malformed JSON
        skipped. Zero fork change: reads only the agent's own message state."""
        import json as _json

        results: dict[str, list] = {}
        call_name = {}  # tool_call_id -> name
        for m in self.state.messages:
            if isinstance(m, AssistantMessage):
                for tc in m.tool_calls or []:
                    call_name[tc.id] = tc.name
        for m in self.state.messages:
            if not isinstance(m, ToolMessage) or m.error:
                continue
            name = call_name.get(m.id)
            if not name or not m.content:
                continue
            c = m.content
            if c.startswith("[WRITE REVIEW") or c.startswith("[SYSTEM CHECK"):
                continue
            try:
                results.setdefault(name, []).append(_json.loads(c))
            except Exception:
                continue
        return results

    def extract_receipts(self) -> list[ToolReceipt]:
        results: dict[str, ToolMessage] = {}
        for m in self.state.messages:
            if isinstance(m, ToolMessage):
                results[m.id] = m
        receipts: list[ToolReceipt] = []
        for m in self.state.messages:
            if not isinstance(m, AssistantMessage):
                continue
            for tc in m.tool_calls or []:
                tm = results.get(tc.id)
                if tm is None:
                    continue  # no result yet
                content = tm.content or ""
                # bank smoke 2026-07-06: the banking_knowledge discoverable-tool
                # wrapper returns semantic failures ("Error: Tool ... has not
                # been unlocked") with error=False — a receipt that must never
                # ground a DONE. Content-level backstop; domain tools that fail
                # properly set error=True and are unaffected.
                content_error = content.startswith("Error:")
                receipts.append(
                    ToolReceipt(
                        call_id=tc.id,
                        tool=tc.name,
                        args=tc.arguments or {},
                        error=bool(tm.error) or content_error,
                        content=content,
                    )
                )
        return receipts

    # ── LLM call sites (fork-parity contexts) ────────────────────────────
    def ledger_llm(self, instruction: str) -> str:
        reply = generate(
            model=self.agent.llm,
            messages=self.state.system_messages
            + self.state.messages
            + [UserMessage(role="user", content=instruction)],
            call_name="recuris_ledger_selfmaintain",
            **{
                **(self.agent.llm_args or {}),
                "temperature": getattr(self.agent.sm.manager, "temperature", 0.0),
            },
        )
        return reply.content or ""

    def llm_draft(self, wm_text: str) -> AssistantMessage:
        sys_content = self.agent.system_prompt + "\n\n" + wm_text
        return generate(
            model=self.agent.llm,
            tools=self.agent.tools,
            messages=[SystemMessage(role="system", content=sys_content)]
            + self.state.messages,
            call_name="agent_response",
            **self.agent.llm_args,
        )

    def redraft_with_correction(self, wm_text: str, correction: str) -> AssistantMessage:
        corr = self.agent.system_prompt + "\n\n" + wm_text + correction
        return generate(
            model=self.agent.llm,
            tools=self.agent.tools,
            messages=[SystemMessage(role="system", content=corr)] + self.state.messages,
            call_name="agent_response",
            **self.agent.llm_args,
        )

    def bounce_and_redraft(
        self, draft: AssistantMessage, results_by_call: dict[str, str]
    ) -> AssistantMessage:
        for tc in draft.tool_calls or []:
            self.state.messages.append(
                ToolMessage(
                    id=tc.id,
                    role="tool",
                    content=results_by_call.get(tc.id, NOT_EXECUTED_TEXT),
                    requestor="assistant",
                    error=False,
                )
            )
        reviewed = generate(
            model=self.agent.llm,
            tools=self.agent.tools,
            messages=self.state.system_messages + self.state.messages,
            call_name="agent_response",
            **self.agent.llm_args,
        )
        self.state.messages.append(reviewed)
        return reviewed

    # ── draft plumbing ───────────────────────────────────────────────────
    def commit(self, draft: AssistantMessage) -> None:
        self.state.messages.append(draft)

    def draft_content(self, draft: AssistantMessage) -> str:
        return draft.content or ""

    def draft_tool_calls(self, draft: AssistantMessage):
        return list(draft.tool_calls or [])

    def set_draft_content(self, draft: AssistantMessage, text: str) -> None:
        draft.content = text


class RecurisAgent(LLMAgent):
    """tau2 agent whose turn loop is the recuris invariant runtime."""

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        llm: str,
        llm_args: Optional[dict] = None,
        skill_memory: Optional[str] = None,
    ):
        super().__init__(
            tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args
        )
        root = (
            skill_memory
            or os.getenv("RECURIS_SKILL_MEMORY")
            or os.getenv("RECURIS_PROFILE")  # legacy alias
            or str(DEFAULT_SKILL_MEMORY)
        )
        # env overrides are tau2-harness vocabulary -> applied HERE, never by
        # the generic loader (audit fix #2)
        from recuris.skillmemory import apply_tau2_env_overrides

        self.sm = apply_tau2_env_overrides(load_skill_memory(root))
        write_tools = {t.name for t in tools if _is_write_tool(t)}
        self.runtime = TurnRuntime(self.sm, write_tools)
        self._sim_tag = ""  # set by factory when task info is available (audit fix #13)

    # ── state ────────────────────────────────────────────────────────────
    def get_init_state(self, message_history=None) -> RecurisAgentState:
        base = super().get_init_state(message_history=message_history)
        return RecurisAgentState(
            system_messages=base.system_messages,
            messages=base.messages,
            rs=self.runtime.new_state(sim_tag=self._sim_tag),
        )

    # ── main turn ────────────────────────────────────────────────────────
    def generate_next_message(self, message, state):
        if isinstance(message, UserMessage) and message.is_audio:
            raise ValueError("User message cannot be audio.")
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)
        port = _Tau2Port(self, state)
        final = self.runtime.run_turn(state.rs, port, message)
        return final, state

    # ── harness hooks (duck-typed by the fork's orchestrator) ────────────
    def wm_pending_lines(self, state) -> list[str]:
        rs = getattr(state, "rs", None)
        return self.runtime.pending_lines(rs) if rs else []

    def wm_board_text(self, state) -> Optional[str]:
        rs = getattr(state, "rs", None)
        return self.runtime.board_text(rs) if rs else None

    # ── lifecycle ────────────────────────────────────────────────────────
    def stop(self, message=None, state=None) -> None:
        rs = getattr(state, "rs", None)
        if rs is not None:
            # Optionally make the fingerprint part of Tau2's durable
            # trajectory checkpoint. ``stop`` runs after the conversation but
            # before Tau2 deep-copies the trajectory into SimulationRun, so
            # neither model can observe this metadata.
            if os.getenv(EMBED_FINGERPRINT_ENV) == "1":
                _embed_fingerprint_in_checkpoint(state, rs.fp)
            rs.fp.dump()  # RECURIS_FINGERPRINT jsonl, one line per conversation
        super().stop(message, state)


def create_recuris_agent(tools, domain_policy, **kwargs):
    """Factory for registry.register_agent_factory(..., 'recuris_agent')."""
    agent = RecurisAgent(
        tools=tools,
        domain_policy=domain_policy,
        llm=kwargs.get("llm"),
        llm_args=kwargs.get("llm_args"),
    )
    task = kwargs.get("task")
    tid = getattr(task, "id", None)
    if tid is not None:
        agent._sim_tag = f"task{tid}"  # fingerprint lines become attributable
    return agent
