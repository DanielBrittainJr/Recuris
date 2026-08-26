# The launcher contract

The evolution loop is deterministic code. Every *generative* step in it —
diagnosing a failure, writing a patch, repairing a rejected candidate,
reviewing a round — is a call out to an agentic coding CLI, and every one of
those calls goes through a single eight-argument launcher script.

This is where the boundary is, so it is worth being precise about what sits on
each side.

**On the loop's side:** which tasks to evaluate, how many trials, what evidence
the session may see, what tools it may use, which paths it may read or write,
whether a candidate is admitted, and what gets recorded. None of that is
delegated, and none of it can be argued with.

**On the agent's side:** reading the evidence and proposing an edit.

The agent is proprietary third-party software and is not shipped here. The
contract is, along with a reference implementation for Claude Code.

## The contract

The driver invokes:

```
<launcher> <prompt_file> <jsonl_out> <tools> <allowed_rules> \
           <model> <proxy_port> <reasoning> <settings_path>
```

| # | Argument | Meaning |
|---|---|---|
| 1 | `prompt_file` | the phase prompt; read it on stdin or from the path |
| 2 | `jsonl_out` | where to write the event stream; stderr goes to the same path with `.err` |
| 3 | `tools` | comma-separated tool surface for this phase |
| 4 | `allowed_rules` | permission rules, e.g. `Edit(runs/x/packages/cand/**)` |
| 5 | `model` | the model id the meta-agent runs on |
| 6 | `proxy_port` | loopback port of the translating proxy the driver started |
| 7 | `reasoning` | effort level |
| 8 | `settings_path` | optional settings file; may be empty |

It must exit with the agent's exit code, and print `EXIT <rc>` on stdout.

`jsonl_out` must contain Claude-Code `stream-json` events, one JSON object per
line. The driver reads the assistant text, the tool calls, and the terminal
result. Any harness that emits that format can be substituted by pointing
`RECURIS_SESSION_LAUNCHER` at it.

## What ships here

| File | What it is |
|---|---|
| `run_claude_code.sh` | reference launcher. Needs Claude Code ≥ 2.1.226 for `--tools`, `--bare`, `--effort` and `--no-session-persistence`. |
| `claude_code_env.sh` | the environment discipline: bind the CLI to the driver's proxy, disable telemetry and auto-update, and raise the shell-tool timeout so a multi-minute evaluation is not backgrounded out from under the session. |
| `../proxy/anthropic_openai_proxy.py` | translates the Anthropic-shaped requests the CLI makes into OpenAI-compatible ones, so the meta-agent can run on any provider. |
| `run_dsh.sh` | DeepSeek Harness launcher. Runs one fresh headless session, enforces the phase's exact tools and paths inside DSH, and projects DSH's native log into the stream-json audit contract. |
| `dsh_launcher.py` | DSH process/configuration adapter plus lossless timing, token, tool-call and result telemetry. Native DSH logs are retained outside the repository and their path is recorded in each trace. |
| `dsh_scope_guard.mjs` | DSH-side capability guard. The workspace sandbox remains the outer boundary; this adds Recuris's narrower per-phase allowlist. |

## Substituting your own

Write a script that satisfies the table above and set:

```bash
export RECURIS_SESSION_LAUNCHER=/path/to/your_launcher.sh
```

For DeepSeek Harness:

```bash
export RECURIS_SESSION_LAUNCHER="$PWD/src/recuris/metaagent/launchers/run_dsh.sh"
# Optional when `python` is not the Python 3.12 running Recuris:
export RECURIS_PYTHON=/path/to/python3.12
```

The Recuris paper reports DeepSeek Harness for Runs B and C, but the public
artifact does not include that launcher. This adapter is an independent
implementation of the repository's published eight-argument launcher contract;
it is not represented as the authors' unreleased DSH configuration.

`dsh` must be on `PATH`. The launcher creates a new isolated DSH home for each
phase under `$RECURIS_DSH_RUN_ROOT` (or the platform-local application-data
directory), so no chat context crosses diagnosis, patch, repair, or review
phases. It uses the driver's loopback Anthropic proxy, which preserves the
same forced model/reasoning treatment and upstream-model counters as the
reference launcher.

The normal stream-json trace gains a `dsh_metrics` record containing exact
wall time, per-model-step latency, per-tool latency, input/output tokens,
resolved model, scope proof, and the native DSH log path. Set
`RECURIS_DSH_INPUT_USD_PER_M` and `RECURIS_DSH_OUTPUT_USD_PER_M` only when a
non-zero API cost estimate is useful; local runs correctly default to `$0`.

Two things are easy to get wrong and expensive to discover late.

**Honour argument 3.** The driver narrows the tool surface per phase on
purpose: a diagnosis session that can write files can edit the package it is
supposed to be diagnosing, and the round's result stops meaning anything.

**Do not let the session persist across phases.** Each phase is meant to start
from the evidence the driver assembled for it. A session that carries context
from the previous phase has seen material the driver deliberately withheld, and
the information contract the paper describes no longer holds.
