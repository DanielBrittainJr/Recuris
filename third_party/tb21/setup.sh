#!/usr/bin/env bash
# Fetch Terminal-Bench 2.1.
#
# No patch: we run the benchmark unmodified. The Recuris agent is registered
# with harbor by import path, so the task set stays exactly as published.
#
#   bash third_party/tb21/setup.sh [target-dir]
#
# Default target: external/terminal-bench-2.1 (git-ignored).
#
# Note that the tasks are only half of it. Each task builds a container image,
# and results depend on which image snapshot you have -- see README.md, and the
# precondition recorded in splits/tb21/split_manifest.json.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
TARGET="${1:-${RECURIS_EXTERNAL_ROOT:-$REPO_ROOT/external}/terminal-bench-2.1}"

UPSTREAM="https://github.com/harbor-framework/terminal-bench-2-1.git"
COMMIT="$(tr -d '[:space:]' < "$HERE/upstream.commit")"

echo "[recuris] target: $TARGET"
echo "[recuris] commit: $COMMIT"

if [ -e "$TARGET" ]; then
  echo "[recuris] $TARGET already exists; remove it to re-run this script." >&2
  exit 1
fi

mkdir -p "$(dirname "$TARGET")"
git -c core.autocrlf=false clone "$UPSTREAM" "$TARGET"
cd "$TARGET"
git -c advice.detachedHead=false checkout "$COMMIT"

echo "[recuris] $(find tasks -maxdepth 1 -mindepth 1 -type d | wc -l) tasks available"

cat <<EOF

[recuris] Terminal-Bench 2.1 is ready at $TARGET

Next:
  bash third_party/harbor/apply.sh     # only if your Docker setup needs it
  recuris check-data --benchmark tb21
  recuris tta run --taskset splits/tb21/tta_taskset_v3.json \\
      --run-id smoke --arm m0 --limit 1 --rounds 1
EOF
