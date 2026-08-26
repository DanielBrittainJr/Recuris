"""Anthropic Messages API -> OpenAI-compatible translation proxy.

The coding agent that drives the evolution loop speaks the Anthropic
Messages API. This lets it run on any OpenAI-compatible endpoint, which is
what makes it possible to ask whether a mid-capability model, driven by the
loop, can evolve a Skill Memory package the way a stronger one does.

Exposes POST /v1/messages (streaming Anthropic SSE) and
POST /v1/messages/count_tokens, forwarding to /chat/completions.

Two things it does on every request, both load-bearing for the experiment:
it forces the configured model, so the agent's auxiliary model aliases
cannot silently swap in a cheaper one mid-campaign and change the treatment
without anything appearing in the logs; and it injects the configured
reasoning effort, for the same reason.

    python -m recuris.metaagent.proxy.anthropic_openai_proxy

Reads OPENAI_API_KEY / OPENAI_BASE_URL from the environment, falling back to
a .env file in the workspace. The key is never logged.
"""
import asyncio
import json
import os
import sys
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ---------------------------------------------------------------- config
from recuris.paths import workspace_root

ROOT = workspace_root()
LOG = ROOT / "logs" / "proxy.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

def _load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

_ENV = _load_env()
API_KEY = os.environ.get("OPENAI_API_KEY", "") or _ENV.get("OPENAI_API_KEY", "")
# No vendor default: an endpoint that was not chosen deliberately is worse
# than a startup error naming the variable to set.
API_BASE = (os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
            or _ENV.get("OPENAI_BASE_URL") or _ENV.get("OPENAI_API_BASE")
            or "").rstrip("/")
API_URL = API_BASE + "/chat/completions"
MODEL = (os.environ.get("RECURIS_PROXY_MODEL")
         or os.environ.get("RECURIS_META_MODEL", ""))
REASONING = os.environ.get("PROXY_REASONING_EFFORT", "high")
# Claude-style launchers spell disabled reasoning ``off`` while OpenAI-shaped
# endpoints use ``none``.  Keep REASONING unchanged for treatment/audit proof,
# and translate only the value sent upstream.
UPSTREAM_REASONING = "none" if REASONING == "off" else REASONING
TEMPERATURE = float(os.environ.get("PROXY_TEMPERATURE", "0.0"))
PORT = int(os.environ.get("PROXY_PORT", "4000"))
STREAM_HEARTBEAT_SECONDS = float(
    os.environ.get("PROXY_STREAM_HEARTBEAT_SECONDS", "10.0")
)
if STREAM_HEARTBEAT_SECONDS <= 0:
    raise ValueError("PROXY_STREAM_HEARTBEAT_SECONDS must be positive")
SEEN_UPSTREAM_MODELS: set[str] = set()
UPSTREAM_MODEL_COUNTS: dict[str, int] = {}

def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, file=sys.stderr, flush=True)


def record_upstream_model(model):
    """Record the Ark-resolved model without exposing credentials or content."""
    if not model:
        return
    resolved = str(model)
    first_seen = resolved not in SEEN_UPSTREAM_MODELS
    SEEN_UPSTREAM_MODELS.add(resolved)
    UPSTREAM_MODEL_COUNTS[resolved] = (
        UPSTREAM_MODEL_COUNTS.get(resolved, 0) + 1
    )
    if first_seen:
        log(f"upstream model resolved={resolved}")

# ---------------------------------------------------------------- helpers
def _text_of(content):
    """Anthropic content (str | list-of-blocks) -> plain text (for tool_result / system)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(b.get("text", ""))
                elif b.get("type") == "tool_result":
                    out.append(_text_of(b.get("content", "")))
                elif b.get("type") == "image":
                    out.append("[image omitted]")
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(x for x in out if x)
    return ""

def _system_text(system):
    if not system:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(b.get("text", "") for b in system
                            if isinstance(b, dict) and b.get("type") == "text")
    return None

def anthropic_to_openai(req: dict) -> dict:
    """Translate an Anthropic /v1/messages request body to OpenAI chat.completions."""
    msgs = []
    sys_txt = _system_text(req.get("system"))
    if sys_txt:
        msgs.append({"role": "system", "content": sys_txt})

    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            if isinstance(content, str):
                msgs.append({"role": "user", "content": content})
                continue
            # list: split tool_result blocks into role:tool messages, text into a user msg
            texts = []
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "tool_result":
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": _text_of(b.get("content", "")) or "(no output)",
                    })
                elif t == "text":
                    texts.append(b.get("text", ""))
                elif t == "image":
                    texts.append("[image omitted]")
            if texts:
                msgs.append({"role": "user", "content": "\n".join(texts)})
        elif role == "assistant":
            text_parts, tool_calls = [], []
            if isinstance(content, str):
                text_parts.append(content)
            else:
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    t = b.get("type")
                    if t == "text":
                        text_parts.append(b.get("text", ""))
                    elif t == "tool_use":
                        tool_calls.append({
                            "id": b.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                            },
                        })
                    # thinking blocks are dropped
            am = {"role": "assistant"}
            am["content"] = "\n".join(t for t in text_parts if t) or None
            if tool_calls:
                am["tool_calls"] = tool_calls
            msgs.append(am)

    payload = {
        "model": MODEL,
        "messages": msgs,
        "stream": bool(req.get("stream", False)),
    }
    if req.get("max_tokens"):
        payload["max_tokens"] = req["max_tokens"]
    # Freeze sampling too; Claude Code must not silently choose a different
    # stochastic treatment between sessions.
    payload["temperature"] = TEMPERATURE
    if "top_p" in req and req["top_p"] is not None:
        payload["top_p"] = req["top_p"]
    if req.get("stop_sequences"):
        payload["stop"] = req["stop_sequences"]

    # tools
    tools = []
    for t in req.get("tools", []) or []:
        if not isinstance(t, dict):
            continue
        schema = t.get("input_schema")
        if schema is None:
            continue  # skip server-side / non-function tools
        tools.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", "") or "",
                "parameters": schema,
            },
        })
    if tools:
        payload["tools"] = tools
        # The Anthropic SSE adapter tracks one open tool-use block at a time.
        # Force serial tool selection upstream so Ark cannot emit interleaved
        # parallel calls that the adapter cannot represent safely.
        payload["parallel_tool_calls"] = False
        tc = req.get("tool_choice")
        if isinstance(tc, dict):
            tt = tc.get("type")
            if tt == "auto":
                payload["tool_choice"] = "auto"
            elif tt == "any":
                payload["tool_choice"] = "required"
            elif tt == "tool" and tc.get("name"):
                payload["tool_choice"] = {"type": "function",
                                          "function": {"name": tc["name"]}}
            elif tt == "none":
                payload["tool_choice"] = "none"

    # This proxy is Meta-Agent-only: every Claude Code request uses the same
    # frozen reasoning treatment, including requests carrying a small-model alias.
    payload["reasoning_effort"] = UPSTREAM_REASONING
    if payload["stream"]:
        payload["stream_options"] = {"include_usage": True}
    return payload

def _est_tokens(req: dict) -> int:
    n = len(_system_text(req.get("system")) or "")
    for m in req.get("messages", []):
        n += len(_text_of(m.get("content", "")))
    for t in req.get("tools", []) or []:
        n += len(json.dumps(t.get("input_schema", {})))
    return max(1, n // 4)

# ---------------------------------------------------------------- SSE build
def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _monotonic():
    """Injectable monotonic clock for downstream-idle accounting."""
    return time.monotonic()


FINISH_MAP = {"stop": "end_turn", "length": "max_tokens",
              "tool_calls": "tool_use", "content_filter": "end_turn"}

async def stream_anthropic(req: dict):
    """Consume Ark's OpenAI SSE and keep the Anthropic client stream alive.

    Ark may continuously emit ``reasoning_content`` chunks for several minutes.
    Those chunks are intentionally hidden from Claude Code, so upstream queue
    activity is not evidence that the downstream client has received anything.
    Heartbeats are therefore scheduled from the last Anthropic SSE event emitted
    to the client, not from the last line received from Ark.
    """
    payload = anthropic_to_openai(req)
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    msg_id = "msg_" + str(int(time.time() * 1000))
    in_tokens = _est_tokens(req)
    last_downstream_event = _monotonic()

    def downstream_event(event, data):
        nonlocal last_downstream_event
        last_downstream_event = _monotonic()
        return _sse(event, data)

    def content_watchdog_heartbeat():
        """Emit a real SDK stream event without inserting response text.

        Claude Code deliberately ignores Anthropic ``ping`` events when
        measuring its 300-second content-event watchdog.  A null
        ``message_delta`` is yielded by the Anthropic SDK, updates no content
        block, and therefore keeps the harness alive without exposing Ark's
        hidden reasoning or fabricating visible model text.
        """
        return downstream_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": None, "stop_sequence": None},
                "usage": {"output_tokens": out_tokens},
            },
        )

    # message_start
    yield downstream_event("message_start", {
        "type": "message_start",
        "message": {"id": msg_id, "type": "message", "role": "assistant",
                    "model": MODEL, "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": in_tokens, "output_tokens": 0}},
    })
    yield downstream_event("ping", {"type": "ping"})

    # Producer pushes upstream lines to a queue. The consumer tracks downstream
    # silence independently because untranslated reasoning lines keep this queue
    # active without keeping Claude Code's stream alive.
    q: asyncio.Queue = asyncio.Queue()

    async def producer():
        try:
            timeout = httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=20.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", API_URL, json=payload,
                                         headers=headers) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "ignore")
                        await q.put(("error", f"upstream {resp.status_code}: {body[:800]}"))
                        return
                    async for line in resp.aiter_lines():
                        await q.put(("line", line))
        except Exception as e:
            await q.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            await q.put(("end", None))

    prod = asyncio.create_task(producer())

    # translator state
    text_idx = None          # anthropic index of the (single) text block, if opened
    open_idx = None          # currently-open anthropic content block index
    next_idx = 0
    tool_map = {}            # openai tool_call index -> anthropic block index
    stop_reason = "end_turn"
    out_tokens = 0

    def close_open():
        nonlocal open_idx
        if open_idx is not None:
            s = downstream_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": open_idx},
            )
            open_idx = None
            return s
        return None

    try:
        while True:
            remaining = max(
                0.0,
                STREAM_HEARTBEAT_SECONDS
                - (_monotonic() - last_downstream_event),
            )
            try:
                kind, val = await asyncio.wait_for(q.get(), timeout=remaining)
            except asyncio.TimeoutError:
                yield downstream_event("ping", {"type": "ping"})
                yield content_watchdog_heartbeat()
                continue

            # The queue can remain continuously active with hidden
            # ``reasoning_content``. Emit a client-visible heartbeat based on
            # downstream silence even when q.get() never times out.
            if (_monotonic() - last_downstream_event
                    >= STREAM_HEARTBEAT_SECONDS):
                yield downstream_event("ping", {"type": "ping"})
                yield content_watchdog_heartbeat()

            if kind == "end":
                break
            if kind == "error":
                log("STREAM ERROR " + str(val))
                s = close_open()
                if s:
                    yield s
                yield downstream_event(
                    "error",
                    {"type": "error",
                     "error": {"type": "api_error", "message": str(val)}},
                )
                yield downstream_event(
                    "message_stop", {"type": "message_stop"}
                )
                return
            # kind == "line"
            line = val
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            record_upstream_model(chunk.get("model"))

            if chunk.get("usage"):
                out_tokens = chunk["usage"].get("completion_tokens", out_tokens) or out_tokens
            choices = chunk.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            delta = ch.get("delta") or {}
            fr = ch.get("finish_reason")

            # text content (reasoning_content is intentionally dropped)
            txt = delta.get("content")
            if txt:
                if text_idx is None:
                    s = close_open()
                    if s:
                        yield s
                    text_idx = next_idx
                    next_idx += 1
                    open_idx = text_idx
                    yield downstream_event("content_block_start", {
                        "type": "content_block_start", "index": text_idx,
                        "content_block": {"type": "text", "text": ""}})
                elif open_idx != text_idx:
                    s = close_open()
                    if s:
                        yield s
                    open_idx = text_idx  # reopen not allowed; but text is contiguous in practice
                yield downstream_event("content_block_delta", {
                    "type": "content_block_delta", "index": text_idx,
                    "delta": {"type": "text_delta", "text": txt}})

            # tool calls
            for tc in delta.get("tool_calls") or []:
                oi = tc.get("index", 0)
                fn = tc.get("function") or {}
                if oi not in tool_map:
                    s = close_open()
                    if s:
                        yield s
                    ai = next_idx
                    next_idx += 1
                    tool_map[oi] = ai
                    open_idx = ai
                    yield downstream_event("content_block_start", {
                        "type": "content_block_start", "index": ai,
                        "content_block": {"type": "tool_use",
                                          "id": tc.get("id") or f"toolu_{msg_id}_{oi}",
                                          "name": fn.get("name", ""), "input": {}}})
                args = fn.get("arguments")
                if args:
                    yield downstream_event("content_block_delta", {
                        "type": "content_block_delta", "index": tool_map[oi],
                        "delta": {"type": "input_json_delta", "partial_json": args}})

            if fr:
                stop_reason = FINISH_MAP.get(fr, "end_turn")

        # finalize
        s = close_open()
        if s:
            yield s
        yield downstream_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": out_tokens or 1}})
        yield downstream_event("message_stop", {"type": "message_stop"})
    finally:
        if not prod.done():
            prod.cancel()

# ---------------------------------------------------------------- app
app = FastAPI()

@app.post("/v1/messages")
async def messages(request: Request):
    req = await request.json()
    n_tools = len(req.get("tools") or [])
    n_msgs = len(req.get("messages") or [])
    model_req = req.get("model", "?")
    log(f"/v1/messages model={model_req} msgs={n_msgs} tools={n_tools} "
        f"stream={req.get('stream')}")
    if req.get("stream"):
        return StreamingResponse(stream_anthropic(req), media_type="text/event-stream")

    # non-streaming: aggregate
    payload = anthropic_to_openai(req)
    payload["stream"] = False
    payload.pop("stream_options", None)
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        r = await client.post(API_URL, json=payload, headers=headers)
    if r.status_code != 200:
        log(f"non-stream upstream {r.status_code}: {r.text[:400]}")
        return JSONResponse(status_code=r.status_code,
                            content={"type": "error",
                                     "error": {"type": "api_error", "message": r.text[:800]}})
    oai = r.json()
    record_upstream_model(oai.get("model"))
    ch = (oai.get("choices") or [{}])[0]
    m = ch.get("message") or {}
    blocks = []
    if m.get("content"):
        blocks.append({"type": "text", "text": m["content"]})
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:
            inp = {}
        blocks.append({"type": "tool_use", "id": tc.get("id", ""),
                       "name": fn.get("name", ""), "input": inp})
    usage = oai.get("usage") or {}
    return JSONResponse({
        "id": oai.get("id", "msg_x"), "type": "message", "role": "assistant",
        "model": oai.get("model") or MODEL,
        "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": FINISH_MAP.get(ch.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", _est_tokens(req)),
                  "output_tokens": usage.get("completion_tokens", 1)},
    })

@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    req = await request.json()
    return JSONResponse({"input_tokens": _est_tokens(req)})

@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL, "endpoint": API_BASE, "reasoning": REASONING,
            "temperature": TEMPERATURE,
            "seen_upstream_models": sorted(SEEN_UPSTREAM_MODELS),
            "upstream_model_counts": {
                model: UPSTREAM_MODEL_COUNTS[model]
                for model in sorted(UPSTREAM_MODEL_COUNTS)
            }}

if __name__ == "__main__":
    missing = [
        name for name, value in (
            ("OPENAI_API_KEY", API_KEY),
            ("OPENAI_BASE_URL", API_BASE),
            ("RECURIS_META_MODEL", MODEL),
        ) if not value
    ]
    if missing:
        log(f"FATAL: not configured; set {', '.join(missing)}")
        sys.exit(1)
    log(f"proxy up on :{PORT} -> {API_BASE} model={MODEL} reasoning={REASONING} "
        f"temperature={TEMPERATURE}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
