"""Builtin strategy library: the engine's side of the SPI.

Packages SELECT these by name in manifest.yaml; they never edit them.
Registry lookups happen at load time so a bad name fails at startup, not mid-run.
"""

from __future__ import annotations

from recuris.builtin.checkers import TruthProtocolChecker
from recuris.builtin.deliverers import (
    BoundaryInject,
    ExemplarBounceDeliverer,
    NeedDrivenRetrieval,
    StandingInject,
    StateReminder,
)
from recuris.builtin.embed_retrieval import EmbeddingRetrieval
from recuris.builtin.entrykinds import (
    GenericItemKind,
    KnowledgeNeedKind,
    ServiceRequestAuthKind,
    ServiceRequestKind,
)
from recuris.builtin.execution_gates import AntiEscalationGate, ExecutionGate
from recuris.builtin.managers import BlueprintThenDelta, SelfMaintainEachTurn
from recuris.errors import ProfileError

ENTRY_KINDS = {
    "service_request": ServiceRequestKind,
    "generic_item": GenericItemKind,
    "service_request_auth": ServiceRequestAuthKind,
    "knowledge_need": KnowledgeNeedKind,   # what the agent still needs to know
}
MANAGERS = {
    "self_maintain_each_turn": SelfMaintainEachTurn,
    "blueprint_then_delta": BlueprintThenDelta,
}
DELIVERERS = {
    "exemplar_bounce": ExemplarBounceDeliverer,
    "standing_inject": StandingInject,
    "need_driven_retrieval": NeedDrivenRetrieval,
    "boundary_inject": BoundaryInject,
    "state_reminder": StateReminder,
    "embedding_retrieval": EmbeddingRetrieval,   # dense retrieval; beats lexical on knowledge tasks
}
CHECKERS = {
    "truth_protocol": TruthProtocolChecker,
    "execution_gate": ExecutionGate,        # B-exec: authorized + executable + not executed -> must execute
    "anti_escalation": AntiEscalationGate,  # B-deflect: capable but deflecting to transfer -> bounce, try first
}
MATCHERS = ("receipt_binding_match", "delivery_receipt")  # built in grounding.py


def _make(registry: dict, kind: str, name: str, cfg: dict | None = None):
    try:
        cls = registry[name]
    except KeyError:
        raise ProfileError(
            f"unknown {kind} '{name}' (builtins: {sorted(registry)})"
        ) from None
    return cls(**(cfg or {}))


def make_entry_kind(name: str):
    return _make(ENTRY_KINDS, "entry_kind", name)


def make_manager(name: str, cfg: dict | None = None):
    return _make(MANAGERS, "manager", name, cfg)


def make_deliverer(name: str, cfg: dict | None = None):
    return _make(DELIVERERS, "deliverer", name, cfg)


def make_checker(name: str, cfg: dict | None = None):
    return _make(CHECKERS, "checker", name, cfg)
