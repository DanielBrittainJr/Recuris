#!/usr/bin/env bash
# Fetch and prepare SkillFlow.
#
# SkillFlow carries no licence file, which means all rights are reserved and we
# may not redistribute any part of it. Nothing of theirs is committed in this
# repository: this script clones their repository and applies our patch to your
# copy. The task set is a separate Hugging Face dataset, also fetched, not
# redistributed.
#
#   bash third_party/skillflow/setup.sh [target-dir]
#
# Default target: external/SkillFlow (git-ignored).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
TARGET="${1:-${RECURIS_EXTERNAL_ROOT:-$REPO_ROOT/external}/SkillFlow}"

UPSTREAM="https://github.com/ZhangZi-a/SkillFlow.git"
COMMIT="$(tr -d '[:space:]' < "$HERE/upstream.commit")"
DATASET="zhang-ziao/SkillFlow-Task"

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

echo "[recuris] applying recuris.patch"
git apply --check "$HERE/recuris.patch"
git apply "$HERE/recuris.patch"

echo "[recuris] fetching the task set from Hugging Face"
if command -v hf >/dev/null 2>&1; then
  hf download "$DATASET" --repo-type dataset --local-dir "$TARGET/test_tasks"
else
  echo "[recuris] 'hf' is not on PATH. Install huggingface_hub, then run:" >&2
  echo "  hf download $DATASET --repo-type dataset --local-dir $TARGET/test_tasks" >&2
  exit 1
fi

cat <<EOF

[recuris] SkillFlow is ready at $TARGET

Next:
  ./$TARGET/docker/harbor-cli-base/build.sh
  python $TARGET/utils/prebuild_task_images.py --tasks-root $TARGET/test_tasks
  recuris check-data --benchmark skillflow

Stock harbor works on a normal Docker host. Apply
third_party/harbor/apply.sh only if you hit one of the two symptoms in
third_party/harbor/README.md.
EOF
