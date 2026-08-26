#!/usr/bin/env bash
# DeepSeek Harness launcher for one Recuris Meta-Agent phase.
#
# The deterministic Recuris driver owns the prompt, tool surface, path scope,
# model treatment, timeout and admission decision.  dsh_launcher.py adapts that
# contract to a fresh DSH headless session and writes a Claude stream-json
# compatible trace back to the driver.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${RECURIS_PYTHON:-python}"

set +e
"$PYTHON" "$HERE/dsh_launcher.py" "$@"
rc=$?
set -e

echo "EXIT $rc"
exit "$rc"
