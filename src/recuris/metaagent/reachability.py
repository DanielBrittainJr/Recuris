"""Deterministic carrier-reachability checks for generated Skill Memories.

The checks are capability based: they inspect the selected WM entry kind and
the benchmark tool metadata.  They do not read task answers or held-out
outcomes.
"""

from __future__ import annotations

import ast
import importlib
import sys
from functools import lru_cache

from recuris.paths import repo_root, tau2_root

ROOT = repo_root()
TAU2_ROOT = tau2_root(required=False)

# Benchmarks whose downstream is a terminal harness rather than tau2's tool
# face. They share one tool surface and one dispatched-event set, so the
# branches below test membership here rather than naming a single benchmark.
# The evolution loop in this release drives tau2 only; the distinction stays
# because the reachability rules genuinely differ between the two runtimes.
HARBOR_BENCHMARKS = frozenset({"tb21"})


def _ensure_framework_imports() -> None:
    for path in (str(ROOT / "src"), str(TAU2_ROOT / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)


@lru_cache(maxsize=None)
def entry_kind_preserves_tool(entry_kind: str) -> bool:
    """Probe whether an EntryKind retains a valid write-tool field."""
    _ensure_framework_imports()
    from recuris import builtin

    probe = "__recuris_reachability_probe__"
    try:
        kind = builtin.make_entry_kind(str(entry_kind))
        parsed = kind.parse_proposal(
            {"description": "reachability probe", "tool": probe, "params": {}},
            {"write_tools": {probe}},
        )
    except Exception:
        return False
    return isinstance(parsed, dict) and parsed.get("tool") == probe


@lru_cache(maxsize=1)
def _tool_aware_entry_kinds_frozen() -> frozenset[str]:
    """Return builtin EntryKinds whose normalized entries can carry tools."""
    _ensure_framework_imports()
    from recuris import builtin

    return frozenset({
        name for name in builtin.ENTRY_KINDS
        if entry_kind_preserves_tool(name)
    })


def tool_aware_entry_kinds() -> set[str]:
    """Return a mutable copy of the tool-preserving builtin names."""
    return set(_tool_aware_entry_kinds_frozen())


@lru_cache(maxsize=None)
def tb21_tool_capabilities(domain: str = "terminal-bench") -> dict[str, dict[str, object]]:
    """Tool face of the Terminus-2 runtime — its single terminal tool.

    TerminalSkillBridge loads the runtime with write-tool set
    {"bash_command"} (tb21/runtime_bridge.py, TerminalSkillBridge.__init__)
    and every ToolReceipt is stamped tool="bash_command"
    (observe_commands).  A terminal command can always mutate container
    state, so it is a write tool.


    `domain` is accepted for call-site parity with
    tau2_tool_capabilities(self.domain); the terminal face is the same
    regardless of the task or task set.
    """
    capabilities = {
        "bash_command": {
            "mutates_state": True,
            "tool_type": "write",
        },
    }
    if not capabilities:
        # mirrors tau2_tool_capabilities' fail-closed tail: never return empty
        raise RuntimeError(
            f"no TB2.1 tools discovered for domain {domain!r}"
        )
    return capabilities


def tb21_checker_capabilities() -> dict[str, dict[str, object]]:
    """Checker face of the TB2.1 runtime — the draft_ready hook only.

    TerminalSkillBridge dispatches checkers at exactly one event:
    check_completion() iterates checker_bindings for DRAFT_READY
    (tb21/runtime_bridge.py:103-155).  truth_protocol and execution_gate are
    event-generic builtins (recuris/builtin/__init__.py:46-50) and are
    exposed.  anti_escalation is deliberately ABSENT: its card_route targets
    a transfer tool ("transfer_to_human_agents", reachability.py:68-73) that
    does not exist in the TB tool face, so exposing it would let a plan pass
    validation and then route to an unreachable tool.  Omission fails closed
    — plan_schema.py:488-492 rejects set_checker on any name missing
    here.
    """
    return {
        "truth_protocol": {},
        "execution_gate": {},
    }


# ── dispatched-event tables ──────────────────────────────────────────────
# These two frozensets mirror what the downstream runtimes actually raise.
# A hand-maintained mirror drifts (it did: the harbor entry still claimed
# turn_start-only for weeks after _dispatch_on_retrieval started raising
# intent_recorded, which collapsed the harbor action space to the two
# turn_start carriers).  They are therefore DERIVED FROM SOURCE by
# derive_dispatched_deliverer_events() below, rather than trusted as written,
# so that editing a runtime's dispatch sites cannot leave this table quietly
# wrong.
TAU2_DISPATCHED_DELIVERER_EVENTS = frozenset(
    {"turn_start", "intent_recorded", "pre_write"}
)
# Harbor (Terminus-2) bridge: TerminalSkillBridge.prepare_prompt raises
# TURN_START and _dispatch_on_retrieval raises INTENT_RECORDED after every
# command observation.  PRE_WRITE has no dispatch site — Terminus-2 commits
# its own commands, so there is no pre-write review hook — and DRAFT_READY
# reaches CHECKERS only (check_completion iterates checker_bindings), never a
# deliverer.
HARBOR_DISPATCHED_DELIVERER_EVENTS = frozenset({"turn_start", "intent_recorded"})


def dispatched_deliverer_events_for(benchmark: str) -> frozenset[str]:
    """Deliverer events the benchmark's downstream runtime actually raises.

    The machine's DELIVERER_EVENTS names every event a deliverer may bind to,
    but each downstream runtime raises only a subset: Tau2's TurnRuntime
    dispatches all three (recuris/runtime.py: run_turn and _pre_write),
    while the harbor bridge raises turn_start and intent_recorded but never
    pre_write (adapters/tb21/runtime_bridge.py) -- a
    binding on `pre_write` never fires there.  Offline referees must judge
    ignition against this set, not the machine-wide one, or a package can
    pass the probe and then stay silent in every episode.
    """
    if benchmark in HARBOR_BENCHMARKS:
        return HARBOR_DISPATCHED_DELIVERER_EVENTS
    return TAU2_DISPATCHED_DELIVERER_EVENTS


def derive_dispatched_deliverer_events(source: str, label: str = "<source>") -> frozenset[str]:
    """Derive a runtime's deliverer-dispatch events from its own source.

    Closed-world by construction, exactly like _tau2_tool_capabilities_from_ast:
    parse rather than import, so the answer comes from the code that ships.  A
    deliverer runs at event E in a runtime if and only if that runtime either

      * calls ``..._deliver(E, ...)`` (TurnRuntime._deliver and every bridge
        that reuses it), or
      * iterates ``....deliverer_bindings.get(E, ...)`` itself (the pre_write
        checkpoint does this inline).

    The body of a ``*_deliver`` definition is skipped: the generic dispatcher
    forwards whatever event its callers hand it and raises nothing on its own.
    ``checker_bindings`` sites are deliberately not matched either: draft_ready
    reaches checkers only, and treating it as a deliverer event is the same
    class of false-positive this function exists to prevent.  A dispatch site
    whose event is not a literal raises instead of silently under-reporting.
    """
    events: set[str] = set()
    tree = ast.parse(source, filename=label)
    inside_dispatcher: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.endswith("_deliver")):
            inside_dispatcher.update(id(child) for child in ast.walk(node))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in inside_dispatcher:
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get":
            owner = func.value
            if not (isinstance(owner, ast.Attribute)
                    and owner.attr == "deliverer_bindings"):
                continue
        elif isinstance(func, ast.Attribute) and func.attr.endswith("_deliver"):
            pass
        elif isinstance(func, ast.Name) and func.id.endswith("_deliver"):
            pass
        else:
            continue
        if not node.args:
            raise RuntimeError(
                f"{label}: deliverer dispatch site with no event argument"
            )
        event = _literal_event(node.args[0])
        if event is None:
            raise RuntimeError(
                f"{label}: deliverer dispatch site at line {node.lineno} passes a "
                "non-literal event — the dispatched-event table cannot be derived"
            )
        events.add(event)
    return frozenset(events)


def _literal_event(node: ast.AST) -> str | None:
    """Read `Event.X.value` / `"x"` out of a dispatch site's first argument."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (isinstance(node, ast.Attribute) and node.attr == "value"
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "Event"):
        member = node.value.attr
        try:
            _ensure_framework_imports()
            from recuris.events import Event

            return str(Event[member].value)
        except Exception:
            return member.lower()
    return None


def tau2_checker_capabilities() -> dict[str, dict[str, object]]:
    """Return checker wiring exposed by the Tau2 runtime adapter."""
    return {
        "truth_protocol": {},
        "execution_gate": {},
        "anti_escalation": {
            "card_route": {
                "event": "draft_ready",
                "tool_cfg": "transfer_tool",
                "default_tool": "transfer_to_human_agents",
            },
        },
    }


@lru_cache(maxsize=None)
def tau2_tool_capabilities(domain: str) -> dict[str, dict[str, object]]:
    """Inspect Tau2 toolkit decorators without constructing a database."""
    _ensure_framework_imports()
    try:
        from tau2.environment.toolkit import (
            MUTATES_STATE_ATTR,
            TOOL_ATTR,
            TOOL_TYPE_ATTR,
            ToolKitBase,
        )

        module = importlib.import_module(f"tau2.domains.{domain}.tools")
        toolkits = [
            value for value in vars(module).values()
            if isinstance(value, type) and issubclass(value, ToolKitBase)
            and value is not ToolKitBase and value.__module__ == module.__name__
        ]
        capabilities: dict[str, dict[str, object]] = {}
        for toolkit in toolkits:
            for base in reversed(toolkit.__mro__):
                for name, member in vars(base).items():
                    if isinstance(member, property):
                        member = member.fget
                    if not getattr(member, TOOL_ATTR, False):
                        continue
                    tool_type = getattr(member, TOOL_TYPE_ATTR, None)
                    capabilities[name] = {
                        "mutates_state": bool(
                            getattr(member, MUTATES_STATE_ATTR, True)
                        ),
                        "tool_type": (
                            getattr(tool_type, "value", None) or str(tool_type or "")
                        ),
                    }
    except (ImportError, ModuleNotFoundError):
        # Unit-test and static-analysis environments may not have Tau2's full
        # optional dependency set.  The decorator arguments are still a
        # closed-world source of truth, so parse the same domain source
        # without importing or constructing its DB/toolkit.
        capabilities = _tau2_tool_capabilities_from_ast(domain)
    if not capabilities:
        raise RuntimeError(f"no Tau2 tools discovered for domain {domain!r}")
    return capabilities


def _tau2_tool_capabilities_from_ast(
    domain: str,
) -> dict[str, dict[str, object]]:
    path = TAU2_ROOT / "src" / "tau2" / "domains" / domain / "tools.py"
    if not path.is_file():
        raise RuntimeError(f"tau2 domain tool source is missing: {path}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    capabilities: dict[str, dict[str, object]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            fn = decorator.func
            decorator_name = (
                fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute)
                else ""
            )
            if decorator_name not in {"is_tool", "is_discoverable_tool"}:
                continue
            tool_type = "read"
            type_node = decorator.args[0] if decorator.args else None
            for keyword in decorator.keywords:
                if keyword.arg == "tool_type":
                    type_node = keyword.value
            if isinstance(type_node, ast.Attribute):
                tool_type = str(type_node.attr).lower()
            elif isinstance(type_node, ast.Constant):
                tool_type = str(type_node.value).lower()
            mutates_state = tool_type == "write"
            for keyword in decorator.keywords:
                if keyword.arg != "mutates_state":
                    continue
                if isinstance(keyword.value, ast.Constant):
                    mutates_state = bool(keyword.value.value)
                else:
                    raise RuntimeError(
                        f"{path}: {node.name} has non-literal mutates_state"
                    )
            capabilities[node.name] = {
                "mutates_state": mutates_state,
                "tool_type": tool_type,
            }
            break
    return capabilities


def tool_capabilities_for(
    benchmark: str, domain: str
) -> dict[str, dict[str, object]]:
    """Tool metadata for a domain, resolved through its benchmark.

    The benchmarks reach their tool face by different routes -- Tau2 through
    the domain's own tool module, the harbor benchmarks through the fixed
    Terminus-2 terminal surface -- so a caller holding only a domain name
    cannot pick correctly on its own.  On a harbor benchmark the `domain` is
    a TASK id, not a tau2 domain package, so routing it to the Tau2 provider
    does not merely mis-answer: it raises (missing domains/<task>/tools.py)
    and every caller inside a try/except turns that into a rejection.
    """
    if benchmark in HARBOR_BENCHMARKS:
        return tb21_tool_capabilities(domain)
    return tau2_tool_capabilities(domain)


def mutating_tau2_tools(domain: str) -> set[str]:
    return {
        name for name, record in tau2_tool_capabilities(domain).items()
        if record.get("mutates_state") is True
    }
