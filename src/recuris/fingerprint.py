"""Mechanism fingerprint - "read the mechanism before you read the score".


Every mechanism increments named counters and appends structured events; each
conversation dumps its fingerprint next to the results. A run whose score moved
but whose fingerprint shows zero mechanism activity is VARIANCE, not effect
(the v1-gate lesson: 0 triggers yet "+20%").
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from typing import Any

_IO_LOCK = threading.Lock()  # sims run concurrently; jsonl appends must not interleave


class Fingerprint:
    """Per-conversation counters + event log."""

    def __init__(self, sim_tag: str = ""):
        self.sim_tag = sim_tag
        self.counters: Counter[str] = Counter()
        self.events: list[dict[str, Any]] = []

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def event(self, kind: str, **payload: Any) -> None:
        self.counters[kind] += 1
        self.events.append({"kind": kind, **payload})

    def summary(self) -> dict[str, Any]:
        return {"sim_tag": self.sim_tag, "counters": dict(self.counters)}

    # ── persistence ──────────────────────────────────────────────────────
    def dump(self, path: str | None = None) -> None:
        """Append one jsonl line. Default path from RECURIS_FINGERPRINT (env)."""
        path = path or os.getenv("RECURIS_FINGERPRINT")
        if not path:
            return
        line = json.dumps(
            {"sim_tag": self.sim_tag, "counters": dict(self.counters), "events": self.events},
            ensure_ascii=False,
            default=str,
        )
        try:
            with _IO_LOCK, open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # observability must never break the run
            pass


def aggregate(jsonl_path: str) -> dict[str, Any]:
    """Aggregate a fingerprint jsonl into run-level counters (scorecard input)."""
    total: Counter[str] = Counter()
    sims = 0
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            sims += 1
            total.update(rec.get("counters") or {})
    return {"sims": sims, "counters": dict(total)}
