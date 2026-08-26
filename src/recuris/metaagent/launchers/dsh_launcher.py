"""Adapt one Recuris Meta-Agent phase to DeepSeek Harness headless mode.

DSH persists a richer native event log than the public Recuris driver consumes.
This launcher streams the relevant DSH events into the driver's existing Claude
stream-json audit format, while retaining native timing/token data as an extra
``dsh_metrics`` record.  No model context is resumed between invocations.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CLAUDE_TO_DSH = {
    "Read": "read",
    "Glob": "glob",
    "Grep": "grep",
    "Edit": "edit",
    "Write": "write",
    "Context": "context",
}
DSH_TO_CLAUDE = {value: key for key, value in CLAUDE_TO_DSH.items()}
REASONING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
SEARCHABLE_CONTEXT_VERSION = "prime-search-v1"


def _write_jsonl(handle: Any, value: dict[str, Any]) -> None:
    handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    handle.flush()


def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "-" for char in value)
    return cleaned.strip("-")[:80] or "session"


def _dsh_command() -> list[str]:
    explicit = os.environ.get("RECURIS_DSH_BIN", "").strip()
    candidates = [explicit] if explicit else []
    if os.name == "nt":
        candidates.extend(filter(None, [shutil.which("dsh.ps1"), shutil.which("dsh.cmd")]))
    candidates.extend(filter(None, [shutil.which("dsh")]))
    if not candidates:
        raise RuntimeError("the 'dsh' CLI is not on PATH; set RECURIS_DSH_BIN")
    executable = str(Path(candidates[0]).resolve())
    suffix = Path(executable).suffix.lower()
    if suffix == ".ps1":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            raise RuntimeError("a dsh.ps1 launcher was found but PowerShell is unavailable")
        return [shell, "-NoLogo", "-NoProfile", "-File", executable]
    if suffix in {".cmd", ".bat"}:
        command = shutil.which("cmd.exe") or os.environ.get("COMSPEC")
        if not command:
            raise RuntimeError("a dsh.cmd launcher was found but cmd.exe is unavailable")
        return [command, "/d", "/c", executable]
    return [executable]


def _model_settings(model: str, port: int, reasoning: str) -> dict[str, Any]:
    context_window = int(os.environ.get("RECURIS_DSH_CONTEXT_WINDOW", "65536"))
    max_tokens = int(os.environ.get("RECURIS_DSH_MAX_TOKENS", "8192"))
    efforts = {level: (None if level == "off" else level) for level in sorted(REASONING_LEVELS)}
    return {
        "agent-default-model": {
            "provider": "recuris-proxy",
            "model": model,
            "reasoningEffort": reasoning,
        },
        "llm-pi-ai": {
            "providers": {
                "recuris-proxy": {
                    "displayName": "Recuris Meta-Agent Proxy",
                    "apiKeyEnv": "RECURIS_DSH_PROXY_KEY",
                    "api": "anthropic-messages",
                    "baseURL": f"http://127.0.0.1:{port}",
                    "defaultContextWindow": context_window,
                    "defaultMaxTokens": max_tokens,
                    "defaultInput": ["text"],
                    "streamIdleTimeoutMs": int(
                        os.environ.get("RECURIS_DSH_STREAM_IDLE_MS", "300000")
                    ),
                    "compat": {"supportsTemperature": True},
                    "models": [
                        {
                            "id": model,
                            "name": model,
                            "contextWindow": context_window,
                            "maxTokens": max_tokens,
                            "input": ["text"],
                            "reasoningEfforts": efforts,
                        }
                    ],
                }
            }
        },
    }


def _patch_text(guard_url: str) -> str:
    disabled = [
        "attachment-local",
        "session-title-llm",
        "user-questions",
        "tool-bash",
        "tool-pwsh",
        "tool-jobs",
        "tool-skill",
        "plan-mode",
        "command-goal",
        "subagent-fork-in-process",
        "tool-subagent-control",
        "tool-subagent-list-agents",
        "tool-subagent",
        "tool-subagent-fork",
        "tool-subagent-report",
        "workflow-worker-thread",
        "tool-workflow",
        "tool-todo",
        "tool-goal",
        "tool-ralph",
        "tool-str-replace-editor",
        "web",
        "web-search-deepseek",
        "tool-web",
        "session-telemetry-otel",
    ]
    lines = [
        "- id: session-persistence-jsonl",
        "  config:",
        "    root: !!js process.env.RECURIS_DSH_SESSION_ROOT",
        "    compression: none",
        "    packChunks: false",
        "- id: tools",
        "  config:",
        "    mode: native",
        "- id: tool-fs",
        "  config:",
        "    readLimit: !!js Number(process.env.RECURIS_DSH_READ_LIMIT)",
        "- id: agent-loop",
        "  config:",
        "    maxParallelToolCalls: 1",
        "- id: approval",
        "  config:",
        "    policy: never",
        "- id: permission",
        "  config:",
        "    defaultPreset: recuris-workspace",
        "    presets:",
        "      recuris-workspace:",
        "        sandbox: workspace-write",
        "        approval: never",
        "- id: headless-runner",
        "  config:",
        "    task: !!js process.getBuiltinModule('fs').readFileSync(process.env.RECURIS_DSH_PROMPT_FILE, 'utf8')",
    ]
    for entry_id in disabled:
        lines.extend([f"- id: {entry_id}", "  disabled: true"])
    lines.extend(
        [
            "- insert:",
            "  - id: recuris-dsh-scope-guard",
            f"    name: {json.dumps(guard_url)}",
        ]
    )
    return "\n".join(lines) + "\n"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, default=str)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
        elif block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "image":
            parts.append("[image omitted from Recuris audit projection]")
        else:
            parts.append(json.dumps(block, ensure_ascii=False, default=str))
    return "\n".join(part for part in parts if part)


def _arguments(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return {"_invalid_dsh_arguments": str(value)}
    return dict(parsed) if isinstance(parsed, dict) else {"_invalid_dsh_arguments": str(value)}


def _argument_summary(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(
        arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    fingerprint = f"{tool}\0{serialized}".encode("utf-8")
    summary: dict[str, Any] = {
        "argumentBytes": len(serialized.encode("utf-8")),
        "requestHash": hashlib.sha256(fingerprint).hexdigest()[:16],
    }
    if tool == "read":
        fields = {
            "file_path": "path",
            "offset": "offset",
            "limit": "limit",
            "query": "query",
            "case_sensitive": "caseSensitive",
            "context_lines": "contextLines",
            "max_matches": "maxMatches",
        }
    elif tool == "glob":
        fields = {"path": "path", "pattern": "pattern"}
    elif tool == "grep":
        fields = {"path": "path", "pattern": "pattern", "include": "include"}
    elif tool == "context":
        fields = {
            "operation": "operation",
            "query": "query",
            "key": "key",
            "max_results": "maxResults",
            "context_chars": "contextChars",
        }
    else:
        fields = {"file_path": "path"}
    for source, target in fields.items():
        if source in arguments:
            summary[target] = arguments[source]
    for source, target in (
        ("content", "contentBytes"),
        ("old_string", "oldStringBytes"),
        ("new_string", "newStringBytes"),
    ):
        if isinstance(arguments.get(source), str):
            summary[target] = len(arguments[source].encode("utf-8"))
    for source, target in (("value", "valueBytes"), ("question", "questionBytes")):
        if isinstance(arguments.get(source), str):
            summary[target] = len(arguments[source].encode("utf-8"))
    return summary


@dataclass
class Projection:
    requested_model: str
    started_monotonic: float
    input_tokens: int = 0
    output_tokens: int = 0
    final_text: str = ""
    resolved_models: set[str] = field(default_factory=set)
    tool_started: dict[str, dict[str, Any]] = field(default_factory=dict)
    step_started: dict[tuple[int, int], int] = field(default_factory=dict)
    tool_timings: list[dict[str, Any]] = field(default_factory=list)
    model_timings: list[dict[str, Any]] = field(default_factory=list)
    turn_reason: Any = None
    session_id: str = ""
    created_at_ms: int | None = None
    last_event_ms: int | None = None
    parent_session_id: str | None = None
    session_origin: str | None = None
    delegation_depth: int | None = None

    def convert(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        event_type = row.get("type")
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        event_time = row.get("time")
        if isinstance(event_time, int):
            self.last_event_ms = event_time
        if event_type == "session":
            self.session_id = str(row.get("id") or "")
            created = row.get("createdAt")
            self.created_at_ms = created if isinstance(created, int) else None
            parent = row.get("parentSession")
            self.parent_session_id = str(parent) if parent is not None else None
            origin = row.get("origin")
            self.session_origin = str(origin) if origin is not None else None
            depth = row.get("delegationDepth")
            self.delegation_depth = depth if isinstance(depth, int) else None
            return []
        if event_type == "step/start":
            turn, step = data.get("turn"), data.get("step")
            if isinstance(turn, int) and isinstance(step, int) and isinstance(event_time, int):
                self.step_started[(turn, step)] = event_time
            return []
        if event_type == "tool/call":
            call_id = str(data.get("callId") or "")
            if call_id and isinstance(event_time, int):
                dsh_name = str(data.get("name") or "")
                arguments = _arguments(data.get("arguments"))
                self.tool_started[call_id] = {
                    "tool": dsh_name,
                    "startedAtMs": event_time,
                    "turn": data.get("turn"),
                    "step": data.get("step"),
                    "arguments": _argument_summary(dsh_name, arguments),
                }
            return []
        if event_type == "assistant/message":
            return self._assistant(data, row)
        if event_type == "tool/result":
            return self._tool_result(data, row)
        if event_type == "turn/end":
            self.turn_reason = data.get("reason")
        return []

    def _assistant(self, data: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        source = message.get("source") if isinstance(message.get("source"), dict) else {}
        resolved_model = str(source.get("model") or self.requested_model)
        if resolved_model:
            self.resolved_models.add(resolved_model)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        input_tokens = usage.get("inputTokens")
        output_tokens = usage.get("outputTokens")
        if isinstance(input_tokens, int):
            self.input_tokens += input_tokens
        if isinstance(output_tokens, int):
            self.output_tokens += output_tokens

        content: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text") or "")
                content.append({"type": "text", "text": text})
                if text:
                    text_parts.append(text)
            elif block.get("type") == "tool-call":
                dsh_name = str(block.get("name") or "")
                arguments = _arguments(block.get("arguments"))
                content.append(
                    {
                        "type": "tool_use",
                        "id": str(block.get("id") or ""),
                        "name": DSH_TO_CLAUDE.get(dsh_name, dsh_name),
                        "input": arguments,
                    }
                )
        if text_parts:
            self.final_text = "".join(text_parts)
        turn, step, event_time = data.get("turn"), data.get("step"), row.get("time")
        if isinstance(turn, int) and isinstance(step, int) and isinstance(event_time, int):
            started = self.step_started.get((turn, step))
            if started is not None:
                self.model_timings.append(
                    {
                        "turn": turn,
                        "step": step,
                        "startedAtMs": started,
                        "assistantAtMs": event_time,
                        "elapsedMs": max(0, event_time - started),
                        "inputTokens": input_tokens if isinstance(input_tokens, int) else None,
                        "outputTokens": output_tokens if isinstance(output_tokens, int) else None,
                    }
                )
        return [
            {
                "type": "assistant",
                "message": {"model": resolved_model, "content": content},
                "_dsh": {
                    "seq": row.get("seq"),
                    "time": row.get("time"),
                    "usage": usage,
                },
            }
        ]

    def _tool_result(self, data: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        blocks: list[dict[str, Any]] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool-result":
                continue
            call_id = str(block.get("toolCallId") or "")
            is_error = bool(block.get("isError"))
            content = _content_text(block.get("content"))
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "is_error": is_error,
                    "content": content,
                }
            )
            started = self.tool_started.get(call_id)
            event_time = row.get("time")
            if started is not None and isinstance(event_time, int):
                dsh_name = str(started.get("tool") or "")
                started_at = int(started["startedAtMs"])
                self.tool_timings.append(
                    {
                        "callId": call_id,
                        "tool": DSH_TO_CLAUDE.get(dsh_name, dsh_name),
                        "turn": started.get("turn"),
                        "step": started.get("step"),
                        "startedAtMs": started_at,
                        "finishedAtMs": event_time,
                        "elapsedMs": max(0, event_time - started_at),
                        "isError": is_error,
                        "resultTextBytes": len(content.encode("utf-8")),
                        "resultTextLines": content.count("\n") + (1 if content else 0),
                        "arguments": started.get("arguments") or {},
                    }
                )
        if not blocks:
            return []
        return [
            {
                "type": "user",
                "message": {"content": blocks},
                "_dsh": {"seq": row.get("seq"), "time": row.get("time")},
            }
        ]

    def retrieval_metrics(self) -> dict[str, Any]:
        retrieval = [
            timing for timing in self.tool_timings
            if timing.get("tool") in {"Read", "Glob", "Grep"}
        ]
        counts = {tool: 0 for tool in ("Read", "Glob", "Grep")}
        seen: set[str] = set()
        repeated = 0
        paths: set[str] = set()
        bounded_reads = 0
        query_reads = 0
        ordinary_default_reads = 0
        for timing in retrieval:
            tool = str(timing.get("tool"))
            counts[tool] += 1
            arguments = timing.get("arguments") if isinstance(timing.get("arguments"), dict) else {}
            request_hash = str(arguments.get("requestHash") or "")
            if request_hash in seen:
                repeated += 1
            elif request_hash:
                seen.add(request_hash)
            path = arguments.get("path")
            if isinstance(path, str) and path:
                paths.add(path)
            if tool == "Read" and arguments.get("limit") is not None:
                bounded_reads += 1
            if tool == "Read" and isinstance(arguments.get("query"), str):
                query_reads += 1
            if (
                tool == "Read"
                and arguments.get("limit") is None
                and not isinstance(arguments.get("query"), str)
            ):
                ordinary_default_reads += 1
        first_read = next(
            (index for index, timing in enumerate(retrieval) if timing.get("tool") == "Read"),
            len(retrieval),
        )
        searches_before_read = sum(
            1 for timing in retrieval[:first_read]
            if timing.get("tool") in {"Glob", "Grep"}
        )
        return {
            "patternVersion": SEARCHABLE_CONTEXT_VERSION,
            "calls": len(retrieval),
            "callsByTool": counts,
            "firstTool": retrieval[0].get("tool") if retrieval else None,
            "searchCallsBeforeFirstRead": searches_before_read,
            "queryReadCalls": query_reads,
            "explicitLimitReadCalls": bounded_reads,
            "ordinaryDefaultReadCalls": ordinary_default_reads,
            "repeatedRequestCalls": repeated,
            "uniquePaths": sorted(paths),
            "resultTextBytes": sum(int(timing.get("resultTextBytes") or 0) for timing in retrieval),
            "resultTextLines": sum(int(timing.get("resultTextLines") or 0) for timing in retrieval),
            "elapsedMs": sum(int(timing.get("elapsedMs") or 0) for timing in retrieval),
            "errorCalls": sum(1 for timing in retrieval if timing.get("isError")),
        }

    def context_metrics(self) -> dict[str, Any]:
        calls = [
            timing for timing in self.tool_timings
            if timing.get("tool") == "Context"
        ]
        counts: dict[str, int] = {}
        for timing in calls:
            arguments = timing.get("arguments")
            operation = (
                str(arguments.get("operation") or "unknown")
                if isinstance(arguments, dict) else "unknown"
            )
            counts[operation] = counts.get(operation, 0) + 1
        return {
            "calls": len(calls),
            "callsByOperation": counts,
            "elapsedMs": sum(int(timing.get("elapsedMs") or 0) for timing in calls),
            "resultTextBytes": sum(
                int(timing.get("resultTextBytes") or 0) for timing in calls
            ),
            "errorCalls": sum(1 for timing in calls if timing.get("isError")),
        }


class SessionTail:
    def __init__(self, root: Path, projection: Projection, output: Any) -> None:
        self.root = root
        self.projection = projection
        self.output = output
        self.path: Path | None = None
        self.offset = 0
        self.pending = b""

    def poll(self, *, final: bool = False) -> None:
        if self.path is None:
            matches = sorted(self.root.rglob("session.jsonl")) if self.root.exists() else []
            if matches:
                # The root session is created before any Context worker. Keep
                # tailing that first log even when one-shot child sessions are
                # added later; they are projected separately after settlement.
                self.path = matches[0]
        if self.path is None:
            return
        with self.path.open("rb") as source:
            source.seek(self.offset)
            chunk = source.read()
        if chunk:
            self.offset += len(chunk)
            self.pending += chunk
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        if final and self.pending.strip():
            lines.append(self.pending)
            self.pending = b""
        for raw in lines:
            if not raw.strip():
                continue
            row = json.loads(raw.decode("utf-8", errors="strict"))
            for projected in self.projection.convert(row):
                _write_jsonl(self.output, projected)


def _project_session(path: Path, requested_model: str) -> Projection:
    projection = Projection(requested_model=requested_model, started_monotonic=0.0)
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                projection.convert(json.loads(line))
    return projection


def _child_session_metrics(
    session_root: Path, root_path: Path | None, requested_model: str
) -> tuple[list[dict[str, Any]], list[Projection]]:
    values: list[dict[str, Any]] = []
    projections: list[Projection] = []
    root_resolved = root_path.resolve() if root_path is not None else None
    for session_path in sorted(session_root.rglob("session.jsonl")):
        if root_resolved is not None and session_path.resolve() == root_resolved:
            continue
        child = _project_session(session_path, requested_model)
        if child.session_origin != "subagent" and child.parent_session_id is None:
            continue
        projections.append(child)
        values.append(
            {
                "sessionId": child.session_id,
                "parentSessionId": child.parent_session_id,
                "delegationDepth": child.delegation_depth,
                "rawSession": str(session_path),
                "inputTokens": child.input_tokens,
                "outputTokens": child.output_tokens,
                "resolvedModels": sorted(child.resolved_models),
                "turnReason": child.turn_reason,
                "modelTimings": child.model_timings,
                "toolTimings": child.tool_timings,
                "retrieval": child.retrieval_metrics(),
            }
        )
    return values, projections


def _version(command: list[str], environment: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            [*command, "--version"],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/pid", str(process.pid), "/t", "/f"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)


def _price(tokens: int, env_name: str) -> float:
    return (tokens / 1_000_000) * float(os.environ.get(env_name, "0"))


def _context_state_metrics(value: dict[str, Any]) -> dict[str, Any]:
    entries = value.get("entries") if isinstance(value.get("entries"), dict) else {}
    entry_metrics = []
    for key, raw in sorted(entries.items()):
        entry = raw if isinstance(raw, dict) else {}
        text = str(entry.get("value") or "")
        entry_metrics.append(
            {
                "key": str(key),
                "valueBytes": len(text.encode("utf-8")),
                "updatedAtMs": entry.get("updatedAtMs"),
            }
        )
    operations = (
        value.get("operations") if isinstance(value.get("operations"), list) else []
    )
    return {
        "version": value.get("version"),
        "createdAtMs": value.get("createdAtMs"),
        "updatedAtMs": value.get("updatedAtMs"),
        "entries": entry_metrics,
        "operations": operations,
    }


def run(argv: list[str]) -> int:
    if len(argv) != 8:
        raise RuntimeError(
            "usage: dsh_launcher.py <prompt_file> <jsonl_out> <tools> "
            "<allowed_rules> <model> <proxy_port> <reasoning> <settings_path>"
        )
    prompt_file, out_file, tools_csv, _allowed_rules, model, port_text, reasoning, scope_file = argv
    if not model.strip():
        raise RuntimeError("Recuris did not name the Meta-Agent model")
    if reasoning not in REASONING_LEVELS:
        raise RuntimeError(f"unsupported reasoning effort for DSH: {reasoning}")
    port = int(port_text)
    workspace = Path(os.environ.get("RECURIS_WORKSPACE") or Path(__file__).resolve().parents[4]).resolve()
    output_path = Path(out_file).resolve()
    error_path = output_path.with_suffix(".err")
    guard_path = Path(__file__).with_name("dsh_scope_guard.mjs").resolve()
    run_root = Path(
        os.environ.get("RECURIS_DSH_RUN_ROOT")
        or Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "Recuris" / "dsh-runs"
    ).resolve()
    run_home = run_root / f"{_safe_name(output_path.stem)}-{uuid.uuid4().hex[:10]}"
    session_root = run_home / "sessions"
    run_home.mkdir(parents=True, exist_ok=False)
    session_root.mkdir(parents=True, exist_ok=True)
    settings_path = run_home / "settings.yaml"
    patch_path = run_home / "recuris.patch.yml"
    scope_report = run_home / "scope-report.json"
    context_state = run_home / "context-state.json"
    dsh_stdout = run_home / "dsh.stdout.txt"
    dsh_stderr = run_home / "dsh.stderr.txt"
    settings_path.write_text(
        json.dumps(_model_settings(model, port, reasoning), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    patch_path.write_text(_patch_text(guard_path.as_uri()), encoding="utf-8")

    command = _dsh_command()
    environment = os.environ.copy()
    environment.update(
        {
            "DSH_HOME": str(run_home),
            "DSH_PERMISSION_MODE": "workspace-write",
            "DSH_TOOLS_MODE": "native",
            "DSH_TELEMETRY_MODE": "DISABLED",
            "RECURIS_DSH_PROXY_KEY": "recuris-loopback",
            "RECURIS_DSH_PROMPT_FILE": str(Path(prompt_file).resolve()),
            "RECURIS_DSH_SESSION_ROOT": str(session_root),
            "RECURIS_DSH_SCOPE_FILE": str(Path(scope_file).resolve()),
            "RECURIS_DSH_SCOPE_REPORT": str(scope_report),
            "RECURIS_DSH_CONTEXT_STATE": str(context_state),
            "RECURIS_DSH_TOOLS": tools_csv,
            "RECURIS_DSH_WORKSPACE": str(workspace),
            "RECURIS_DSH_READ_LIMIT": str(
                int(os.environ.get("RECURIS_DSH_READ_LIMIT", "240"))
            ),
            "RECURIS_DSH_CONTEXT_CHILD_MAX_TOKENS": str(
                int(os.environ.get("RECURIS_DSH_CONTEXT_CHILD_MAX_TOKENS", "1536"))
            ),
            "RECURIS_DSH_CONTEXT_CHILD_TIMEOUT_MS": str(
                int(os.environ.get("RECURIS_DSH_CONTEXT_CHILD_TIMEOUT_MS", "20000"))
            ),
            "RECURIS_DSH_CONTEXT_MAX_DELEGATIONS": str(
                int(os.environ.get("RECURIS_DSH_CONTEXT_MAX_DELEGATIONS", "3"))
            ),
        }
    )
    dsh_version = _version(command, environment)
    requested_tools = [tool.strip() for tool in tools_csv.split(",") if tool.strip()]
    unknown_tools = [tool for tool in requested_tools if tool not in CLAUDE_TO_DSH]
    if unknown_tools:
        raise RuntimeError(f"unsupported Recuris tool surface: {unknown_tools}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    projection = Projection(requested_model=model, started_monotonic=time.monotonic())
    started_wall = time.time()
    with output_path.open("w", encoding="utf-8", newline="\n") as output:
        _write_jsonl(
            output,
            {
                "type": "system",
                "subtype": "init",
                "model": model,
                "tools": requested_tools,
                "_dsh": {"version": dsh_version, "runHome": str(run_home)},
            },
        )
        with dsh_stdout.open("wb") as stdout_handle, dsh_stderr.open("wb") as stderr_handle:
            process = subprocess.Popen(
                [*command, "--profile", "headless", "--patch", str(patch_path), "prompt-from-file"],
                cwd=str(workspace),
                env=environment,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt" else 0
                ),
                start_new_session=os.name != "nt",
            )
            tail = SessionTail(session_root, projection, output)
            try:
                while process.poll() is None:
                    tail.poll()
                    time.sleep(0.10)
                tail.poll(final=True)
            finally:
                _stop_process_tree(process)
            returncode = int(process.returncode or 0)

        stdout_text = dsh_stdout.read_text(encoding="utf-8", errors="replace").strip()
        stderr_text = dsh_stderr.read_text(encoding="utf-8", errors="replace")
        error_path.write_text(
            "DeepSeek Harness native stderr:\n"
            + stderr_text
            + f"\nNative DSH run: {run_home}\n",
            encoding="utf-8",
        )
        scope_report_value: dict[str, Any] = {}
        scope_error = ""
        if scope_report.is_file():
            scope_report_value = json.loads(scope_report.read_text(encoding="utf-8"))
            if not scope_report_value.get("exactToolSurface"):
                scope_error = "DSH exposed a tool surface different from the Recuris phase"
        else:
            scope_error = "DSH did not produce its phase-scope report"

        child_metrics, child_projections = _child_session_metrics(
            session_root, tail.path, model
        )
        child_input_tokens = sum(item.input_tokens for item in child_projections)
        child_output_tokens = sum(item.output_tokens for item in child_projections)
        total_input_tokens = projection.input_tokens + child_input_tokens
        total_output_tokens = projection.output_tokens + child_output_tokens
        all_resolved_models = set(projection.resolved_models)
        for child in child_projections:
            all_resolved_models.update(child.resolved_models)
        context_state_value: dict[str, Any] = {}
        if context_state.is_file():
            context_state_value = _context_state_metrics(
                json.loads(context_state.read_text(encoding="utf-8"))
            )

        model_error = ""
        if all_resolved_models and all_resolved_models != {model}:
            model_error = (
                "DSH resolved unexpected model(s): "
                + ", ".join(sorted(all_resolved_models))
            )
        completed = isinstance(projection.turn_reason, dict) and projection.turn_reason.get("kind") == "completed"
        if returncode == 0 and not completed:
            returncode = 1
        if scope_error or model_error:
            returncode = 1

        elapsed_seconds = time.monotonic() - projection.started_monotonic
        estimated_cost = _price(total_input_tokens, "RECURIS_DSH_INPUT_USD_PER_M")
        estimated_cost += _price(total_output_tokens, "RECURIS_DSH_OUTPUT_USD_PER_M")
        metrics = {
            "type": "dsh_metrics",
            "sessionId": projection.session_id,
            "dshVersion": dsh_version,
            "runHome": str(run_home),
            "rawSession": str(tail.path) if tail.path else None,
            "startedAtUnix": started_wall,
            "elapsedSeconds": round(elapsed_seconds, 3),
            "inputTokens": total_input_tokens,
            "outputTokens": total_output_tokens,
            "rootInputTokens": projection.input_tokens,
            "rootOutputTokens": projection.output_tokens,
            "childInputTokens": child_input_tokens,
            "childOutputTokens": child_output_tokens,
            "estimatedCostUsd": round(estimated_cost, 8),
            "toolTimings": projection.tool_timings,
            "modelTimings": projection.model_timings,
            "searchableContext": {
                **projection.retrieval_metrics(),
                "enabled": bool(
                    (scope_report_value.get("searchableContext") or {}).get("enabled")
                ),
            },
            "context": {
                **projection.context_metrics(),
                "state": context_state_value,
                "workers": child_metrics,
            },
            "resolvedModels": sorted(all_resolved_models),
            "turnReason": projection.turn_reason,
            "scope": scope_report_value,
        }
        _write_jsonl(output, metrics)
        if scope_report_value:
            actual = [
                DSH_TO_CLAUDE.get(tool, tool)
                for tool in scope_report_value.get("visibleTools") or []
            ]
            _write_jsonl(
                output,
                {
                    "type": "system",
                    "subtype": "init",
                    "model": model,
                    "tools": actual,
                    "_dsh": {"scopeVerified": not bool(scope_error)},
                },
            )
        errors = [item for item in (scope_error, model_error) if item]
        if returncode != 0 and not errors:
            errors.append(stderr_text.strip() or f"DSH exited {returncode}")
        result_text = projection.final_text or stdout_text
        if errors:
            result_text = "; ".join(errors) + (f"\n{result_text}" if result_text else "")
        _write_jsonl(
            output,
            {
                "type": "result",
                "subtype": "success" if returncode == 0 else "error_during_execution",
                "is_error": returncode != 0,
                "result": result_text,
                "duration_ms": round(elapsed_seconds * 1000),
                "usage": {
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                },
                "dsh_metrics": metrics,
            },
        )
    return returncode


def main() -> int:
    try:
        return run(sys.argv[1:])
    except Exception as exc:
        # Preserve a valid result record whenever the output path is known, so
        # the deterministic driver can classify the launcher failure cleanly.
        if len(sys.argv) >= 3:
            output = Path(sys.argv[2])
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("a", encoding="utf-8", newline="\n") as handle:
                    _write_jsonl(
                        handle,
                        {
                            "type": "result",
                            "subtype": "error_during_execution",
                            "is_error": True,
                            "result": f"DSH launcher error: {type(exc).__name__}: {exc}",
                        },
                    )
            except Exception:
                pass
        print(f"DSH launcher error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
