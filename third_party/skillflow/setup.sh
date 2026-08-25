#!/usr/bin/env bash
# Fetch SkillFlow and apply the Recuris patch.
#
#   bash third_party/skillflow/setup.sh [target-dir]   # default: external/SkillFlow
#
# SkillFlow carries no licence, so nothing of theirs is committed here: this
# clones their repository and patches your copy. The task set is a separate
# Hugging Face dataset, also fetched rather than redistributed.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$(cd "$HERE/../.." && pwd)/external/SkillFlow}"
COMMIT="$(tr -d '[:space:]' < "$HERE/upstream.commit")"
DATASET="zhang-ziao/SkillFlow-Task"

if [ -e "$TARGET" ]; then
  echo "$TARGET already exists; remove it to re-run." >&2
  exit 1
fi

git -c core.autocrlf=false clone https://github.com/ZhangZi-a/SkillFlow.git "$TARGET"
cd "$TARGET"
git -c advice.detachedHead=false checkout "$COMMIT"
git apply "$HERE/recuris.patch"

if command -v hf >/dev/null 2>&1; then
  hf download "$DATASET" --repo-type dataset --local-dir "$TARGET/test_tasks"
else
  echo "'hf' is not on PATH. Install huggingface_hub, then run:" >&2
  echo "  hf download $DATASET --repo-type dataset --local-dir $TARGET/test_tasks" >&2
  exit 1
fi

cat <<EOF

SkillFlow is ready at $TARGET

Next:
  ./$TARGET/docker/harbor-cli-base/build.sh
  python $TARGET/utils/prebuild_task_images.py --tasks-root $TARGET/test_tasks
  recuris check-data --benchmark skillflow
EOF
