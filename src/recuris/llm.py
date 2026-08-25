"""LLM abstraction — lets the whole invariant loop run offline in unit tests.

The runtime never imports a vendor SDK. Adapters provide a client backed by the
host harness (tau2 -> litellm `generate`). ScriptedLLM below is the offline
stand-in: it replays a fixed script per call site, so the whole invariant loop
can be exercised without a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ToolCallSpec:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Reply:
    content: str = ""
    tool_calls: tuple[ToolCallSpec, ...] = ()


class LLMClient(Protocol):
    def complete(self, *, purpose: str, prompt_key: str = "") -> Reply:
        """Produce the next reply. The adapter closes over its own message
        state; `purpose` distinguishes call sites (ledger_update / draft /
        redraft_correction / redraft_review) for logging, and so a scripted
        stand-in can answer each call site differently."""
        ...


@dataclass
class ScriptedLLM:
    """Test double: pops replies from per-purpose queues.

    script: purpose -> list of Reply (or raw str, coerced to text Reply).
    Raises IndexError when a queue underflows — a test that consumes more
    LLM calls than scripted is a failing test, by design.
    """

    script: dict[str, list] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def complete(self, *, purpose: str, prompt_key: str = "") -> Reply:
        self.calls.append(purpose)
        queue = self.script.get(purpose)
        if not queue:
            raise IndexError(f"ScriptedLLM: no scripted reply left for {purpose!r}")
        item = queue.pop(0)
        if isinstance(item, str):
            return Reply(content=item)
        return item
