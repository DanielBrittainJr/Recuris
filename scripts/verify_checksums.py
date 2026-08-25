#!/usr/bin/env python3
"""Verify a fetched third-party tree against a committed CHECKSUMS.json.

    python scripts/verify_checksums.py third_party/tau2/CHECKSUMS.json <root>

Benchmark payloads are fetched rather than redistributed, which means the
contents of the tree a user ends up with are not under our control. This is how
a run finds out that it is scoring against different data before it produces a
number rather than after.

Text files are normalised to LF before hashing. Git rewrites line endings on
some platforms, so a byte-exact comparison would report a mismatch for a tree
that is in fact correct -- and a check that cries wolf gets switched off.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

TEXT_SUFFIXES = {".md", ".json", ".txt", ".py", ".yaml", ".yml", ".toml", ".j2", ".cfg"}


def tree_digest(root: Path) -> tuple[str, int, int]:
    """SHA-256 over (relative path, LF-normalised content) for every file."""
    digest = hashlib.sha256()
    count = 0
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        raw = path.read_bytes()
        if path.suffix in TEXT_SUFFIXES:
            raw = raw.replace(b"\r\n", b"\n")
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(raw)
        count += 1
        total += len(raw)
    return digest.hexdigest(), count, total


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    manifest_path, base = Path(argv[0]), Path(argv[1])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("trees") or {}
    if not entries:
        print(f"{manifest_path}: no 'trees' to check")
        return 2

    failures = 0
    for relative, expected in sorted(entries.items()):
        target = base / relative
        if not target.is_dir():
            print(f"  MISSING  {relative}")
            failures += 1
            continue
        actual, count, total = tree_digest(target)
        if actual != expected["sha256"]:
            print(f"  DIFFERS  {relative}")
            print(f"           expected {expected['sha256']}")
            print(f"           actual   {actual}")
            print(f"           files {count} (expected {expected['files']}), "
                  f"bytes {total} (expected {expected['bytes']})")
            failures += 1
        else:
            print(f"  ok       {relative}  ({count} files)")

    if failures:
        print(
            f"\n{failures} tree(s) do not match {manifest_path}.\n"
            "The benchmark payload is not the one these results were produced "
            "against. Re-run the setup script; if it still differs, upstream has "
            "changed and the numbers here are not comparable to yours."
        )
        return 1
    print(f"\nAll trees match {manifest_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
