#!/usr/bin/env bash
# Apply the Recuris patch to a harbor 0.20.0 source checkout.
#
# This is conditional, not routine. On a normal Docker host, stock harbor
# 0.20.0 runs both SkillFlow and Terminal-Bench 2.1 without it. The patch
# exists for hosts where `docker compose exec` behaves differently -- see
# README.md for the two symptoms it addresses.
#
#   bash third_party/harbor/apply.sh <path-to-harbor-checkout>
#
# If no path is given, the script asks Python where harbor is installed, which
# works for an editable install and not for a wheel.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$HERE/harbor-0.20.0-docker.patch"

if [ $# -ge 1 ]; then
  TARGET="$1"
else
  TARGET="$(python -c 'import harbor, pathlib; print(pathlib.Path(harbor.__file__).parents[2])' 2>/dev/null || true)"
  if [ -z "$TARGET" ]; then
    echo "harbor is not importable; pass the checkout path explicitly." >&2
    exit 1
  fi
fi

if [ ! -f "$TARGET/src/harbor/environments/docker/docker.py" ]; then
  echo "not a harbor source checkout: $TARGET" >&2
  echo "A wheel install cannot be patched; clone harbor and install it editable." >&2
  exit 1
fi

VERSION="$(python -c 'import importlib.metadata as m; print(m.version("harbor"))' 2>/dev/null || echo unknown)"
if [ "$VERSION" != "0.20.0" ]; then
  echo "warning: this patch was written against harbor 0.20.0, found $VERSION" >&2
fi

cd "$TARGET"
if git apply --check --reverse "$PATCH" 2>/dev/null; then
  echo "[recuris] already applied; nothing to do."
  exit 0
fi
git apply --check "$PATCH"
git apply "$PATCH"
echo "[recuris] patched $TARGET"
