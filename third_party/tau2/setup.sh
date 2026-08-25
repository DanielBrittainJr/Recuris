#!/usr/bin/env bash
# Fetch tau2-Bench and apply the Recuris patch.
#
#   bash third_party/tau2/setup.sh [target-dir]     # default: external/tau2-bench
#
# The harness is pinned to the commit in upstream.commit and the domain data
# comes from tag v1.0.1, which corrected 75+ tasks in retail and airline. Our
# numbers are on the corrected tasks, so both pins matter.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$(cd "$HERE/../.." && pwd)/external/tau2-bench}"
COMMIT="$(tr -d '[:space:]' < "$HERE/upstream.commit")"

if [ -e "$TARGET" ]; then
  echo "$TARGET already exists; remove it to re-run." >&2
  exit 1
fi

# core.autocrlf=false keeps the tree LF on Windows too.
git -c core.autocrlf=false clone https://github.com/sierra-research/tau2-bench.git "$TARGET"
cd "$TARGET"
git -c advice.detachedHead=false checkout "$COMMIT"
git apply "$HERE/recuris.patch"
git checkout v1.0.1 -- data/tau2

cat <<EOF

tau2-Bench is ready at $TARGET

Next:
  uv pip install -e "$TARGET"
  recuris check-data --benchmark tau2
EOF
