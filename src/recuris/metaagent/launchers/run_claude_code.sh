#!/usr/bin/env bash
# Reference launcher: run ONE phase-scoped coding-agent session for the driver.
#
# This is the whole seam between the deterministic loop and the model that
# writes patches. The driver never calls a coding agent directly; it calls a
# launcher with eight positional arguments and reads Claude-Code stream-json
# events back. Supplying a different launcher is how you substitute a different
# agent -- see README.md in this directory for the contract.
#
#   run_claude_code.sh <prompt_file> <jsonl_out> "<tools>" "<allowed_rules>" \
#                      [model] [proxy_port] [reasoning] [settings_file]
#
# The driver decides the tool surface per phase; the reducer phase may get
# scoped Glob/Grep on top of Read/Edit. Evaluation, gating and snapshots stay
# on the driver's side of the seam, so Bash and PowerShell are never exposed.
#
# Requires Claude Code >= 2.1.226 for --tools / --bare / --effort /
# --no-session-persistence.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${RECURIS_WORKSPACE:-$(cd "$HERE/../../../.." && pwd)}"

PROMPT_FILE="$1"
OUT="$2"
TOOLS="$3"
ALLOWED_RULES="$4"

export RECURIS_META_MODEL="${5:-${RECURIS_META_MODEL:-}}"
export RECURIS_PROXY_PORT="${6:-${RECURIS_PROXY_PORT:-4000}}"
export RECURIS_META_REASONING="${7:-${RECURIS_META_REASONING:-high}}"
SETTINGS_FILE="${8:-}"

if [ -z "$RECURIS_META_MODEL" ]; then
  echo "RECURIS_META_MODEL is not set: name the model the meta-agent runs on" >&2
  exit 2
fi

# shellcheck source=./claude_code_env.sh
source "$HERE/claude_code_env.sh"

CLAUDE="${RECURIS_CLAUDE_BIN:-$(command -v claude || true)}"
if [ -z "$CLAUDE" ]; then
  echo "the 'claude' CLI is not on PATH; set RECURIS_CLAUDE_BIN" >&2
  exit 2
fi

EXTRA_ARGS=(--bare --permission-mode dontAsk --no-session-persistence
            --effort "$RECURIS_META_REASONING")

# The session must be able to read the benchmark's policy document, and
# nothing else under the benchmark: the driver's deny rules do the scoping.
if [ -n "${TAU2_ROOT:-}" ]; then
  EXTRA_ARGS+=(--add-dir "$TAU2_ROOT")
elif [ -d "external/tau2-bench" ]; then
  EXTRA_ARGS+=(--add-dir "external/tau2-bench")
fi

if [ -n "$SETTINGS_FILE" ]; then
  EXTRA_ARGS+=(--settings "$SETTINGS_FILE")
fi

set +e
"$CLAUDE" -p \
  --model "$RECURIS_META_MODEL" \
  --tools "$TOOLS" \
  --allowedTools "$ALLOWED_RULES" \
  "${EXTRA_ARGS[@]}" \
  --output-format stream-json --verbose \
  < "$PROMPT_FILE" \
  > "$OUT" 2> "${OUT%.jsonl}.err"
rc=$?
set -e

echo "EXIT $rc"
exit "$rc"
