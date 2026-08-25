# -*- coding: utf-8 -*-
"""Gold-id sanitizer — makes answer-key leakage structurally impossible.

Two jobs, both deterministic:
  1. mask():  replace every real domain id in text fed TO the meta-agent
              (answer keys, trajectories) with a stable synthetic placeholder,
              so the model never even sees a real id it could copy into a card.
  2. scan():  backstop — given card text, return any real domain id found
              (used by ma_lint --domain and by the driver as a red line).

The gold-id universe is harvested by regex from the domain's db.json +
tasks.json (every order id, 7+-digit numeric id, user id, payment id, email
that exists in the benchmark data). A card may only ever contain placeholder
ids, so ANY exact match against the harvested set is a leak.

Usage:
  recuris metaagent sanitize --domain retail --scan-pkg skill_memories/<package>
  recuris metaagent sanitize --domain retail --self-test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from recuris.metaagent import reachability as _reachability
from recuris.paths import skill_memory_root, tau2_data_dir, workspace_root

HERE = Path(__file__).resolve().parent
ROOT = workspace_root()
DOMAINS_DIR = tau2_data_dir(required=False) / "tau2" / "domains"
CACHE_DIR = ROOT / ".recuris_cache" / "gold_ids"

# id-shaped token patterns, per class. Harvest = every match in db.json+tasks.json.
ID_PATTERNS = {
    "order": re.compile(r"#W\d{5,}"),
    "numeric": re.compile(r"\b\d{7,}\b"),
    "user": re.compile(r"\b[a-z]+(?:_[a-z]+)+_\d{3,}\b"),
    "payment": re.compile(r"\b(?:gift_card|credit_card|paypal)_\d+\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b"),
}
PLACEHOLDER_FMT = {
    "order": "#W000000{n}",          # keeps the #W shape so tool-call syntax stays readable
    "numeric": "10000000{n:02d}",    # 10-digit-looking, validated non-real at build time
    "user": "sample_user_{n:04d}",
    "payment": "gift_card_000000{n}",
    "email": "user{n}@example.com",
}


def domain_interface_vocabulary(domain: str, benchmark: str = "tau2") -> set[str]:
    """Identifier-shaped strings that are part of the domain's *interface*.

    Tool names (including Tau2 discoverable sub-tools such as
    ``submit_cash_back_dispute_0589``) are how a card routes work — they are
    not answer-key material, yet several match the id-shaped patterns below.
    Harvesting them would mask them in evidence (breaking tool-call syntax the
    diagnosis must read) and red-line any card that names the tool it rides,
    which is exactly what disarmed the banking_knowledge candidates.  They are
    therefore exempt from harvest, mask, and scan.
    """
    try:
        return set(_reachability.tool_capabilities_for(benchmark, domain))
    except Exception:
        # No tool face discoverable in this environment: fall back to the
        # historical behavior (harvest everything).  Over-masking is the
        # fail-closed direction; campaigns always run where tools resolve.
        return set()


def _subtract_vocabulary(gold: dict, vocabulary: set[str]) -> dict:
    return {
        cls: sorted(set(ids) - vocabulary)
        for cls, ids in gold.items()
    }


def harvest_gold_ids(
    domain: str, refresh: bool = False, benchmark: str = "tau2"
) -> dict:
    """{class: sorted list of real ids} harvested from the domain data files.
    Cached to metaagent/runs/gold_ids_<domain>.json (data files never change).
    The interface vocabulary is subtracted both before caching and after a
    cache load, so caches written before the vocabulary exemption existed are
    cleaned on read rather than trusted."""
    vocabulary = domain_interface_vocabulary(domain, benchmark)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"gold_ids_{domain}.json"
    if cache.exists() and not refresh:
        return _subtract_vocabulary(
            json.loads(cache.read_text(encoding="utf-8")), vocabulary
        )
    ddir = DOMAINS_DIR / domain
    text = ""
    for fname in ("db.json", "tasks.json"):
        f = ddir / fname
        if f.exists():
            text += f.read_text(encoding="utf-8", errors="ignore")
    if not text:
        raise FileNotFoundError(f"no db.json/tasks.json under {ddir}")
    out = {}
    for cls, pat in ID_PATTERNS.items():
        out[cls] = sorted(set(pat.findall(text)) - vocabulary)
    # a placeholder must never collide with a real id — validate the format space
    for cls, fmt in PLACEHOLDER_FMT.items():
        real = set(out.get(cls, []))
        for n in range(1, 100):
            if fmt.format(n=n) in real:
                raise RuntimeError(f"placeholder collision: {fmt.format(n=n)} is a REAL {cls} id")
    cache.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


class Masker:
    """Stable text→text masking: the same real id maps to the same placeholder
    for the lifetime of this Masker (one Masker per round keeps a whole round's
    evidence coherent)."""

    def __init__(self, gold: dict):
        self.gold = {cls: set(v) for cls, v in gold.items()}
        self.mapping: dict[str, str] = {}
        self._counters = {cls: 0 for cls in ID_PATTERNS}

    def _placeholder(self, cls: str, real: str) -> str:
        if real not in self.mapping:
            self._counters[cls] += 1
            self.mapping[real] = PLACEHOLDER_FMT[cls].format(n=self._counters[cls])
        return self.mapping[real]

    def mask(self, text: str) -> str:
        # order class first (its matches contain digits the numeric class would split)
        for cls in ("order", "payment", "user", "email", "numeric"):
            pat = ID_PATTERNS[cls]
            real_set = self.gold.get(cls, set())

            def _sub(m, _cls=cls, _real=real_set):
                tok = m.group(0)
                return self._placeholder(_cls, tok) if tok in _real else tok

            text = pat.sub(_sub, text)
        return text


def scan_text(text: str, gold: dict) -> list[str]:
    """Return every REAL domain id present in text (empty list = clean)."""
    hits = []
    for cls, pat in ID_PATTERNS.items():
        real = set(gold.get(cls, []))
        for tok in set(pat.findall(text)):
            if tok in real:
                hits.append(f"{cls}:{tok}")
    return sorted(hits)


def scan_package(pkg: Path, gold: dict) -> dict:
    """{relative card path: [leaked ids]} for every *.md/manifest under pkg."""
    leaks = {}
    for p in sorted(pkg.rglob("*")):
        if p.is_file() and p.suffix.lower() in {".md", ".yaml", ".yml"}:
            hits = scan_text(p.read_text(encoding="utf-8", errors="ignore"), gold)
            if hits:
                leaks[str(p.relative_to(pkg))] = hits
    return leaks


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--benchmark", default="tau2", choices=["tau2", "tb21"])
    ap.add_argument("--scan-pkg")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    gold = harvest_gold_ids(
        args.domain, refresh=args.refresh, benchmark=args.benchmark
    )
    print(f"gold-id universe for {args.domain}: "
          + ", ".join(f"{cls}={len(v)}" for cls, v in gold.items()))

    if args.scan_pkg:
        leaks = scan_package(Path(args.scan_pkg), gold)
        if leaks:
            print(f"\nLEAK — {sum(len(v) for v in leaks.values())} real id(s) found:")
            for rel, hits in leaks.items():
                print(f"  X {rel}: {hits}")
            sys.exit(1)
        print("\nCLEAN — no real domain ids in the package.")

    if args.self_test:
        ok = True
        # 1) the hand-written champion uses placeholders only -> must be CLEAN
        v3 = skill_memory_root() / "tau2_retail"
        if v3.exists():
            leaks = scan_package(v3, gold)
            print(f"self-test v3 (expect CLEAN): {'CLEAN' if not leaks else f'LEAK {leaks}'}")
            ok = ok and not leaks
        # 2) the known-contaminated ma_work card -> must be caught
        bad = (ROOT / "skill-memories" / "tau2_retail_ma_work" / "tau2_retail_ma_work"
               / "action_result" / "exchange_delivered_order_items_exemplar.md")
        if bad.exists():
            hits = scan_text(bad.read_text(encoding="utf-8", errors="ignore"), gold)
            print(f"self-test leaked card (expect LEAK): {'LEAK ' + str(hits) if hits else 'MISSED!'}")
            ok = ok and bool(hits)
        # 3) masking round-trips: masked text contains no real ids
        m = Masker(gold)
        sample = "call exchange_delivered_order_items(order_id=\"{}\")".format(
            (gold.get("order") or ["#W0"])[0])
        masked = m.mask(sample)
        clean = not scan_text(masked, gold)
        print(f"self-test masking (expect CLEAN after mask): {'CLEAN' if clean else 'DIRTY: ' + masked}")
        ok = ok and clean
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
