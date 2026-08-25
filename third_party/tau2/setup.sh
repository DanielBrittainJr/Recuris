#!/usr/bin/env bash
# Fetch and prepare tau2-Bench.
#
# What this produces is a hybrid, and it has to be: the harness we evaluated on
# is the v1.0.0 lineage at 8ebb749, and the domain data is the v1.0.1 release.
# Neither a plain v1.0.0 nor a plain v1.0.1 checkout reproduces it. The reason
# is in README.md; the short version is that v1.0.1 corrected 75+ tasks, so
# scores across the two versions are not comparable, and our results are on the
# corrected tasks.
#
# The checkout is a real git clone, not an unpacked archive: tau2 records the
# benchmark revision by running `git rev-parse HEAD`, and a run without it
# cannot bind its provenance.
#
#   bash third_party/tau2/setup.sh [target-dir]
#
# Default target: external/tau2-bench (git-ignored).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
TARGET="${1:-${RECURIS_EXTERNAL_ROOT:-$REPO_ROOT/external}/tau2-bench}"

UPSTREAM="https://github.com/sierra-research/tau2-bench.git"
HARNESS_COMMIT="$(tr -d '[:space:]' < "$HERE/upstream.commit")"
DATA_TAG="v1.0.1"

echo "[recuris] target:  $TARGET"
echo "[recuris] harness: $HARNESS_COMMIT"
echo "[recuris] data:    $DATA_TAG"

if [ -e "$TARGET" ]; then
  echo "[recuris] $TARGET already exists; remove it to re-run this script." >&2
  exit 1
fi

mkdir -p "$(dirname "$TARGET")"

# core.autocrlf=false so the tree is LF on every platform. The committed
# checksums are LF-normalised anyway, but a CRLF working tree also changes what
# the benchmark's own file reads produce on Windows.
git -c core.autocrlf=false clone "$UPSTREAM" "$TARGET"
cd "$TARGET"
git -c advice.detachedHead=false checkout "$HARNESS_COMMIT"

echo "[recuris] applying recuris.patch"
git apply --check "$HERE/recuris.patch"
git apply "$HERE/recuris.patch"

echo "[recuris] overlaying $DATA_TAG domain data"
git checkout "$DATA_TAG" -- data/tau2

echo "[recuris] verifying payload"
python "$REPO_ROOT/scripts/verify_checksums.py" "$HERE/CHECKSUMS.json" "$TARGET"

cat <<EOF

[recuris] tau2-Bench is ready at $TARGET

Next:
  uv pip install -e "$TARGET"     # so 'import tau2' works
  recuris check-data --benchmark tau2
EOF
