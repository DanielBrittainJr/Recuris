#!/usr/bin/env python3
"""Re-anchor the protected Skill Memory packages.

Run this after a *deliberate* change to a package listed in
``integrity/anchors.json``, and commit both files it rewrites:

    skill_memories/champions.lock.json   per-file digests
    integrity/anchors.json               the aggregate the lock must match

The two must move together. The lock is writable, so on its own it vouches for
nothing; the anchor is what a reviewer sees change in a diff. Regenerating one
without the other is exactly the failure this split exists to prevent, so this
script is the only supported way to move either.

    python scripts/reanchor_integrity.py            # rewrite both
    python scripts/reanchor_integrity.py --check    # verify, change nothing

``--check`` is what CI runs: it regenerates in memory and fails if the
committed files differ, so a package edited without re-anchoring cannot merge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recuris.metaagent import integrity  # noqa: E402
from recuris.paths import repo_root  # noqa: E402

# The protected set. Adding a package here is a deliberate act: from then on it
# cannot be edited without a re-anchor.
PROTECTED = (
    "skill_memories/tau2_retail",
    "skill_memories/tau2_airline",
    "skill_memories/skillflow",
    "skill_memories/tb21_seed",
)


def build() -> tuple[dict, dict]:
    lock = integrity.build_lock([Path(name) for name in PROTECTED])
    anchors = {
        "schema_version": 1,
        "comment": (
            "Aggregate digest of the protected Skill Memory packages. "
            "Regenerate with scripts/reanchor_integrity.py."
        ),
        "packages": {
            name: lock["packages"][name]["file_count"] for name in sorted(PROTECTED)
        },
        "tree_sha256": lock["tree_sha256"],
    }
    return lock, anchors


def dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify the committed files match the tree; write nothing",
    )
    args = ap.parse_args()

    root = repo_root()
    lock_path = root / "skill_memories" / "champions.lock.json"
    anchors_path = root / "integrity" / "anchors.json"
    lock, anchors = build()

    if args.check:
        problems = []
        for path, expected in ((lock_path, lock), (anchors_path, anchors)):
            if not path.exists():
                problems.append(f"missing: {path}")
                continue
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            # created_at is a timestamp, not content; compare everything else.
            on_disk.pop("created_at_utc", None)
            fresh = dict(expected)
            fresh.pop("created_at_utc", None)
            if on_disk != fresh:
                problems.append(f"stale: {path.relative_to(root).as_posix()}")
        if problems:
            print("INTEGRITY ANCHORS OUT OF DATE:")
            for line in problems:
                print("  " + line)
            print("\nA protected package changed. If that was intended, run")
            print("  python scripts/reanchor_integrity.py")
            print("and commit both files. If not, revert the package.")
            return 1
        print(f"ANCHORS OK tree_sha256={anchors['tree_sha256']}")
        return 0

    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(dump(lock), encoding="utf-8")
    anchors_path.write_text(dump(anchors), encoding="utf-8")
    print(f"RE-ANCHORED tree_sha256={anchors['tree_sha256']}")
    for name, count in anchors["packages"].items():
        print(f"  {name}: {count} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
