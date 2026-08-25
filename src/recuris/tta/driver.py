# -*- coding: utf-8 -*-
"""Test-time adaptation (TTA) on Terminal-Bench 2.1.

The campaign driver (``recuris.metaagent.driver``) evolves ONE archive against a *train* split
and is then judged on held-out tasks: its product must generalise. TTA is the
other end of the same machine — the archive is rebuilt per task, from that
task's own failures, and is judged on that same task. Nothing is claimed to
transfer; what is claimed is that the loop converts a repeated attempt into a
repaired one.

Information contract (this is the part reviewers will press on, so it is
enforced here rather than left to prompt discipline):

  the meta-agent sees   task instruction (public, the worker sees it too)
                        the failed attempt's trajectory
                        one bit: "a hidden verifier scored this 0"

  the meta-agent never  the verifier, the tests, the expected output,
                        the reward of any other attempt or task

``--arm`` picks what is being measured, and all three arms get the SAME attempt
budget so the comparison is not N attempts against one:

  bare   stock agent, no Skill Memory, ``rounds`` independent attempts
  m0     seed package, ``rounds`` independent attempts, NO learning between
         them (isolates the machine from the learning)
  tta    seed package, and after each failure the meta-agent writes a card
         into a per-task archive that the next attempt carries

Round 1 of ``m0`` and ``tta`` are identical by construction: neither has
learned anything yet, so any difference between them at round 1 is noise.

Usage:
  recuris tta run --taskset splits/tb21/tta_taskset_v3.json \
      --run-id demo --arm tta --rounds 2 --concurrency 3
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from recuris.paths import external_root, skill_memory_root, workspace_root

ROOT = workspace_root()


def _tasks_root() -> Path:
    """Where the Terminal-Bench 2.1 task directories live.

    Override with ``RECURIS_TB21_TASKS``; the default is where
    ``third_party/tb21/setup.sh`` puts them. The tasks and their container
    images belong to their authors and are fetched, not redistributed.
    """
    raw = os.environ.get("RECURIS_TB21_TASKS", "").strip()
    return Path(raw) if raw else external_root() / "terminal-bench-2.1" / "tasks"


BENCH = _tasks_root()

# The treatment is frozen to the published baseline config. Deliberately no
# max_turns, model_info or summarisation overrides: a throttled template costs
# roughly ten points and looks like a normal run while doing it.
WORKER_MODEL = os.environ.get(
    "RECURIS_TTA_WORKER", "openai/doubao-seed-2-0-pro-260215")
WORKER_REASONING = os.environ.get("RECURIS_TTA_WORKER_REASONING", "medium")
WORKER_API_BASE = os.environ.get("OPENAI_BASE_URL", "").strip()

# The meta-agent stays on one model whatever the policy model is. A campaign
# that swaps the executing agent is single-variable only if the card writer
# does not move with it, so the writer's endpoint is configured separately
# rather than borrowing the worker's.
META_MODEL = os.environ.get("RECURIS_TTA_META", "doubao-seed-2-1-pro-260628")
META_API_BASE = (os.environ.get("RECURIS_META_BASE_URL", "").strip()
                 or WORKER_API_BASE)

# Open-source policy models (vLLM). Setting RECURIS_TTA_WORKER_MODEL switches the
# emitted agent block to the shape the K4 baselines used: an explicit api_key,
# NO reasoning_effort (doubao-only; vLLM rejects it), and — for the qwen35-*
# family — the enable_thinking:false chat-template flag, without which the
# reasoning parser swallows the whole reply. Unset, every byte the driver emits
# is what it emitted before, so the doubao campaign stays reproducible.
WORKER_OVERRIDE = os.environ.get("RECURIS_TTA_WORKER_MODEL", "").strip()
WORKER_API_KEY = ""
WORKER_THINKING_OFF = False
if WORKER_OVERRIDE:
    WORKER_MODEL = WORKER_OVERRIDE
    WORKER_API_BASE = (os.environ.get("RECURIS_TTA_WORKER_API_BASE", "").strip()
                       or WORKER_API_BASE)
    WORKER_API_KEY = (os.environ.get("RECURIS_TTA_WORKER_API_KEY", "").strip()
                      or "dummy-local-vllm")
    WORKER_THINKING_OFF = os.environ.get(
        "RECURIS_TTA_WORKER_THINKING_OFF", "").strip().lower() in ("1", "true", "yes", "on")

WORKER_PROVENANCE = ({"model": WORKER_MODEL, "api_base": WORKER_API_BASE,
                      "thinking_off": WORKER_THINKING_OFF,
                      "reasoning_effort": None, "meta_api_base": META_API_BASE}
                     if WORKER_OVERRIDE else
                     {"model": WORKER_MODEL, "reasoning_effort": WORKER_REASONING})

# The verifier installs uv from the public net inside the container; on this
# host that only works through the site proxy, exactly as the published
# baseline config (configs/tb21_base_from_v4_provenance.yaml) does it.
PROXY = os.environ.get("RECURIS_TB21_PROXY", "")
HARBOR = os.environ.get("RECURIS_HARBOR_BIN", "harbor")

STOCK_AGENT = "terminus-2"
# harbor resolves `name` before `import_path` when the name is a known agent,
# so a config carrying both silently runs the stock agent. Emitting only the
# import path is load-bearing; the trajectory's agent.version confirms which
# one actually ran.
RECURIS_AGENT = "recuris.adapters.tb21.harbor_terminus:RecurisTerminus2"

_print_lock = threading.Lock()

# Harbor producing no result.json is infrastructure, not a task outcome. Once
# the engine is down every remaining task would be recorded as a failure and the
# arm would "finish" in seconds with a fabricated 0/N — worse than crashing,
# because the summary looks like data. Trip a shared breaker instead.
INFRA_STRIKES = 3
_infra = {"strikes": 0, "down": False}
_infra_lock = threading.Lock()


class InfrastructureDown(RuntimeError):
    pass


def _note_infra(ok: bool) -> None:
    with _infra_lock:
        if ok:
            _infra["strikes"] = 0
            return
        _infra["strikes"] += 1
        if _infra["strikes"] >= INFRA_STRIKES and not _infra["down"]:
            _infra["down"] = True
            log(f"INFRASTRUCTURE BREAKER TRIPPED after {INFRA_STRIKES} runs with "
                f"no result.json — check the Docker engine; remaining tasks are "
                f"left UNATTEMPTED rather than recorded as failures")


def _infra_down() -> bool:
    with _infra_lock:
        return _infra["down"]


def docker_alive() -> bool:
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, text=True, timeout=60).returncode == 0
    except Exception:
        return False


def log(msg: str) -> None:
    with _print_lock:
        print(f"[tta {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def tree_sha256(root: Path) -> str:
    """Content digest of a package tree.

    ``results.json``'s ``git_commit`` is not a usable version fingerprint for
    this repo (HEAD has not moved in six weeks while 345 files changed), so
    every artifact records tree digests instead.
    """
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts:
            continue
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def render_config(*, job_name: str, jobs_dir: Path, task: str,
                  package: Path | None, out: Path) -> Path:
    lines = [
        f"job_name: {job_name}",
        f"jobs_dir: {jobs_dir.as_posix()}",
        "debug: false",
        "quiet: true",
        "n_concurrent_trials: 1",
        "n_attempts: 1",
        "",
        "retry:",
        "  max_retries: 0",
        "",
        "environment:",
        "  type: docker",
        "  delete: false",          # harbor's default --rmi all deletes the task image
    ]
    if PROXY:
        lines += [
            "  env:",
            f"    HTTP_PROXY: {PROXY}", f"    HTTPS_PROXY: {PROXY}",
            f"    http_proxy: {PROXY}", f"    https_proxy: {PROXY}",
            "    NO_PROXY: localhost,127.0.0.1",
            "    no_proxy: localhost,127.0.0.1",
            "",
            "verifier:",
            "  env:",
            f"    HTTP_PROXY: {PROXY}", f"    HTTPS_PROXY: {PROXY}",
            f"    http_proxy: {PROXY}", f"    https_proxy: {PROXY}",
            "    NO_PROXY: localhost,127.0.0.1",
            "    no_proxy: localhost,127.0.0.1",
            "    UV_HTTP_TIMEOUT: '300'",
        ]
    lines += [
        "",
        "agents:",
    ]
    if package is None:
        lines.append(f"  - name: {STOCK_AGENT}")
    else:
        lines.append(f"  - import_path: {RECURIS_AGENT}")
    lines += [
        f"    model_name: {WORKER_MODEL}",
        "    kwargs:",
        f"      api_base: {WORKER_API_BASE}",
    ]
    if WORKER_OVERRIDE:
        lines.append(f"      api_key: {WORKER_API_KEY}")
    else:
        lines.append(f"      reasoning_effort: {WORKER_REASONING}")
    lines.append("      record_terminal_session: false")
    if WORKER_THINKING_OFF:
        lines += [
            "      extra_body:",
            "        chat_template_kwargs:",
            "          enable_thinking: false",
        ]
    if package is not None:
        lines.append(f"      skill_memory: {package.as_posix()}")
    lines += [
        "",
        "datasets:",
        f"  - path: {BENCH.as_posix()}",
        "    task_names:",
        f"      - {task}",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def run_harbor(config: Path, env: dict, timeout: int) -> int:
    proc = subprocess.run(
        [HARBOR, "run", "-c", str(config), "--yes"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        timeout=timeout, encoding="utf-8", errors="replace",
    )
    return proc.returncode


def read_outcome(job_dir: Path) -> dict:
    """Reward plus the artifacts the diagnosis and the audit need."""
    blank = {"reward": None, "trial_dir": None, "trajectory": None,
             "agent_version": None, "fingerprint": None, "error": None}
    res = job_dir / "result.json"
    if not res.exists():
        # Harbor wrote nothing. Almost always the Docker engine died under a
        # long local run (a documented failure mode on this host), not a task
        # outcome — the caller treats it as infrastructure, never as a zero.
        return {**blank, "error": "no result.json"}
    doc = json.loads(res.read_text(encoding="utf-8"))
    reward = None
    for ev in ((doc.get("stats") or {}).get("evals") or {}).values():
        for rv, trials in ((ev.get("reward_stats") or {}).get("reward") or {}).items():
            if trials:
                reward = float(rv)
    trial_dirs = [d for d in job_dir.iterdir() if d.is_dir()]
    out = {**blank, "reward": reward}
    if trial_dirs:
        td = trial_dirs[0]
        out["trial_dir"] = td.as_posix()
        traj = td / "agent" / "trajectory.json"
        if traj.exists():
            out["trajectory"] = traj.as_posix()
            try:
                out["agent_version"] = json.loads(
                    traj.read_text(encoding="utf-8")).get("agent", {}).get("version")
            except Exception:
                pass
        fp = td / "agent" / "recuris_fingerprint.json"
        if fp.exists():
            try:
                out["fingerprint"] = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                pass
    return out


def delivery_evidence(trial_dir: Path, needles: list[str]) -> dict:
    """Did each card body actually reach the model?

    trajectory.json records Harbor's ORIGINAL prompt, never the rewritten one
    (terminus_2.py keeps `prompt` as a loop local and our bridge rewrites it
    inside the call), so absence there proves nothing. The prompt actually sent
    is what `_query_llm` writes to agent/episode-N/prompt.txt.
    """
    hits = {n[:40]: 0 for n in needles}
    ep = trial_dir / "agent"
    if ep.exists():
        for p in sorted(ep.glob("episode-*/prompt.txt")):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for n in needles:
                if n and n in text:
                    hits[n[:40]] += 1
    return hits


DIAG_SYSTEM = (
    "You are a meta-agent that improves an agent's Skill Memory. "
    "You will see ONE failed attempt: the task instruction and the agent's full trajectory. "
    "You are told ONLY that a hidden verifier scored this attempt 0. "
    "You CANNOT see the verifier, the tests, or any expected output, and you must not guess them. "
    "Diagnose the root cause strictly from evidence inside the trajectory "
    "(internal inconsistencies, unchecked assumptions, unverified completion claims, "
    "a method that cannot support the claim being made). "
    "Then write ONE reusable skill card as a procedure that would prevent this failure class. "
    "The card must be general enough to apply to other tasks of this kind, must NOT contain any "
    "task-specific answer, literal expected value, or copied output, and must be <= 200 words. "
    "If a previous card is shown, you may supersede it; say so in root_cause. "
    'Output strict JSON: {"root_cause": "...", "evidence_from_trajectory": "...", '
    '"card_id": "snake_case_identifier", "card_title": "...", "card_body": "..."}'
)


def diagnose(instruction: str, trajectory_path: Path, prior_cards: list[str],
             api_key: str) -> dict | None:
    from openai import OpenAI
    doc = json.loads(trajectory_path.read_text(encoding="utf-8"))
    steps = []
    for i, s in enumerate(doc.get("steps", [])):
        steps.append(f"--- STEP {i} ---\n" + json.dumps(s, ensure_ascii=False)[:4000])
    traj = "\n".join(steps)[:60000]
    prior = ""
    if prior_cards:
        prior = "\n\n# CARDS ALREADY IN THE ARCHIVE (the agent had these and still failed)\n" \
                + "\n\n".join(prior_cards)
    user = (f"# TASK INSTRUCTION\n{instruction}\n{prior}\n\n"
            f"# AGENT TRAJECTORY (failed, verifier score = 0)\n{traj}")
    cli = OpenAI(api_key=api_key, base_url=META_API_BASE)
    for attempt in range(3):
        try:
            r = cli.chat.completions.create(
                model=META_MODEL,
                messages=[{"role": "system", "content": DIAG_SYSTEM},
                          {"role": "user", "content": user}],
                temperature=0.0, extra_body={"thinking": {"type": "enabled"}})
            return json.loads(r.choices[0].message.content)
        except Exception as exc:                       # TPM limits are per-minute
            log(f"  diagnosis attempt {attempt + 1} failed: {type(exc).__name__}: {exc}")
            time.sleep(60 * (attempt + 1))
    return None


CARD_TEMPLATE = """---
id: {cid}
type: procedure
trigger:
  event: turn_start
  tool: "*"
source: tta-round-{rnd}
---
{body}
"""

# A card that quotes the task's own numbers back is memorisation, not a rule.
# Cheap syntactic guard; the real test is cross-task transfer.
LEAK_PATTERNS = [re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), re.compile(r"\b\d{4,}\b")]


def leak_flags(body: str) -> list[str]:
    return [p.pattern for p in LEAK_PATTERNS if p.search(body)]


def run_task(task: dict, args, env: dict, api_key: str, run_dir: Path,
             instruction_of) -> dict:
    name = task["task"]
    tdir = run_dir / name
    tdir.mkdir(parents=True, exist_ok=True)

    # Resume, round by round rather than all-or-nothing. Raising --rounds on a
    # finished run must extend it, not replay it: re-running a past round would
    # re-charge its diagnosis and could mint a differently-worded card than the
    # one that round's sealed job actually carried, silently breaking
    # provenance. So completed rounds are read from the record, the per-task
    # archive is left exactly as those rounds built it, and only the new rounds
    # execute.
    done = tdir / "record.json"
    rec = {"task": name, "stratum": task.get("stratum"), "arm": args.arm,
           "rounds": [], "passed_at_round": None, "leak_flags": []}
    package = tdir / "package" if args.arm in ("m0", "tta") else None
    resume_from = 1
    if done.exists():
        try:
            prev = json.loads(done.read_text(encoding="utf-8"))
            prev_rounds = prev.get("rounds", [])
            if prev.get("passed_at_round"):
                log(f"{name}: already solved at round {prev['passed_at_round']}")
                return prev
            if prev_rounds and (package is None or package.exists()):
                rec["rounds"] = prev_rounds
                rec["leak_flags"] = prev.get("leak_flags", [])
                resume_from = max(r["round"] for r in prev_rounds) + 1
                if resume_from > args.rounds:
                    log(f"{name}: already exhausted {args.rounds} rounds")
                    return prev
                log(f"{name}: extending from round {resume_from} "
                    f"(archive kept as rounds 1..{resume_from - 1} built it)")
        except Exception:
            pass

    if package is not None and resume_from == 1:
        if package.exists():
            shutil.rmtree(package)
        shutil.copytree(Path(args.seed_package), package,
                        ignore=shutil.ignore_patterns("__pycache__"))

    for rnd in range(resume_from, args.rounds + 1):
        if _infra_down():
            rec["unattempted"] = True
            raise InfrastructureDown(f"{name}: breaker already tripped")
        job = f"{args.run_id}_{args.arm}_{name}_r{rnd}"
        job_dir = run_dir / "jobs" / job
        if (job_dir / "result.json").exists():
            log(f"{name} r{rnd}: reusing sealed job")
        else:
            cfg = render_config(job_name=job, jobs_dir=run_dir / "jobs",
                                task=name, package=package, out=tdir / f"r{rnd}.yaml")
            try:
                run_harbor(cfg, env, args.task_timeout)
            except subprocess.TimeoutExpired:
                log(f"{name} r{rnd}: harbor timeout")
        out = read_outcome(job_dir)
        _note_infra(out.get("error") is None)
        if out.get("error") and _infra_down():
            rec["unattempted"] = True
            raise InfrastructureDown(f"{name} r{rnd}: {out['error']}")
        entry = {"round": rnd, "reward": out["reward"],
                 "agent_version": out["agent_version"],
                 "package_sha256": tree_sha256(package) if package else None}
        if package and out["trial_dir"]:
            bodies = [p.read_text(encoding="utf-8").split("---", 2)[-1].strip()[:120]
                      for p in sorted((package / "em").rglob("*.md"))]
            entry["delivered"] = delivery_evidence(Path(out["trial_dir"]), bodies)
        rec["rounds"].append(entry)
        log(f"{name} r{rnd}: reward={out['reward']} ver={out['agent_version']}")

        if out["reward"] and out["reward"] > 0:
            rec["passed_at_round"] = rnd
            break
        if args.arm != "tta" or rnd == args.rounds or not out["trajectory"]:
            continue

        prior = [p.read_text(encoding="utf-8") for p in sorted((package / "em").rglob("*.md"))]
        diag = diagnose(instruction_of(name), Path(out["trajectory"]), prior, api_key)
        if not diag:
            log(f"{name} r{rnd}: diagnosis unavailable; next round carries the same archive")
            continue
        (tdir / f"diagnosis_r{rnd}.json").write_text(
            json.dumps(diag, ensure_ascii=False, indent=1), encoding="utf-8")
        cid = re.sub(r"[^a-z0-9_]", "_", str(diag.get("card_id") or f"tta_r{rnd}").lower())[:48]
        body = str(diag.get("card_body") or "").strip()
        flags = leak_flags(body)
        if flags:
            rec["leak_flags"].append({"round": rnd, "card": cid, "patterns": flags})
            log(f"{name} r{rnd}: LEAK-FLAG on {cid} -> {flags}")
        (package / "em" / f"{cid}.md").write_text(
            CARD_TEMPLATE.format(cid=cid, rnd=rnd, body=body), encoding="utf-8")
        entry["card_written"] = cid
        log(f"{name} r{rnd}: card '{cid}' added ({len(body)} chars)")

    rec["attempts_used"] = len(rec["rounds"])
    (tdir / "record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--taskset", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--arm", required=True, choices=["bare", "m0", "tta"])
    ap.add_argument("--seed-package", default="tb21_seed",
                    help="Skill Memory package the m0 and tta arms start from: "
                         "a path, or a name under skill_memories/")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--task-timeout", type=int, default=3600)
    ap.add_argument("--limit", type=int, default=0, help="first N tasks only (smoke)")
    ap.add_argument("--tasks", nargs="*", default=None,
                    help="run only these task names, whatever their position "
                         "in the taskset; overrides --limit")
    ap.add_argument("--out-root", default="runs/tb21_tta",
                    help="relative to the workspace root (RECURIS_WORKSPACE)")
    args = ap.parse_args()

    if args.arm == "bare" and args.rounds < 1:
        ap.error("--rounds must be >= 1")

    taskset = json.loads(Path(args.taskset).read_text(encoding="utf-8"))
    tasks = taskset["tasks"]
    if args.tasks:
        wanted = set(args.tasks)
        tasks = [t for t in tasks if t["task"] in wanted]
        unknown = wanted - {t["task"] for t in tasks}
        if unknown:
            ap.error(f"taskset has no such tasks: {sorted(unknown)}")
    elif args.limit:
        tasks = tasks[: args.limit]

    if not WORKER_API_BASE:
        sys.exit("OPENAI_BASE_URL is not set; the worker needs an endpoint")
    if not BENCH.is_dir():
        sys.exit(
            f"Terminal-Bench 2.1 tasks not found at {BENCH}. "
            "Run `bash third_party/tb21/setup.sh`, or set RECURIS_TB21_TASKS."
        )

    seed_package = Path(args.seed_package)
    if not seed_package.exists():
        seed_package = skill_memory_root() / args.seed_package
    if args.arm != "bare" and not seed_package.is_dir():
        ap.error(f"seed package not found: {args.seed_package}")
    args.seed_package = str(seed_package)

    run_dir = ROOT / args.out_root / f"{args.run_id}_{args.arm}"
    run_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # A .env file is a convenience, not a requirement: exported variables
    # work on their own, which is what a container or a CI job will have.
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    api_key = env.get("OPENAI_API_KEY", "")
    if not api_key:
        sys.exit("OPENAI_API_KEY is not set (export it, or put it in .env)")

    instr_cache: dict[str, str] = {}

    def instruction_of(name: str) -> str:
        if name not in instr_cache:
            instr_cache[name] = (BENCH / name / "instruction.md").read_text(encoding="utf-8")
        return instr_cache[name]

    (run_dir / "provenance.json").write_text(json.dumps({
        "run_id": args.run_id, "arm": args.arm, "rounds": args.rounds,
        "taskset": taskset.get("name"), "n_tasks": len(tasks),
        "seed_package": args.seed_package if args.arm != "bare" else None,
        "seed_package_sha256": (tree_sha256(Path(args.seed_package))
                                if args.arm != "bare" else None),
        "src_tree_sha256": tree_sha256(Path(__file__).resolve().parents[1]),
        "worker": WORKER_PROVENANCE,
        "meta_model": META_MODEL if args.arm == "tta" else None,
        "information_contract": "meta-agent sees instruction + trajectory + one fail bit; "
                                "never the verifier, tests, or expected output",
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    if not docker_alive():
        sys.exit("docker engine is not responding — start it before launching an arm")
    log(f"run={args.run_id} arm={args.arm} tasks={len(tasks)} rounds={args.rounds} "
        f"cc={args.concurrency}")
    records = []
    with futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(run_task, t, args, env, api_key, run_dir, instruction_of): t
                for t in tasks}
        for f in futures.as_completed(futs):
            try:
                records.append(f.result())
            except InfrastructureDown as exc:
                name = futs[f]["task"]
                log(f"{name}: UNATTEMPTED ({exc})")
                records.append({"task": name, "arm": args.arm,
                                "stratum": futs[f].get("stratum"),
                                "unattempted": True, "error": repr(exc)})
            except Exception as exc:
                name = futs[f]["task"]
                log(f"{name}: FAILED {type(exc).__name__}: {exc}")
                records.append({"task": name, "arm": args.arm,
                                "stratum": futs[f].get("stratum"), "error": repr(exc)})

    attempted = [r for r in records if not r.get("unattempted") and not r.get("error")]
    passed = [r for r in records if r.get("passed_at_round")]
    summary = {
        "run_id": args.run_id, "arm": args.arm, "n": len(records),
        "n_attempted": len(attempted),
        "n_unattempted": len(records) - len(attempted),
        "infrastructure_down": _infra_down(),
        f"pass_at_{args.rounds}": len(passed),
        "rounds_cap": args.rounds,
        "rate": round(len(passed) / len(attempted), 4) if attempted else None,
        "by_round": {str(r): sum(1 for x in passed if x["passed_at_round"] == r)
                     for r in range(1, args.rounds + 1)},
        "by_stratum": {s: {"n": sum(1 for x in records if x.get("stratum") == s),
                           "passed": sum(1 for x in passed if x.get("stratum") == s)}
                       for s in ("A", "B")},
        "leak_flagged": [r["task"] for r in records if r.get("leak_flags")],
        # Budget actually consumed. Both arms are capped at --rounds, but both
        # stop on success, so a method that solves early is also cheaper. Report
        # this next to the success rate: same cap does not mean same cost.
        "attempts_used_total": sum(r.get("attempts_used", 0) for r in records),
        "attempts_used_mean": (round(sum(r.get("attempts_used", 0) for r in records)
                                     / len(attempted), 3) if attempted else None),
        "records": records,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    log(f"DONE arm={args.arm} solved-within-{args.rounds} = {len(passed)}/{len(attempted)} attempted "
        f"({summary['n_unattempted']} unattempted) by_round={summary['by_round']} "
        f"leak_flagged={len(summary['leak_flagged'])}"
        + ("  [INFRASTRUCTURE DOWN — arm incomplete]" if _infra_down() else ""))


if __name__ == "__main__":
    main()
