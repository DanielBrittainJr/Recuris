#!/usr/bin/env python3
"""Verify a fetched third-party tree against a committed CHECKSUMS.json.

    python scripts/verify_checksums.py third_party/tau2/CHECKSUMS.json <root>
    python scripts/verify_checksums.py third_party/tau2/CHECKSUMS.json <root> --write

Benchmark payloads are fetched rather than redistributed, which means the
contents of the tree a user ends up with are not under our control. This is how
a run finds out that it is scoring against different data before it produces a
number rather than after.

Text files are normalised to LF before hashing. Git rewrites line endings on
some platforms, so a byte-exact comparison would report a mismatch for a tree
that is in fact correct -- and a check that cries wolf gets switched off.

Whether a file counts as text is decided by looking at its bytes, not at its
extension. An extension allowlist gets this wrong in one direction only, and
silently: an unlisted text file is hashed raw, so the same upstream tree digests
differently on a CRLF checkout than on an LF one. That is not hypothetical. The
allowlist this replaces omitted ``.dot``, and tau2's telecom domain ships three
of them totalling exactly 375 lines, so a manifest written on Windows rejected
every checkout on Linux and macOS by exactly 375 bytes.

``--write`` regenerates the manifest from the tree instead of checking against
it. Use it when the pinned upstream revision changes, and commit the result;
hand-editing the JSON is how a digest that matched no real tree got committed in
the first place.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# How much of a file to inspect when deciding text vs binary. Git uses the same
# heuristic (a NUL byte in the first 8k means binary) and the same window.
SNIFF_BYTES = 8192


def is_binary(raw: bytes) -> bool:
    return b"\x00" in raw[:SNIFF_BYTES]


def tree_digest(root: Path) -> tuple[str, int, int]:
    """SHA-256 over (relative path, LF-normalised content) for every file."""
    digest = hashlib.sha256()
    count = 0
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        raw = path.read_bytes()
        if not is_binary(raw):
            raw = raw.replace(b"\r\n", b"\n")
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(raw)
        count += 1
        total += len(raw)
    return digest.hexdigest(), count, total


def write(manifest_path: Path, base: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("trees") or {}
    if not entries:
        print(f"{manifest_path}: no 'trees' to write")
        return 2
    changed = 0
    for relative in sorted(entries):
        target = base / relative
        if not target.is_dir():
            print(f"  MISSING  {relative}")
            return 1
        sha256, count, total = tree_digest(target)
        before = entries[relative]
        entries[relative] = {"sha256": sha256, "files": count, "bytes": total}
        mark = "same"
        if before.get("sha256") != sha256:
            changed += 1
            mark = f"UPDATED (was {before.get('sha256', '?')[:12]})"
        print(f"  {relative}  {sha256[:12]}  {count} files  {mark}")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nwrote {manifest_path} ({changed} tree(s) changed)")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--write"]
    if len(args) != 2:
        print(__doc__)
        return 2
    manifest_path, base = Path(args[0]), Path(args[1])
    if "--write" in argv:
        return write(manifest_path, base)

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
