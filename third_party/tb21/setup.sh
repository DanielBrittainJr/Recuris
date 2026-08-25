#!/usr/bin/env bash
# Fetch Terminal-Bench 2.1.
#
#   bash third_party/tb21/setup.sh [target-dir]   # default: external/terminal-bench-2.1
#
# No patch: the benchmark runs unmodified. The Recuris agent is registered with
# harbor by import path, so the task set stays exactly as published.
#
# Each task builds a container image, and results depend on which image snapshot
# you have. Build them once and keep them.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$(cd "$HERE/../.." && pwd)/external/terminal-bench-2.1}"
COMMIT="$(tr -d '[:space:]' < "$HERE/upstream.commit")"

if [ -e "$TARGET" ]; then
  echo "$TARGET already exists; remove it to re-run." >&2
  exit 1
fi

git -c core.autocrlf=false clone https://github.com/harbor-framework/terminal-bench-2-1.git "$TARGET"
cd "$TARGET"
git -c advice.detachedHead=false checkout "$COMMIT"

cat <<EOF

Terminal-Bench 2.1 is ready at $TARGET
$(find tasks -maxdepth 1 -mindepth 1 -type d | wc -l) tasks available

Next:
  recuris check-data --benchmark tb21
EOF
