"""Scorecard: the mechanism fingerprint of a run, and only then its score.

Read the mechanism before you read the score. A score on its own cannot
distinguish a package that worked from a package that never fired, and the
second case looks exactly like the first until you go looking. So this refuses
to print a score without first printing what actually happened: gate fires,
truth bounces, review behaviour, sanitizer strips.

Usage:
  recuris scorecard <results.json or simulation dir> [--fingerprint fp.jsonl]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

RETAIL_WRITE_TOOLS = {
    "exchange_delivered_order_items", "return_delivered_order_items",
    "cancel_pending_order", "modify_pending_order_items",
    "modify_pending_order_payment", "modify_pending_order_address",
    "modify_user_address",
}

AIRLINE_WRITE_TOOLS = {
    "book_reservation", "update_reservation_flights",
    "update_reservation_passengers", "update_reservation_baggages",
    "cancel_reservation", "send_certificate",
}

BANKING_WRITE_TOOLS = {
    # banking_knowledge: DB-mutating agent tools (fork tools.py @is_tool WRITE +
    # GENERIC mutates_state — discoverable unlock/call/give each write scored records)
    "change_user_email", "log_verification",
    "call_discoverable_agent_tool", "give_discoverable_user_tool",
    "unlock_discoverable_agent_tool",
}

WRITE_TOOL_SETS = {"retail": RETAIL_WRITE_TOOLS, "airline": AIRLINE_WRITE_TOOLS,
                   "banking": BANKING_WRITE_TOOLS}


# --- board audit (B8 rewrite, 2026-07-06) -------------------------------
# The old check counted EVERY assistant message containing the marker as a
# breach — but the fork's orchestrator legitimately appends the harness board
# to the trajectory's assistant messages on user routing (orchestrator.py
# ~1116), so every board-ON run screamed a false "INVARIANT 3 BREACH"
# (v31_A: 214/214 false positives), while true forgeries with the exact
# marker cannot reach trajectories at all (agent-side sanitizer strips them,
# fp counter sanitizer_strip) and VARIANT forgeries were invisible. The audit
# below flags what can actually be wrong:
#   forged_body   — model-authored board-like lines OUTSIDE the harness block
#   tampered      — a marker block whose lines fail the render whitelist
#                   (or >1 marker block in one message)
#   forced_ctx    — a marker inside forced terminal rounds, where the
#                   orchestrator never appends a board
BOARD_MARKER = "--- Progress (system-verified)"
# tolerant union: recuris renderer (6 dots, DONE|NOT YET EXECUTED) plus the
# fork's lifecycle-variant marks — so a lifecycle run doesn't false-flag.
BOARD_LINE_RE = re.compile(
    r"^\d+\. .+ \.{3,} ?(DONE|NOT YET EXECUTED|NOT PERMITTED BY POLICY|IN PROGRESS|"
    r"CANNOT COMPLETE|AWAITING YOUR CONFIRMATION)\s*$")
BOARD_FOOTER_RE = re.compile(r"^! .+$")
BOARD_FUZZY_RE = re.compile(r"progress\s*\(.{0,16}verified|system.?verified", re.I)


def audit_board(content: str, in_forced_round: bool):
    """Audit one assistant message. Returns (carries_marker, forged_body,
    tampered, forced_ctx) counts for that message."""
    idx = content.find(BOARD_MARKER)
    body = content if idx < 0 else content[:idx]
    forged_body = sum(
        1 for ln in (line.strip() for line in body.splitlines()) if ln
        and (BOARD_LINE_RE.match(ln) or BOARD_FUZZY_RE.search(ln))
    )
    if idx < 0:
        return False, forged_body, 0, 0
    tampered = 0
    for ln in (line.rstrip() for line in content[idx:].splitlines()):
        if not ln or ln.startswith(BOARD_MARKER):
            continue
        if BOARD_LINE_RE.match(ln) or BOARD_FOOTER_RE.match(ln):
            continue
        tampered += 1
    if content.count(BOARD_MARKER) > 1:
        tampered += 1
    return True, forged_body, tampered, (1 if in_forced_round else 0)


def detect_write_tools(sims, forced="auto"):
    """Pick the domain write-tool set. Retail/airline tool names are disjoint,
    so 'auto' selects whichever set actually appears in the trajectories.
    Returns (domain_name, tool_set, seen_count); seen_count==0 flags a wrong
    choice loudly instead of silently reading real_write_success=0 (the bug this
    replaces: RETAIL_WRITE_TOOLS was hardcoded, so every airline run scored 0
    real writes and a permanent false DONE-vs-write MISMATCH)."""
    seen = set()
    for s in sims:
        for m in s.get("messages") or []:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    if tc.get("name"):
                        seen.add(tc["name"])
    if forced and forced != "auto":
        ts = WRITE_TOOL_SETS[forced]
        return forced, ts, len(ts & seen)
    name, ts = max(WRITE_TOOL_SETS.items(), key=lambda kv: len(kv[1] & seen))
    return name, ts, len(ts & seen)


def load_results(path: Path) -> dict:
    if path.is_dir():
        cands = list(path.glob("*.json"))
        if not cands:
            sys.exit(f"no json under {path}")
        path = max(cands, key=lambda p: p.stat().st_mtime)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--fingerprint", default=None)
    ap.add_argument("--domain", default="auto", choices=["auto", "retail", "airline", "banking"],
                    help="write-tool set for real-write / DONE reconciliation "
                         "(auto-detect from trajectories by default)")
    args = ap.parse_args()

    data = load_results(Path(args.results))
    sims = data.get("simulations") or []
    domain_name, write_tools, seen_writes = detect_write_tools(sims, args.domain)
    per_task: dict[str, list[float]] = defaultdict(list)
    term = Counter()
    gate_nudges = 0
    forced_writes_after_nudge = 0
    board_carrying = 0      # harness-board-bearing assistant msgs (informational)
    board_forged_body = 0   # model-authored board-like text outside the block
    board_tampered = 0      # marker block failing the render whitelist
    board_forced_ctx = 0    # marker inside forced terminal rounds
    for s in sims:
        reward = ((s.get("reward_info") or {}).get("reward"))
        if reward is not None:
            per_task[str(s.get("task_id"))].append(float(reward))
        term[s.get("termination_reason")] += 1
        msgs = s.get("messages") or []
        saw_nudge = False
        in_forced = False
        for m in msgs:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "user" and isinstance(content, str):
                if content.startswith("[SYSTEM CHECK"):
                    gate_nudges += 1
                    saw_nudge = True
                    in_forced = True
                else:
                    in_forced = False  # real customer turn — normal routing
            if role == "assistant" and isinstance(content, str):
                carries, fb, tp, fc = audit_board(content, in_forced)
                board_carrying += 1 if carries else 0
                board_forged_body += fb
                board_tampered += tp
                board_forced_ctx += fc
            if saw_nudge and role == "assistant" and m.get("tool_calls"):
                forced_writes_after_nudge += 1
                saw_nudge = False

    # real successful write executions in OFFICIAL trajectories (replay ground truth)
    real_write_success = 0
    for s in sims:
        msgs = s.get("messages") or []
        results_ok = {m.get("id") for m in msgs
                      if m.get("role") == "tool" and not m.get("error")}
        for m in msgs:
            if m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                if tc.get("name") in write_tools and tc.get("id") in results_ok:
                    real_write_success += 1

    n = sum(len(v) for v in per_task.values())
    passed = sum(1 for v in per_task.values() for r in v if r >= 1.0)
    gate_fire_sims = sum(
        1
        for s in sims
        if any(
            (m.get("role") == "user" and isinstance(m.get("content"), str)
             and (m.get("content") or "").startswith("[SYSTEM CHECK"))
            for m in (s.get("messages") or [])
        )
    )

    print("=" * 62)
    print("MECHANISM FINGERPRINT (read this BEFORE the score)")
    print("=" * 62)
    print(f"sims: {len(sims)}   terminations: {dict(term)}")
    print(f"write-tool set: {domain_name} ({len(write_tools)} tools; "
          f"{seen_writes} seen in trajectories)"
          + ("   !! NONE seen — wrong --domain / no writes?" if seen_writes == 0 else ""))
    print(f"terminal-gate nudges in trajectories: {gate_nudges} "
          f"({gate_fire_sims}/{len(sims)} sims = {gate_fire_sims / max(1, len(sims)):.0%} fire rate)")
    print(f"forced tool-call rounds after nudge: {forced_writes_after_nudge}")
    board_bad = board_forged_body + board_tampered + board_forced_ctx
    print(f"board audit: assistant msgs carrying the harness board: {board_carrying} "
          "(expected when the status board is ON)")
    print(f"  forged board-like text outside block: {board_forged_body} | "
          f"block failing whitelist: {board_tampered} | "
          f"board in forced terminal rounds: {board_forced_ctx} "
          f"{'!! INVARIANT 3 BREACH' if board_bad else '(clean)'}")
    print(f"real successful WRITE executions in trajectories: {real_write_success}")
    if args.fingerprint and Path(args.fingerprint).exists():
        from recuris.fingerprint import aggregate

        agg = aggregate(args.fingerprint)
        print(f"agent-side fingerprint ({agg['sims']} conversations):")
        if agg["sims"] != len(sims):
            print(f"  !! fp lines ({agg['sims']}) != results sims ({len(sims)}) — "
                  "the fingerprint file likely has stale/retry/resume duplicates; "
                  "its counters and the DONE-vs-write check below are UNRELIABLE "
                  "(start from a fresh fp, or don't reuse the path across runs)")
        for k in sorted(agg["counters"]):
            print(f"  {k}: {agg['counters'][k]}")
        strips = agg["counters"].get("sanitizer_strip", 0)
        print(f"  -> INVARIANT 3 pair: attempted board forgeries stripped pre-send "
              f"(sanitizer_strip): {strips}; escaped into trajectories (board audit "
              f"above): {board_bad}")
        done_marks = agg["counters"].get("ground_covered", 0)
        unmatched = agg["counters"].get("ground_unmatched", 0)
        # terminal-edge writes: success in the FINAL agent turn -> no later
        # run_turn exists to ground them (same structural property as the fork;
        # conservative direction — never a false DONE).
        edge = 0
        for s in sims:
            msgs = s.get("messages") or []
            ok_ids = {m.get("id") for m in msgs
                      if m.get("role") == "tool" and not m.get("error")}
            last_a = max((i for i, m in enumerate(msgs)
                          if m.get("role") == "assistant"), default=-1)
            if last_a >= 0:
                for tc in msgs[last_a].get("tool_calls") or []:
                    if tc.get("name") in write_tools and tc.get("id") in ok_ids:
                        edge += 1
        lhs = done_marks + unmatched + edge
        verdict = "(consistent)" if lhs == real_write_success else "!! MISMATCH — investigate"
        print(f"DONE-vs-write correspondence: DONE={done_marks} + unmatched={unmatched} "
              f"+ terminal-edge={edge} vs real-writes={real_write_success} {verdict}")
    print("-" * 62)
    print("SCORE")
    print(f"total: {passed}/{n} = {passed / max(1, n):.1%}")
    for tid in sorted(per_task, key=lambda x: (len(x), x)):
        rs = per_task[tid]
        print(f"  task {tid}: {sum(1 for r in rs if r >= 1.0)}/{len(rs)}")


if __name__ == "__main__":
    main()
