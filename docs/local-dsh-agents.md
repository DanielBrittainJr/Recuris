# Running local DSH agents

The harness core is a terminal workflow today. Recuris launches DeepSeek
Harness headlessly so the same process can enforce scopes, timeboxes, model
routing, and telemetry without depending on a UI. A website can call this same
launcher later, but it should remain a front end rather than a second execution
path.

## One-time setup

Install Recuris and DeepSeek Harness, start an OpenAI-compatible local model
server, and choose a model that supports tool calls. For an Ollama server the
endpoint is normally `http://127.0.0.1:11434/v1`.

PowerShell:

```powershell
uv sync
npm install --global @deepseek-ai/dsh@0.1.1-rc.2

$env:OPENAI_BASE_URL = 'http://127.0.0.1:11434/v1'
$env:OPENAI_API_KEY = 'local-only'
$env:RECURIS_SESSION_LAUNCHER = (
  Resolve-Path 'src/recuris/metaagent/launchers/run_dsh.sh'
).Path
```

Bash:

```bash
uv sync
npm install --global @deepseek-ai/dsh@0.1.1-rc.2

export OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export OPENAI_API_KEY=local-only
export RECURIS_SESSION_LAUNCHER="$PWD/src/recuris/metaagent/launchers/run_dsh.sh"
```

Use a dedicated loopback proxy port and name the exact model served by the
endpoint:

```bash
recuris metaagent qualify \
  --run-id local-harness-smoke \
  --meta-model YOUR_LOCAL_MODEL \
  --meta-reasoning off \
  --proxy-port 4047
```

Qualification intentionally verifies the old exact `Read`/`Edit` contract.
In a normal `recuris metaagent run`, the driver automatically adds `Context`
to every ordinary DSH phase that can read. The model sees its schema and usage
guidance in the system prompt; no task-specific prompt template is required.

## What agents receive

Every agent launched through `RECURIS_SESSION_LAUNCHER=.../run_dsh.sh` receives
the same defaults:

* complete phase history remains searchable after active-context pruning;
* bounded named working memory persists for the phase;
* the model may start a foreground, read-only, one-level context worker;
* every parent and child model step, tool call, token, duration, result size,
  resolved model, and local/API cost estimate is recorded;
* filesystem access remains the intersection of the outer workspace sandbox,
  the phase's path rules, and the child's read-only tool filter.
* a tool-surface setup failure aborts before the first model call, and three
  identical denied requests cancel the active turn instead of allowing a retry
  loop.

This does not alter agents running directly in another app. Codex, ChatGPT, or
an independently launched DSH session uses these capabilities only when that
agent is routed through this launcher contract. To make another orchestrator
use them, configure its worker command to invoke the same launcher rather than
copying the prompt alone.

The terminal result trace contains a `dsh_metrics` record. Its `context` field
shows context operations and child sessions; `toolTimings` and `modelTimings`
show where time went; `root*Tokens`, `child*Tokens`, and the total token fields
separate orchestration overhead from delegated work. `runHome` points to the
durable native logs for deeper review.

## Useful controls

```text
RECURIS_DSH_CONTEXT=0                         disable Context entirely
RECURIS_DSH_SEARCHABLE_CONTEXT=0              disable bounded searchable Read
RECURIS_DSH_CONTEXT_CHILD_TIMEOUT_MS=20000    per-worker wall-time budget
RECURIS_DSH_CONTEXT_CHILD_MAX_TOKENS=1536     per-worker output budget
RECURIS_DSH_CONTEXT_MAX_DELEGATIONS=3         worker-call budget per phase
RECURIS_DSH_INPUT_USD_PER_M=0                 optional input price estimate
RECURIS_DSH_OUTPUT_USD_PER_M=0                optional output price estimate
```

Use `Context` as a second pair of eyes, not as an automatic vote. The parent
still owns the objective and final answer; the child receives a narrow evidence
question and returns evidence without mutation authority.
