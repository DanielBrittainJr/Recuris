#!/usr/bin/env python3
"""Fail on anything credential-shaped, or on infrastructure that names our machines.

Two classes, and the distinction matters.

**Credentials.** A leaked key is unrecoverable by editing: once it is in a
commit, in a clone, or in an archive someone downloaded, the only remedy is
rotation. So this runs in CI on every push, and it errs toward false positives,
which are cheap to allow-list.

**Infrastructure.** Host addresses, ports, and absolute paths on our machines
are not secrets, but they are noise at best and a map at worst, and they make
the repository read as an internal artefact that happened to be published.

Deliberately *not* a UUID-shaped regex over everything. Benchmark data and
Skill Memory cards are full of genuine identifiers, and a scan that reports
thirty false positives per run is a scan people learn to ignore. The patterns
below are specific to credential *syntax*, plus the literal shapes of the
infrastructure we know we had.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "external", "runs", "jobs", "ma_runs", "tb21_runs", "logs",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "dist", "build",
    "tau2data", ".recuris_cache",
}
SUFFIXES = {
    ".py", ".md", ".sh", ".yaml", ".yml", ".json", ".toml", ".cfg", ".j2",
    ".txt", ".patch", ".example", ".commit", ".map", ".lock", ".gitattributes",
    "",
}

CREDENTIAL_PATTERNS = [
    (r"\bsk-[A-Za-z0-9]{20,}", "OpenAI-style key"),
    (r"\bsk-ant-[A-Za-z0-9_-]{20,}", "Anthropic key"),
    (r"\bghp_[A-Za-z0-9]{30,}", "GitHub token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"\bAIza[0-9A-Za-z_-]{30,}", "Google API key"),
    (r"\bhf_[A-Za-z0-9]{30,}", "Hugging Face token"),
    (r"(?i)\bapi[_-]?key\s*[:=]\s*['\"][^'\"$<{][^'\"]{12,}['\"]", "inline api_key literal"),
    (r"(?i)\bauthorization\s*[:=]\s*['\"]?bearer\s+(?!\$|\{)[A-Za-z0-9._-]{16,}", "inline bearer token"),
    (r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", "private key"),
]

INFRASTRUCTURE_PATTERNS = [
    (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}\b", "host:port"),
    (r"/data_storage/", "internal absolute path"),
    (r"[A-Z]:/CurrentResearch", "internal absolute path"),
    (r"(?i)\bLAPTOP-[A-Z0-9]{6,}", "hostname"),
]

# Loopback is fine: the translating proxy genuinely runs there, and saying so
# is how a reader knows nothing leaves the machine.
ALLOWED = re.compile(r"127\.0\.0\.1|localhost|0\.0\.0\.0|1\.2\.3\.4")


def main() -> int:
    # Findings can contain any byte the repository contains; a narrow console
    # codepage must not be able to crash the scan before it reports them.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    repo = Path(__file__).resolve().parents[1]
    findings: list[tuple[str, int, str, str]] = []

    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(repo).parts):
            continue
        if path.suffix not in SUFFIXES:
            continue
        rel = path.relative_to(repo).as_posix()
        if rel == "scripts/scan_secrets.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.split("\n"), 1):
            for pattern, label in CREDENTIAL_PATTERNS + INFRASTRUCTURE_PATTERNS:
                match = re.search(pattern, line)
                if match and not ALLOWED.search(match.group(0)):
                    findings.append((rel, lineno, label, line.strip()[:110]))

    if findings:
        print(f"{len(findings)} finding(s):")
        for rel, lineno, label, line in findings:
            print(f"  {rel}:{lineno}  [{label}]  {line}")
        print(
            "\nA credential must be rotated, not just deleted: assume anything "
            "that reached a commit is already public. Infrastructure references "
            "should be replaced with an environment variable."
        )
        return 1

    print("no credentials or internal infrastructure found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
