#!/usr/bin/env python3
"""Fail if Chinese characters appear where they should not.

This repository was written bilingually and translated for release. A gate is
the only thing that keeps it translated: without one, the next edit
reintroduces a comment that most readers cannot read, and nobody notices.

One directory is deliberately exempt, and the reason is worth stating rather
than leaving implicit -- an unexplained exemption is indistinguishable from an
oversight.

``skill_memories/``, except ``_base``
    The packages are research artefacts, committed byte-for-byte as they were
    evaluated. Some cards, manifests and comments contain Chinese, because the
    meta-agent wrote them that way. Editing them for tidiness would produce a
    repository whose packages no longer correspond to any package we ran.
    ``_base`` is a template rather than a measured artefact, so it is held to
    the rule.

"""

from __future__ import annotations

import sys
from pathlib import Path

# The detection is a code-point range test rather than a regex, so that this
# file's own source stays ASCII. A checker that trips on itself, or that cannot
# print its own report on a narrow console codepage, is one people switch off.
CJK_RANGES = (
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
)

ROOTS = ("src", "scripts", "configs", "splits", "docs", "third_party")
SUFFIXES = {".py", ".md", ".sh", ".yaml", ".yml", ".json", ".toml", ".cfg", ".j2", ".txt"}

EXEMPT_PREFIXES = ("skill_memories/",)


def first_cjk(line: str) -> int | None:
    """The code point of the first Chinese character, or None."""
    for char in line:
        point = ord(char)
        for low, high in CJK_RANGES:
            if low <= point <= high:
                return point
    return None


def exempt(rel: str) -> bool:
    if rel.startswith("skill_memories/_base/"):
        return False
    return rel.startswith(EXEMPT_PREFIXES)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    repo = Path(__file__).resolve().parents[1]
    offenders: list[tuple[str, int, int]] = []

    targets = [repo / r for r in ROOTS] + [repo / "skill_memories" / "_base"]
    for root in targets:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in SUFFIXES:
                continue
            rel = path.relative_to(repo).as_posix()
            if exempt(rel):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.split("\n"), 1):
                point = first_cjk(line)
                if point is not None:
                    offenders.append((rel, lineno, point))

    if offenders:
        print(f"{len(offenders)} line(s) contain Chinese characters:")
        for rel, lineno, point in offenders:
            print(f"  {rel}:{lineno}  (first: U+{point:04X})")
        print(
            "\nShipped code and documentation are English. If a file is an "
            "evolved artefact that must stay verbatim, exempt it in this "
            "script and say why."
        )
        return 1

    print("no Chinese characters in shipped code or documentation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
