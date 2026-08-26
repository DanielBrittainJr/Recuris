# -*- coding: utf-8 -*-
"""Deterministic Meta-Agent driver — the loop is code; the model only thinks.

Round workflow (selected with --meta-workflow):
  split       keeps diagnosis/review and package editing in separate sessions
  continuous  uses one session to diagnose and edit the disposable candidate
  hierarchical maps trajectory batches to parallel diagnosis workers, then one
               reducer edits the disposable candidate

Shared external loop:
  Phase E  (code)   evaluate current best on the train split -> failing tasks,
                    masked answer keys + trajectories, fingerprint, suspects
  Meta step (doubao) either runs split D/review/P sessions or one continuous
                    diagnosis-and-edit session; both produce plan.json and one
                    disposable candidate
  Phase V  (code)   inventory check, ma_lint, leak scan, repair eval,
                    dev-set compound gate (paired bootstrap)
  Phase K  (code)   ACCEPT -> candidate becomes the new best (changelog);
                    REJECT -> candidate discarded. Ledger appended either way.
  Phase L  (doubao) reviews the exact package diff against paired outcomes,
                    then distills the validated result into a lesson line.

Memory across rounds = files the driver injects every round (latest change-to-
outcome review + lessons + ledger tail + changelog + regression suspects +
bounded same-task train evidence), never the model's own context.

Usage:
  recuris metaagent run --domain retail --run-id ma1 --rounds 4 --k 4 \
      --splits splits/tau2/retail_from0_v1_k4.json
Pass --base bootstrap instead of --base neutral to have the meta-agent
instantiate M0 itself in round 0.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import functools
import hashlib
import inspect
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from recuris.paths import path_prefix, tau2_root, workspace_root  # noqa: E402

HERE = Path(__file__).resolve().parent
# Where the campaign writes its artefacts. Not the package directory: a
# campaign produces gigabytes and may live on another volume entirely.
ROOT = workspace_root()
TAU2_ROOT = tau2_root(required=False)
# How permission rules must spell the benchmark checkout, given where it is.
TAU2_PREFIX = path_prefix(TAU2_ROOT, ROOT)

from recuris.metaagent import activation_probe  # noqa: E402
from recuris.metaagent import plan_schema as ma_plan_schema  # noqa: E402
from recuris.metaagent import sanitize as ma_sanitize  # noqa: E402
from recuris.metaagent.downstream import (  # noqa: E402
    FORMAL_ALLOWED_OPENAI_PARAMS,
    FORMAL_DOWNSTREAM_MODEL,
    FORMAL_DOWNSTREAM_REASONING,
    FORMAL_DOWNSTREAM_TEMPERATURE,
    OFFICIAL_BENCHMARK_PROTOCOL,
    SUPPORTED_BENCHMARK_PROTOCOLS,
    RecurisDownstream,
    _benchmark_input_manifest,
    _read_env,
)
from recuris.metaagent.gates import held_out_paired_gate  # noqa: E402
from recuris.metaagent.integrity import (  # noqa: E402
    assert_mutation_paths_safe,
)
from recuris.metaagent.integrity import (
    verify as verify_champions,
)
from recuris.metaagent.reachability import (  # noqa: E402
    tau2_checker_capabilities,
    tau2_tool_capabilities,
    tool_aware_entry_kinds,
)

# The downstream evaluation runs in a subprocess so that a wedged simulation
# cannot take the campaign with it. It uses this interpreter, which defaults to
# the one running the driver -- correct whenever recuris and tau2-Bench share a
# virtualenv, which is what the quickstart produces.
PY = os.environ.get("RECURIS_DOWNSTREAM_PY") or sys.executable

def _tree_kill_cmd(pid: int) -> list[str]:
    """Force-kill a subprocess and its children on either platform."""
    if os.name == "nt":
        return ["taskkill", "/F", "/T", "/PID", str(pid)]
    return ["bash", "-c", f"pkill -9 -P {pid} 2>/dev/null; kill -9 {pid} 2>/dev/null; true"]

DEFAULT_WORKER_MODEL = FORMAL_DOWNSTREAM_MODEL
DEFAULT_META_MODEL = "doubao-seed-2-1-pro-260628"
DEFAULT_SIMULATOR_MODEL = FORMAL_DOWNSTREAM_MODEL
# Bounded retries for one parallel-diagnosis worker. Each retry backs
# off first (see run_worker) so a provider rate limit gets a chance to
# reset instead of consuming every attempt inside one exhausted window.
DIAGNOSIS_ATTEMPTS = 3
META_TEMPERATURE = 0.0
REASONING_LEVELS = ("low", "medium", "high")
DRIVER_SCHEMA_VERSION = 15
REPLAY_SCHEMA_VERSION = 1
CAPABILITY_DISCLOSURE_SCHEMA_VERSION = 1
GENERATION_PROFILE_SCHEMA_VERSION = 1
HISTORY_EVIDENCE_ROUNDS = 2
MAX_REPAIR_HISTORY_CELLS = 6

# Diagnosis-visible budgets for terminal transcripts (chars). The sealed
# trial_evidence keeps the full text; these bound only what diagnosis workers
# read, sized so four workers stay under half the shared meta-model TPM.
DIAGNOSIS_FAIL_TRANSCRIPT_BUDGET = 8_000
DIAGNOSIS_PASS_TRANSCRIPT_BUDGET = 4_000
ERROR_LINE_RE = re.compile(
    r"(?i)(error|traceback|exception|fail(ed|ure)?|fatal|no such file"
    r"|not found|command not found|syntax|assert|permission denied"
    r"|non-zero exit)")

MAX_REPAIR_TRANSCRIPT_CHARS = 4000
MAX_ROUND_REVIEW_PROMPT_DIFF_CHARS = 100_000
MAX_ROUND_REVIEW_CHARS = 8_000
# banking_knowledge's toolkit (KB search + account operations) produces a
# 20.4KB disclosure; the old 20KB budget was sized on retail/airline. 28KB
# keeps the bound meaningful while fitting every current domain.
MAX_CAPABILITY_PROMPT_BLOCK_BYTES = 28_000
MAX_GENERATION_PROFILE_VISIBLE_BYTES = 128_000
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_TAU2_DOMAINS = {"retail", "airline", "banking_knowledge"}
META_WORKFLOWS = {"split", "continuous", "hierarchical"}
FORMAL_PROTOCOL_DIR = HERE / "protocols"
CAPABILITY_MANUAL_PATH = FORMAL_PROTOCOL_DIR / "capability_manual.md"
TAU2_CAPABILITY_EXTENSION_PATH = FORMAL_PROTOCOL_DIR / "tau2_capabilities.md"
SUPPORTED_BENCHMARKS = {"tau2"}

# Dense-reward statistics.  A benchmark whose verifier scores in [0,1] instead
# of {0,1} opts in by exposing `dense_rewards = True` on the downstream shim
# (never by name matching); every routine below reduces to today's binary
# behaviour when the flag is absent or the matrices happen to be 0/1.
DENSE_BOOTSTRAP_ITERS = 3000
DENSE_BOOTSTRAP_SEED = 0
# Fewest task clusters a dense p-value may be built from.  Resampling is over
# TASKS, so a run has as many independent draws as it has tasks however many
# trials each one carries, and below a handful of them the bootstrap has no
# resolution left to spend.  Measured false-accept rate of p < 0.05 under a
# true null (both arms the same distribution, k=2, per-trial sigma=0.15, 2000
# draws) for the two-stage estimator below: 26.5% at n=1, 9.4% at n=2, 7.4% at
# n=3, 5.7% at n=4, 4.9% at n=5, 4.1% at n=8.  Five is the smallest count whose
# rate is back at the nominal 5%, so at four or fewer the estimator declines to
# certify (returns 1.0) instead of reporting a number it cannot support.
# repair_tasks is routinely 1-3 tasks, which is exactly the range this covers.
# Calibrated at k >= 2 -- the driver runs k=3/4/8 -- and it holds with margin
# there (4.5% at n=5 k=3, 1.8% at n=14 k=3, 1.4% at n=14 k=8).  At k=1 there is
# no second trial to resample inside a task, the estimator degenerates to the
# task-only bootstrap it replaces, and five clusters are NOT enough (measured
# 9.3% at n=5, 7.5% at n=14): a dense k=1 run cannot certify at these dev sizes.
DENSE_BOOTSTRAP_MIN_CLUSTERS = 5
# Trials per task the cluster floor above was calibrated at.  The measurements
# in that comment were taken at k >= 2; at k=1 there is no second trial to
# resample inside a task and the same five clusters false-accept at 9.3%.  The
# estimator does not police this (it only counts clusters), so the power gate
# reports it before a dense campaign spends rounds on it.
DENSE_BOOTSTRAP_MIN_TRIALS = 2
# Reference design points for the dense power gate's simulated MDE.  The dense
# gate has no closed-form minimal detectable effect the way the cell-level sign
# test does -- it is a two-stage bootstrap of a mean, so its resolution depends
# on the per-trial noise as well as on the design.  The gate therefore
# SIMULATES: synthesise paired per-trial differences at a candidate effect,
# run the real estimator on them, and report the smallest effect it certifies
# at `DENSE_POWER_SIM_POWER` of the replicates.  The noise level is an
# assumption, not a measurement of this run, which is why the simulated number
# is reported and warned on but never enforced; only the cluster arithmetic is.
# 0.15 is the per-trial sigma the cluster floor above was calibrated at.
DENSE_POWER_SIM_TRIAL_SD = 0.15
DENSE_POWER_SIM_ITERS = 300
DENSE_POWER_SIM_REPLICATES = 15
DENSE_POWER_SIM_POWER = 0.8
DENSE_POWER_SIM_SEED = 20260817
DENSE_POWER_SIM_GRID = (0.01, 0.02, 0.03, 0.05, 0.07,
                        0.10, 0.15, 0.20, 0.30, 0.50)
# Per-task mean movement a dense benchmark must exceed before it counts as an
# improvement or a regression.  Under a partial-credit verifier the same trial
# scores 0.97 one run and 0.95 the next; without a floor every such wobble
# would spend the regression cap and every noise cell would read as harm.
# A shim may override it with `dense_material_delta`.
DENSE_MATERIAL_DELTA = 0.05
# "This cell changed at all" -- float comparison only, not a materiality test.
DENSE_CHANGE_EPS = 1e-9


def _task_sort_key(value) -> tuple:
    """Stable task ordering: numeric Tau2 ids numerically, then names lexically."""
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def _seed_paired_flips(base_matrix: dict, cand_matrix: dict, tasks=None) -> tuple:
    """Discordant seed-paired cells: (fail->pass, pass->fail, paired_total).

    Rows are seed-aligned across arms (the driver enforces paired seed maps),
    so index i in both rows is the same environment seed.
    """
    names = tasks if tasks is not None else (set(base_matrix) & set(cand_matrix))
    b = c = n = 0
    for task in names:
        base_row = base_matrix.get(task) or []
        cand_row = cand_matrix.get(task) or []
        for i in range(min(len(base_row), len(cand_row))):
            n += 1
            if base_row[i] < cand_row[i]:
                b += 1
            elif base_row[i] > cand_row[i]:
                c += 1
    return b, c, n


def _mcnemar_one_sided(b: int, c: int) -> float:
    """P(X >= b) for X ~ Binom(b+c, 1/2): chance of this many favourable flips
    among the discordant cells if the package changed nothing."""
    d = b + c
    if d == 0:
        return 1.0
    return sum(math.comb(d, i) for i in range(b, d + 1)) / 2 ** d


def _evidence_score(value) -> int | float:
    """Cell score for the repair-evidence record.

    Dense rewards must survive verbatim -- int() would report a 0.75 repair as
    a 0 baseline and a 0 candidate, i.e. as no change at all.  Whole values
    stay ints so binary rounds keep byte-identical evidence files.
    """
    number = float(value)
    return int(number) if number.is_integer() else round(number, 6)


def _paired_trial_diffs(base_matrix: dict, cand_matrix: dict, tasks=None) -> list:
    """Paired per-trial score differences, grouped by task: [[d, ...], ...].

    Rows are seed-aligned across arms (the driver enforces paired seed maps),
    so index i is the same environment seed in both and only the paired prefix
    is comparable.  The two nesting levels are the two the estimator has to
    resample: the task is the cluster, the trial is the observation inside it.
    """
    names = sorted(
        (set(base_matrix) & set(cand_matrix)) if tasks is None
        else (set(str(t) for t in tasks) & set(base_matrix) & set(cand_matrix)),
        key=_task_sort_key,
    )
    clusters = []
    for task in names:
        base_row = base_matrix.get(task) or []
        cand_row = cand_matrix.get(task) or []
        paired = min(len(base_row), len(cand_row))
        if not paired:
            continue
        clusters.append([cand_row[i] - base_row[i] for i in range(paired)])
    return clusters


def _paired_mean_bootstrap_p(base_matrix: dict, cand_matrix: dict,
                             direction: str = "positive",
                             iters: int = DENSE_BOOTSTRAP_ITERS,
                             seed: int = DENSE_BOOTSTRAP_SEED,
                             *, tasks=None, harm_margin: float = 0.0) -> float:
    """One-sided p for the paired mean score difference, clustered by task.

    The dense-reward analogue of _mcnemar_one_sided: a cell is a score in
    [0,1], not a flip, so discordant-cell counting throws away the size of
    every change.  `direction` "positive" tests candidate > baseline,
    "negative" tests candidate < baseline (the harm veto).  Deterministic in
    `seed`.

    Two-stage (hierarchical) resampling: a bootstrap draw picks tasks with
    replacement AND, inside each task it picked, picks that task's trials with
    replacement.  Resampling tasks alone treats each task mean as if it were
    measured exactly, which at one task leaves the centred sample [0.0] -- no
    variance at all, so every positive observation came back at the floor
    1/(iters+1) and a true null false-accepted about half the time.  Trials of
    one task do share its environment, so they are not independent draws of the
    package effect, but they are not a point either.

    The null is imposed by centring at the observed statistic, so the reported
    number is P(mean difference this favourable | the package changed nothing
    beyond `harm_margin`).  Below DENSE_BOOTSTRAP_MIN_CLUSTERS tasks the design
    cannot support a significance claim and this returns 1.0 rather than a
    small number the data has not earned.
    """
    if direction not in ("positive", "negative"):
        raise ValueError(f"unsupported bootstrap direction {direction!r}")
    if harm_margin and direction != "negative":
        raise ValueError("harm_margin shifts the harm null only; an improvement "
                         "claim is never discounted")
    clusters = _paired_trial_diffs(base_matrix, cand_matrix, tasks)
    if direction == "negative":
        # The harm hypothesis is "the mean dropped by more than harm_margin",
        # not "the mean dropped at all": under a partial-credit verifier the
        # same finished task scores 0.97 one trial and 0.95 the next, and a
        # veto that fires on that wobble fires on noise.  Shifting the null is
        # the materiality rule the counters already apply, put where the
        # estimate is made.  The positive direction stays unshifted.
        clusters = [[-value - harm_margin for value in row] for row in clusters]
    if len(clusters) < DENSE_BOOTSTRAP_MIN_CLUSTERS:
        return 1.0
    observed = statistics.mean([statistics.mean(row) for row in clusters])
    if observed <= 0:
        return 1.0
    centred = [[value - observed for value in row] for row in clusters]
    rng = random.Random(seed)
    size = len(centred)
    hits = 0
    for _ in range(iters):
        total = 0.0
        for _ in range(size):
            row = centred[rng.randrange(size)]
            width = len(row)
            inner = 0.0
            for _ in range(width):
                inner += row[rng.randrange(width)]
            total += inner / width
        if total / size >= observed:
            hits += 1
    # +1 in both terms: a resampling test never certifies p == 0.
    return (hits + 1) / (iters + 1)


def _dense_power_at(effect: float, tasks: int, k: int, *,
                    trial_sd: float = DENSE_POWER_SIM_TRIAL_SD,
                    replicates: int = DENSE_POWER_SIM_REPLICATES,
                    iters: int = DENSE_POWER_SIM_ITERS,
                    seed: int = DENSE_POWER_SIM_SEED) -> float:
    """Fraction of synthetic `tasks` x `k` campaigns the dense gate certifies.

    The synthetic candidate beats the synthetic baseline by `effect` on every
    trial plus Gaussian per-trial noise of sd `trial_sd`; the p-value is the
    REAL estimator, so whatever the estimator refuses to certify (too few
    clusters, most obviously) shows up here as power 0.  Deterministic in
    `seed`.  Reduced `iters` relative to the campaign's own bootstrap: this
    only has to resolve a 0.05 threshold about `replicates` times, not report
    a p-value.
    """
    hits = 0
    for replicate in range(replicates):
        # str seed -> sha512 inside random.Random, i.e. stable across processes
        # (unlike hash() of a tuple, which is salted for str members).
        rng = random.Random(f"{seed}|{tasks}|{k}|{effect!r}|{replicate}")
        base = {f"t{i:03d}": [0.0] * k for i in range(tasks)}
        cand = {
            f"t{i:03d}": [effect + rng.gauss(0.0, trial_sd) for _ in range(k)]
            for i in range(tasks)
        }
        if _paired_mean_bootstrap_p(base, cand, "positive",
                                    iters=iters, seed=seed) < 0.05:
            hits += 1
    return hits / replicates


def _dense_mde_pp(tasks: int, k: int, *,
                  trial_sd: float = DENSE_POWER_SIM_TRIAL_SD,
                  power: float = DENSE_POWER_SIM_POWER,
                  grid=DENSE_POWER_SIM_GRID) -> float | None:
    """Smallest gridded effect the dense gate certifies at `power`, in pp.

    None when even the largest gridded effect fails -- which is the honest
    answer below DENSE_BOOTSTRAP_MIN_CLUSTERS tasks, where the estimator
    returns 1.0 for every input and no effect of any size is detectable.

    Scans the grid upward and stops at the first hit, so a well-powered design
    costs a few bootstraps and only a hopeless one pays for the whole grid.
    Monte-Carlo, so read it to about one grid step; it is a design sanity
    check, not a published number.
    """
    for effect in grid:
        if _dense_power_at(effect, tasks, k, trial_sd=trial_sd) >= power:
            return round(100.0 * effect, 1)
    return None


def _clip_region(text: str, head: int, tail: int, marker: str) -> str:
    if len(text) <= head + tail + len(marker):
        return text
    return text[:head] + marker + (text[-tail:] if tail else "")


def _condense_transcript(text: str, budget: int) -> str:
    """Compress a terminal transcript for diagnosis without losing errors.

    Step-block aware (splits on the shim's own join seam), keeps the task
    instruction, clips heredoc echo-back and package-manager noise, harvests
    every error-looking line a clip would drop, and gives the final agent step
    and its observation a triple, tail-biased budget -- the verifier scored
    that state.
    """
    marker = "\n...[clipped for diagnosis; sealed evidence keeps the full text]...\n"
    steps = re.split(r"\n\n(?=\[)", text)
    out_steps: list[str] = []
    last_agent = max((i for i, s in enumerate(steps) if s.startswith("[agent")),
                     default=-1)
    for index, step in enumerate(steps):
        harvested: list[str] = []
        final = index >= last_agent >= 0
        if index == 0:
            kept = _clip_region(step, 150, 1_800,
                                "\n...[Terminus-2 prompt boilerplate omitted]...\n")
        else:
            lines_out = []
            for line in step.splitlines():
                if line.startswith("  -> "):
                    lines_out.append(line[:600])
                elif line.startswith("  <- "):
                    limit = 3_000 if final else 1_200
                    if len(line) > limit:
                        cut = line[:200] + " ...[clip]... " + line[-(limit - 220):]
                        harvested += [x[:200] for x in [line]
                                      if ERROR_LINE_RE.search(line[200:-(limit - 220)] or "")]
                        lines_out.append(cut)
                    else:
                        lines_out.append(line)
                else:
                    lines_out.append(line if len(line) <= 1_000 else
                                     _clip_region(line, 500, 400, " ...[clip]... "))
            kept = "\n".join(lines_out)
            limit = 3_000 if final else 1_400
            if len(kept) > limit:
                removed = kept[400:-(limit - 500)]
                harvested += [ln.strip()[:200]
                              for ln in removed.splitlines()
                              if ERROR_LINE_RE.search(ln)][:25]
                kept = _clip_region(kept, 400, limit - 500, marker)
        if harvested:
            kept += "\n  [errors preserved] " + " | ".join(dict.fromkeys(harvested))
        out_steps.append(kept)
    result = "\n\n".join(out_steps)
    if len(result) > budget:
        result = _clip_region(result, budget // 4, (budget * 3) // 4, marker)
    return result


def _perfect_stratum_damage(base_matrix: dict, cand_matrix: dict,
                            material: float = 0.0) -> dict:
    """Cells lost on tasks the base arm passed on every trial, with its null.

    Diagnostic only. Selecting tasks for being at the ceiling and then reading an
    independent redraw is biased downward: an unchanged package scores -5 to -6pp
    here, because a task whose true rate is 0.9 still reaches k/k often enough to
    be selected. `null_rate_pp` is that same bias measured on the base arm alone,
    by selecting on half its seeds and reading the other half, and any reading of
    `lost_rate_pp` has to be taken against it. Real harm is established by the
    mechanism-contact test in the regression evidence, which requires a mechanism
    to have fired on the task that regressed; this number establishes nothing on
    its own and carries no reject authority.

    `material` widens "at the ceiling" for dense rewards, where a partial-credit
    verifier scores the same finished task 0.97 one trial and 0.95 the next: at
    material=0 that wobble reads as a whole lost perfect cell.  At the 0.0
    default the tests are `>= 1.0` / `< 1.0` exactly as before.
    """
    ceiling = 1.0 - material
    perfect = [task for task, row in base_matrix.items()
               if row and all(value >= ceiling for value in row)]
    lost_cells = 0
    lost_tasks = []
    for task in perfect:
        base_row = base_matrix.get(task) or []
        cand_row = cand_matrix.get(task) or []
        paired = min(len(base_row), len(cand_row))
        lost = sum(1 for i in range(paired)
                   if base_row[i] >= ceiling and cand_row[i] < ceiling)
        if lost:
            lost_cells += lost
            lost_tasks.append(task)
    graded = sum(min(len(base_matrix.get(t) or []), len(cand_matrix.get(t) or []))
                 for t in perfect)
    return {
        "perfect_tasks": len(perfect),
        "lost_cells": lost_cells,
        "lost_tasks": sorted(lost_tasks, key=_task_sort_key),
        "lost_rate_pp": round(-100.0 * lost_cells / graded, 2) if graded else 0.0,
        "null_rate_pp": _perfect_stratum_null(base_matrix, material),
        "reject_authority": False,
    }


def _perfect_stratum_null(base_matrix: dict, material: float = 0.0) -> float:
    """What the estimator reports for a package that changed nothing.

    Select on one half of the base arm's seeds, read the other half of the same
    arm. Nothing differs between the halves, so every cell below 1.0 is the
    selection bias and nothing else.
    """
    widths = {len(row) for row in base_matrix.values() if row}
    if not widths or min(widths) < 2:
        return 0.0
    ceiling = 1.0 - material
    k = min(widths)
    half = k // 2
    lost = graded = 0
    for row in base_matrix.values():
        if len(row) < k:
            continue
        for first in (True, False):
            sel = row[:half] if first else row[half:k]
            read = row[half:k] if first else row[:half]
            if all(value >= ceiling for value in sel):
                graded += len(read)
                lost += sum(1 for value in read if value < ceiling)
    return round(-100.0 * lost / graded, 2) if graded else 0.0


class BenchmarkProfile:
    """Every benchmark-specific value the driver injects into prompts,
    provenance, permissions, and reachability.

    The driver is written against this indirection rather than against tau2
    directly, so that adding a benchmark is a matter of supplying a profile
    plus a downstream shim. This release ships the tau2-Bench profile."""

    def __init__(self, benchmark: str):
        if benchmark not in SUPPORTED_BENCHMARKS:
            raise ValueError(f"unsupported benchmark {benchmark!r}")
        self.benchmark = benchmark
        self.package_prefix = "tau2"
        self.commit_key = "tau2_git_commit"
        self.dirty_key = "tau2_git_dirty"
        self.skill_agent = "recuris_agent"
        self.bare_agent = "llm_agent"
        self.capability_extension_path = TAU2_CAPABILITY_EXTENSION_PATH
        self.tool_capabilities = tau2_tool_capabilities
        self.checker_capabilities = tau2_checker_capabilities
        self.runtime_root = None

    def policy_doc_path(self, domain: str) -> Path:
        base = TAU2_ROOT / "data" / "tau2" / "domains" / domain
        policy = base / "policy.md"
        if policy.is_file():
            return policy
        # Knowledge domains carry no policy.md; their agent policy is the
        # templated prompt the runtime itself renders.
        return base / "prompts" / "all_tools.md"

    def policy_doc_display(self, domain: str) -> str:
        return path_prefix(self.policy_doc_path(domain), ROOT)

    def additional_directories(self) -> list[str]:
        return [TAU2_PREFIX]
# The reference launcher is a shell script, so Windows hosts need a POSIX
# shell. Set RECURIS_BASH to point at one; the fallbacks cover a default
# Git for Windows install and any bash already on PATH.
GIT_BASH_CANDIDATES = [
    path for path in (
        os.environ.get("RECURIS_BASH"),
        r"C:\Program Files\Git\bin\bash.exe",
        "bash",
    ) if path
]
# Claude Code 2.1.217 exposes file creation through Edit(old_string="").
# A separate Write tool is no longer present in the init tool surface.
D_TOOLS = "Read Edit"
D_WRITE_TOOLS = "Edit"
P_TOOLS = "Read Edit"
HIERARCHICAL_REDUCER_TOOLS = "Read Edit Glob Grep"
QUALIFICATION_TOOLS = "Read Edit"

PROFILE_DIAGNOSIS_PROMPT_TEMPLATE = """\
You are the Meta-Agent for the Recuris framework (locked generation profile,
diagnosis phase, round {round}).

{capability_block}

This is a sealed capability comparison. The only task evidence, policy, public
runtime contract, and diagnosis protocol you may use are the locked visible
files listed below. Hidden source evidence and successful trajectories are not
available. Do not seek framework source, tests, other run artifacts, the full
domain policy, or historical packages.

Read, in order:
1. `{evidence_path}`
2. `{policy_path}`
3. `{capability_path}`
4. `{tool_capabilities_path}`
5. `{plan_protocol_path}`
6. `{base_manifest_path}` -- the complete visible projection of current M0

Your only output is `{plan_path}`. Write one plan that covers every task
required by the locked plan scope. Use only these opaque case aliases in
`evidence_tasks`: {required_cases}. Never write or infer an underlying benchmark
task id. The deterministic validator will enforce
component/action legality, carrier reachability, forced ownership/opportunity,
and exact required-task coverage. If one repair needs changes owned by
different components, use companion clusters; never place an action under the
wrong component. Derive any repair only from the visible failure skeleton and
visible policy. Do not invent or reconstruct a hidden successful trajectory.

Call Edit exactly once to write the complete `{plan_path}`, then end the
session immediately. Do not make follow-up edits; deterministic validator
feedback, if needed, is handled by a separate bounded retry session."""

PROFILE_PATCH_PROMPT_TEMPLATE = """\
You are the Meta-Agent for the Recuris framework (locked generation profile,
patch phase, round {round}).

{capability_block}

This is a sealed capability comparison. Implement the validated plan exactly.
Use only the locked visible inputs listed below; do not seek framework source,
tests, other run artifacts, the full domain policy, historical packages, or
hidden task/success evidence.

Read, in order:
1. `{plan_path}`
2. `{patch_protocol_path}`
3. `{evidence_path}`
4. `{policy_path}`
5. `{capability_path}`
6. `{tool_capabilities_path}`

The disposable candidate package is `{candidate_path}`. Edit only inside that
directory. Any validated remove_card action has already been applied by the
driver. Implement every remaining fix and no unplanned change. Card content
must be derived from the visible policy and failure skeleton, use placeholders
instead of real identifiers, and must not invent a hidden successful sequence.
Then end the session."""


class MetaAgentSessionFailure(RuntimeError):
    """All bounded Meta-Agent sessions failed before a valid phase artifact."""

    def __init__(self, phase: str, sessions: list[dict]):
        self.phase = phase
        self.sessions = sessions
        classes = [str(session.get("failure_class") or "unknown")
                   for session in sessions]
        upstream_classes = {"upstream-model-mismatch", "upstream-api-error"}
        self.outcome = (
            "upstream-model-failure"
            if classes and all(value in upstream_classes for value in classes)
            else "harness-failure"
        )
        super().__init__(
            f"Meta-Agent phase {phase} produced no valid session "
            f"(failure classes: {classes})"
        )


class MetaTreatmentError(RuntimeError):
    """The provider did not execute the frozen Meta-Agent treatment."""


class ProfileArmIneligible(RuntimeError):
    """A locked-profile safety boundary made this arm ineligible."""

    REASON_CODE = "locked-profile-safety-violation"

    def __init__(self, phase: str):
        if phase not in {"D", "P"}:
            raise ValueError("profile ineligibility phase must be D or P")
        self.phase = phase
        self.reason_code = self.REASON_CODE
        super().__init__(
            f"locked profile arm is ineligible after a phase {phase} "
            "safety violation; no retry is allowed"
        )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Driver:
    def __init__(self, a):
        self.a = a
        self._cc_lock = threading.RLock()
        self._cc_sessions_by_name: dict[str, dict] = {}
        self.domain = a.domain
        self.benchmark = str(getattr(a, "benchmark", "tau2"))
        self.profile = BenchmarkProfile(self.benchmark)
        self.tool_capabilities = self.profile.tool_capabilities
        self.checker_capabilities = self.profile.checker_capabilities
        self.arm = getattr(a, "arm", "autonomous")
        self.generate_only = bool(getattr(a, "generate_only", False))
        self.meta_workflow = str(getattr(a, "meta_workflow", "split"))
        if self.meta_workflow not in META_WORKFLOWS:
            raise ValueError(
                f"meta_workflow must be one of {sorted(META_WORKFLOWS)}"
            )
        self.recursive_review = bool(getattr(a, "recursive_review", False))
        if self.meta_workflow in {"continuous", "hierarchical"}:
            # These workflows already reconcile diagnosis before editing.
            self.recursive_review = False
        self.diagnosis_workers = int(getattr(a, "diagnosis_workers", 8))
        if not 1 <= self.diagnosis_workers <= 16:
            raise ValueError("diagnosis_workers must be between 1 and 16")
        a.meta_workflow = self.meta_workflow
        a.recursive_review = self.recursive_review
        a.diagnosis_workers = self.diagnosis_workers
        a.generate_only = self.generate_only
        self.replay_source_run = getattr(a, "replay_source_run", None)
        self.replay_source_round = getattr(a, "replay_source_round", 1)
        self.generation_profile_spec_path = None
        self.generation_profile_spec_bytes = None
        self.generation_profile_spec = None
        self.generation_profile_spec_sha256 = None
        self.generation_profile_canonical_sha256 = None
        self.generation_profile_comparison_arm = None
        if self.arm not in {"autonomous", "seeded-control"}:
            raise ValueError("arm must be 'autonomous' or 'seeded-control'")
        if self.arm == "autonomous" and a.base not in {"neutral", "bootstrap"}:
            raise ValueError(
                "autonomous arm may start only from neutral/bootstrap; "
                "historical or champion package content is forbidden"
            )
        if self.arm == "seeded-control" and a.base in {"neutral", "bootstrap"}:
            raise ValueError("seeded-control arm requires an explicit package path")
        if getattr(self, "replay_source_run", None):
            if not RUN_ID_RE.fullmatch(str(self.replay_source_run)):
                raise ValueError("replay source run-id is not a safe path component")
            if self.replay_source_run == a.run_id:
                raise ValueError("replay source run must differ from the destination run")
            if self.arm != "autonomous" or a.base != "neutral":
                raise ValueError(
                    "frozen-evidence replay is an autonomous neutral-base recovery arm"
                )
            if a.qualify_only:
                raise ValueError("--replay-source-run is incompatible with --qualify-only")
            # Version 1 deliberately supports the sealed failed discovery round
            # only.  Later-round replay needs a separate pre-round state journal.
            if self.replay_source_round != 1 or a.rounds != 1:
                raise ValueError(
                    "replay schema v1 requires --replay-source-round 1 and --rounds 1"
                )
        if self.generate_only:
            if not self.replay_source_run:
                raise ValueError("--generate-only requires --replay-source-run")
            if a.qualify_only:
                raise ValueError("--generate-only is incompatible with --qualify-only")
            if a.max_sims != 0:
                raise ValueError("--generate-only requires --max-sims 0")
        if self.benchmark == "tau2":
            if self.domain not in SUPPORTED_TAU2_DOMAINS:
                raise ValueError(
                    f"this Tau2 driver supports only {sorted(SUPPORTED_TAU2_DOMAINS)}; "
                    "use a DomainAdapter for other runtimes"
                )
        elif not RUN_ID_RE.fullmatch(self.domain):
            raise ValueError(
                "tb21 taskset name must be a safe identifier; TB21Downstream "
                "validates it against its taskset manifest"
            )
        if not RUN_ID_RE.fullmatch(a.run_id):
            raise ValueError("run-id must contain only letters, digits, dot, underscore, or dash")
        if not (1 <= a.proxy_port <= 65535):
            raise ValueError("proxy-port must be between 1 and 65535")
        # Open-worker campaigns evolve a memory *for* a model served elsewhere,
        # so the worker is the one thing that must be free to differ. The user
        # simulator stays pinned either way: it is not what is being optimised,
        # and unfreezing it would make rounds incomparable to each other as
        # well as to every other arm. RecurisDownstream re-checks this from its
        # own constructor -- the duplication is deliberate, because this check
        # runs before the downstream exists and that one guards direct callers.
        self.open_worker = bool(getattr(a, "open_worker", False))
        frozen = {
            "simulator_model": DEFAULT_SIMULATOR_MODEL,
            "simulator_reasoning": FORMAL_DOWNSTREAM_REASONING,
        }
        if not self.open_worker:
            frozen["worker_model"] = DEFAULT_WORKER_MODEL
            frozen["worker_reasoning"] = FORMAL_DOWNSTREAM_REASONING
        for field, expected in frozen.items():
            if getattr(a, field) != expected:
                raise ValueError(f"{field} is frozen at {expected!r} for formal Meta-Agent runs")
        if self.open_worker and not getattr(a, "worker_llm_args", None):
            raise ValueError(
                "--open-worker requires --worker-llm-args JSON naming the "
                "endpoint (api_base, temperature, timeout, num_retries)"
            )
        self.benchmark_protocol = (
            getattr(a, "benchmark_protocol", None) or OFFICIAL_BENCHMARK_PROTOCOL
        )
        if self.benchmark_protocol not in SUPPORTED_BENCHMARK_PROTOCOLS:
            raise ValueError(
                f"benchmark_protocol must be one of "
                f"{sorted(SUPPORTED_BENCHMARK_PROTOCOLS)}"
            )
        a.benchmark_protocol = self.benchmark_protocol

        profile_spec_arg = getattr(a, "generation_profile_spec", None)
        profile_sha_arg = getattr(a, "generation_profile_sha256", None)
        if bool(profile_spec_arg) != bool(profile_sha_arg):
            raise ValueError(
                "--generation-profile-spec and --generation-profile-sha256 "
                "must be provided together"
            )
        if profile_spec_arg:
            if not self.generate_only:
                raise ValueError(
                    "--generation-profile-spec requires --generate-only"
                )
            if self.meta_workflow != "split":
                raise ValueError(
                    "locked generation profiles currently require "
                    "--meta-workflow split"
                )
            loaded_profile = self._load_generation_profile_spec(
                profile_spec_arg, profile_sha_arg,
            )
            self.generation_profile_spec_path = loaded_profile["path"]
            self.generation_profile_spec_bytes = loaded_profile["raw_bytes"]
            self.generation_profile_spec = loaded_profile["spec"]
            self.generation_profile_spec_sha256 = loaded_profile["raw_sha256"]
            self.generation_profile_canonical_sha256 = loaded_profile[
                "canonical_sha256"
            ]
            self._validate_generation_profile_treatment()
            # Locked comparison profiles intentionally expose only their sealed
            # inputs. The general recursive review is for autonomous runs.
            self.recursive_review = False
            a.recursive_review = False

        # Verify every trust anchor before the first mkdir. A run path that is a
        # symlink/junction could otherwise redirect a harmless-looking write.
        verify_champions()
        self.ma_runs_root = ROOT / "ma_runs"
        assert_mutation_paths_safe([self.ma_runs_root])
        self._assert_no_linklike(self.ma_runs_root, allow_missing=True)
        self.ma_runs_root.mkdir(parents=True, exist_ok=True)
        self.replay_source_dir = None
        if self.replay_source_run:
            replay_source_dir = self.ma_runs_root / str(self.replay_source_run)
            self._assert_no_linklike(replay_source_dir, allow_missing=False)
            if (not replay_source_dir.is_dir() or
                    replay_source_dir.resolve().parent != self.ma_runs_root.resolve()):
                raise ValueError(f"unsafe or missing replay source run: {replay_source_dir}")
            self.replay_source_dir = replay_source_dir
        self.run_dir = self.ma_runs_root / a.run_id
        self._assert_no_linklike(self.run_dir, allow_missing=True)
        assert_mutation_paths_safe([self.run_dir])
        self.run_dir.mkdir(parents=False, exist_ok=True)
        if self.run_dir.resolve().parent != self.ma_runs_root.resolve():
            raise RuntimeError(f"unsafe run directory resolution: {self.run_dir}")
        self._run_lock = None
        self._acquire_run_lock()

        # CC writes only to explicitly scoped artifacts under this run. The
        # referee, immutable versions, and historical champions stay read-only.
        self.package_root = self.run_dir / "packages"
        self.versions_dir = self.package_root / "versions"
        self._assert_no_linklike(self.package_root, allow_missing=True)
        self._assert_no_linklike(self.versions_dir, allow_missing=True)
        try:
            self.versions_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self._release_run_lock()
            raise
        self.cand_dir = self.package_root / "candidate_r0"
        self.state_path = self.run_dir / "state.json"
        self.ledger_path = self.run_dir / "ledger.jsonl"
        self.ledger_events_dir = self.run_dir / "ledger_events"
        self.log_path = self.run_dir / "driver.log"
        self.lessons_path = self.run_dir / "lessons.md"
        self.provenance_path = self.run_dir / "provenance.json"
        self.proxy_proc = None
        self.proxy_owned = False
        try:
            assert_mutation_paths_safe([
                self.run_dir, self.package_root, self.versions_dir, self.ledger_events_dir,
            ])
            self._assert_no_linklike(self.package_root, allow_missing=False)
            self._assert_no_linklike(self.versions_dir, allow_missing=False)
            self._assert_no_linklike(self.ledger_events_dir, allow_missing=True)
            self.ledger_events_dir.mkdir(parents=False, exist_ok=True)
            # Harness qualification exercises only scoped file tools; it must
            # not inspect benchmark answer data or populate shared gold caches,
            # so it keeps its historical empty-set literal: no masking or
            # scanning site is reachable during qualification.
            if a.qualify_only:
                self.gold = set()
            else:
                self.gold = ma_sanitize.harvest_gold_ids(self.domain)
            if a.splits:
                self.splits_path = Path(a.splits).resolve()
                splits = json.loads(self.splits_path.read_text(encoding="utf-8-sig"))
                if not isinstance(splits, dict):
                    raise ValueError("split file must contain a JSON object")
                self.split_metadata = splits
                self.train_ids = self._split_list(
                    splits, "train", benchmark=self.benchmark)
                self.dev_ids = self._split_list(
                    splits, "dev", benchmark=self.benchmark)
                self.test_ids = self._split_list(
                    splits, "test", required=False, benchmark=self.benchmark)
            else:
                self.splits_path = None
                self.split_metadata = {}
                self.train_ids, self.dev_ids, self.test_ids = [], [], []
            self._validate_splits(require_nonempty=not a.qualify_only)
            self._validate_split_lineage()
            if a.qualify_only:
                self.ds = None
            else:
                self.ds = RecurisDownstream(
                    domain=self.domain, max_concurrency=a.max_concurrency,
                    eval_timeout=a.eval_timeout,
                    worker_model=a.worker_model,
                    worker_reasoning=a.worker_reasoning,
                    simulator_model=a.simulator_model,
                    simulator_reasoning=a.simulator_reasoning,
                    worker_llm_args=getattr(a, "worker_llm_args", None),
                    open_worker=self.open_worker,
                    artifact_namespace=a.run_id,
                    benchmark_protocol=self.benchmark_protocol,
                    resume_attempts=getattr(a, "eval_resume_attempts", 1),
                    retrieval_config=getattr(a, "tau2_retrieval_config", None))
            self.bash = next((b for b in GIT_BASH_CANDIDATES
                              if b == "bash" or Path(b).exists()), "bash")
            # Sealed evaluation artifacts from a prior run may be reused when
            # every identity field (tag, tasks, k, package tree hash, agent,
            # models, protocol) matches; the deterministic neutral M0 makes
            # round-0 and round-1 baselines identical across from-zero runs.
            self._foreign_artifacts: list[dict] = []
            source_run = str(getattr(a, "reuse_artifacts_run", "") or "")
            if source_run and self.ds is not None:
                if not RUN_ID_RE.fullmatch(source_run):
                    raise ValueError(
                        "reuse-artifacts run-id is not a safe path component"
                    )
                if source_run == a.run_id:
                    raise ValueError(
                        "reuse-artifacts source must differ from this run"
                    )
                source_prov = ROOT / "ma_runs" / source_run / "provenance.json"
                if not source_prov.is_file():
                    raise ValueError(
                        f"reuse-artifacts source has no provenance: {source_prov}"
                    )
                for record in (json.loads(
                        source_prov.read_text(encoding="utf-8")
                        ).get("downstream_artifacts") or []):
                    if record.get("status") != "success":
                        continue
                    item = copy.deepcopy(record)
                    item["reused_from_run"] = source_run
                    self._foreign_artifacts.append(item)
            self.state = (json.loads(self.state_path.read_text(encoding="utf-8"))
                          if self.state_path.exists() else self._default_state())
            # Configuration and runtime-source compatibility is checked before
            # recovery mutates any state or commit journal.
            self._init_provenance()
            self._restore_downstream_artifacts()
            self._recover_ledger_aggregate()
            self._reconcile_commits()
            self._reconcile_round_finalizations()
            self._validate_state()
        except Exception:
            self._release_run_lock()
            raise

    @staticmethod
    def _is_linklike(path: Path) -> bool:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())

    @classmethod
    def _assert_no_linklike(cls, path: Path, *, allow_missing: bool) -> None:
        path = Path(path)
        if not path.exists():
            if allow_missing:
                return
            raise RuntimeError(f"required path is missing: {path}")
        if cls._is_linklike(path):
            raise RuntimeError(f"refusing symlink/junction path: {path}")

    @staticmethod
    def _load_generation_profile_spec(
            raw_path: str | Path, expected_sha256: str) -> dict:
        """Read and strictly validate a locked generation-profile JSON file."""
        expected = str(expected_sha256 or "").lower()
        if not SHA256_RE.fullmatch(expected):
            raise ValueError(
                "--generation-profile-sha256 must be exactly 64 hex characters"
            )
        path = Path(raw_path)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file() or Driver._is_linklike(path):
            raise ValueError(
                f"generation profile spec is missing or link-like: {path}"
            )
        path = path.resolve()
        raw_bytes = path.read_bytes()
        actual = hashlib.sha256(raw_bytes).hexdigest()
        if actual != expected:
            raise ValueError(
                "generation profile raw SHA-256 mismatch: "
                f"expected {expected}, got {actual}"
            )
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("generation profile spec must be strict UTF-8") from exc
        if "\r" in text:
            raise ValueError(
                "generation profile spec must use canonical LF line endings"
            )

        def reject_constant(value: str):
            raise ValueError(
                f"generation profile spec contains non-finite JSON value {value}"
            )

        try:
            spec = json.loads(text, parse_constant=reject_constant)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"generation profile spec is not strict JSON: {exc}"
            ) from exc
        if not isinstance(spec, dict):
            raise ValueError("generation profile spec root must be an object")

        def exact_keys(value: object, keys: set[str], label: str) -> dict:
            if not isinstance(value, dict):
                raise ValueError(f"{label} must be an object")
            actual_keys = set(value)
            if actual_keys != keys:
                raise ValueError(
                    f"{label} keys must be exactly {sorted(keys)}; "
                    f"got {sorted(actual_keys)}"
                )
            return value

        required_profile_keys = {
            "schema_version", "profile_id", "domain", "replay", "source",
            "visible_inputs", "tool_allowlist", "attempt_timeouts",
            "plan_scope", "information_boundary", "alias_to_task",
            "plan_contract", "activation_queries",
        }
        actual_profile_keys = set(spec)
        if frozenset(actual_profile_keys) not in {
                frozenset(required_profile_keys),
                frozenset(required_profile_keys | {"comparison_lock"})}:
            raise ValueError(
                "generation profile keys must be exactly the version-1 base "
                "schema, optionally plus comparison_lock; "
                f"got {sorted(actual_profile_keys)}"
            )
        if spec["schema_version"] != GENERATION_PROFILE_SCHEMA_VERSION:
            raise ValueError(
                "unsupported generation profile schema_version "
                f"{spec['schema_version']!r}"
            )
        profile_id = str(spec.get("profile_id") or "")
        if not RUN_ID_RE.fullmatch(profile_id):
            raise ValueError("generation profile profile_id is not a safe identifier")
        domain = str(spec.get("domain") or "")
        if domain not in SUPPORTED_TAU2_DOMAINS:
            raise ValueError(
                f"generation profile domain must be one of "
                f"{sorted(SUPPORTED_TAU2_DOMAINS)}"
            )

        replay = exact_keys(
            spec["replay"], {"source_run_id", "source_round"},
            "generation profile replay",
        )
        if not RUN_ID_RE.fullmatch(str(replay.get("source_run_id") or "")):
            raise ValueError(
                "generation profile replay.source_run_id is not safe"
            )
        if (not isinstance(replay.get("source_round"), int) or
                isinstance(replay.get("source_round"), bool) or
                replay["source_round"] < 1):
            raise ValueError(
                "generation profile replay.source_round must be a positive integer"
            )

        source = exact_keys(
            spec["source"],
            {"task_matrix", "evidence_sha256", "policy_sha256"},
            "generation profile source",
        )
        matrix = source["task_matrix"]
        evidence_hashes = source["evidence_sha256"]
        if not isinstance(matrix, dict) or not matrix:
            raise ValueError(
                "generation profile source.task_matrix must be a non-empty object"
            )
        if not isinstance(evidence_hashes, dict):
            raise ValueError(
                "generation profile source.evidence_sha256 must be an object"
            )
        if set(matrix) != set(evidence_hashes):
            raise ValueError(
                "generation profile task_matrix and evidence_sha256 keys differ"
            )
        row_lengths: set[int] = set()
        for task, row in matrix.items():
            if not str(task).isdigit():
                raise ValueError(
                    f"generation profile contains non-numeric task id {task!r}"
                )
            if not isinstance(row, list) or not row:
                raise ValueError(
                    f"generation profile task_matrix[{task!r}] must be a "
                    "non-empty list"
                )
            if any(
                    not isinstance(value, int) or isinstance(value, bool) or
                    value not in {0, 1}
                    for value in row):
                raise ValueError(
                    f"generation profile task_matrix[{task!r}] must contain "
                    "only integer 0/1 values"
                )
            row_lengths.add(len(row))
            digest = str(evidence_hashes.get(task) or "")
            if not SHA256_RE.fullmatch(digest):
                raise ValueError(
                    f"generation profile evidence_sha256[{task!r}] is invalid"
                )
        if len(row_lengths) != 1:
            raise ValueError(
                "generation profile task matrices must have equal trial counts"
            )
        if not SHA256_RE.fullmatch(str(source.get("policy_sha256") or "")):
            raise ValueError(
                "generation profile source.policy_sha256 is invalid"
            )

        alias_to_task = spec["alias_to_task"]
        if (not isinstance(alias_to_task, dict) or not alias_to_task or
                any(
                    not isinstance(alias, str) or alias.isdigit() or
                    not re.fullmatch(
                        r"[A-Za-z][A-Za-z0-9_.-]{0,63}", alias
                    )
                    for alias in alias_to_task
                ) or
                any(
                    not isinstance(task, str) or not task.isdigit()
                    for task in alias_to_task.values()
                ) or
                len(set(alias_to_task.values())) != len(alias_to_task)):
            raise ValueError(
                "generation profile alias_to_task must be a non-empty "
                "one-to-one safe case-alias -> numeric task-id object"
            )
        if set(alias_to_task.values()) != set(matrix):
            raise ValueError(
                "generation profile alias_to_task values must exactly cover "
                "source.task_matrix"
            )

        visible = exact_keys(
            spec["visible_inputs"], {
                "evidence_md", "policy_md", "capability_md",
                "plan_protocol_md", "patch_protocol_md",
            }, "generation profile visible_inputs",
        )
        visible_bytes = 0
        for name, content in visible.items():
            if (not isinstance(content, str) or not content.strip() or
                    "\r" in content or "\x00" in content):
                raise ValueError(
                    f"generation profile visible_inputs.{name} must be "
                    "non-empty canonical LF text"
                )
            visible_bytes += len(content.encode("utf-8"))
        if visible_bytes > MAX_GENERATION_PROFILE_VISIBLE_BYTES:
            raise ValueError(
                "generation profile visible inputs exceed the bounded size: "
                f"{visible_bytes} > {MAX_GENERATION_PROFILE_VISIBLE_BYTES}"
            )
        for alias in alias_to_task:
            if alias not in visible["evidence_md"]:
                raise ValueError(
                    f"generation profile visible evidence omits case alias "
                    f"{alias!r}"
                )
        for task_id in alias_to_task.values():
            leaked_inputs = [
                name for name, content in visible.items()
                if task_id in content
            ]
            if leaked_inputs:
                raise ValueError(
                    "generation profile visible inputs disclose raw task id "
                    f"{task_id!r}: {sorted(leaked_inputs)}"
                )

        tools = spec["tool_allowlist"]
        if (not isinstance(tools, list) or not tools or
                any(not isinstance(name, str) or
                    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name)
                    for name in tools) or
                len(tools) != len(set(tools))):
            raise ValueError(
                "generation profile tool_allowlist must be a non-empty unique "
                "list of safe tool names"
            )

        timeouts = exact_keys(
            spec["attempt_timeouts"],
            {"diagnosis_seconds", "patch_seconds"},
            "generation profile attempt_timeouts",
        )
        for name, maximum in (("diagnosis_seconds", 2), ("patch_seconds", 3)):
            values = timeouts[name]
            if (not isinstance(values, list) or
                    not 1 <= len(values) <= maximum or
                    any(not isinstance(value, int) or isinstance(value, bool) or
                        not 1 <= value <= 86_400 for value in values)):
                raise ValueError(
                    f"generation profile attempt_timeouts.{name} must contain "
                    f"1..{maximum} positive integer seconds"
                )

        scope = exact_keys(
            spec["plan_scope"], {
                "failing_tasks", "required_task_coverage",
                "forced_owners", "forced_opportunities",
            }, "generation profile plan_scope",
        )
        for name in ("failing_tasks", "required_task_coverage"):
            values = scope[name]
            if (not isinstance(values, list) or not values or
                    any(not isinstance(value, str) or value not in alias_to_task
                        for value in values) or
                    len(values) != len(set(values))):
                raise ValueError(
                    f"generation profile plan_scope.{name} must be a "
                    "non-empty unique list of declared case aliases"
                )
        case_aliases = set(alias_to_task)
        failing_tasks = set(scope["failing_tasks"])
        required_tasks = set(scope["required_task_coverage"])
        if not failing_tasks <= case_aliases:
            raise ValueError(
                "generation profile plan_scope.failing_tasks must be a subset "
                "of alias_to_task"
            )
        if not required_tasks <= failing_tasks:
            raise ValueError(
                "generation profile required_task_coverage must be a subset "
                "of failing_tasks"
            )
        forced_owners = scope["forced_owners"]
        forced_opportunities = scope["forced_opportunities"]
        if not isinstance(forced_owners, dict):
            raise ValueError(
                "generation profile plan_scope.forced_owners must be an object"
            )
        if not isinstance(forced_opportunities, dict):
            raise ValueError(
                "generation profile plan_scope.forced_opportunities must be "
                "an object"
            )
        valid_forced_owners = (
            ma_plan_schema.PATCHABLE_COMPONENTS |
            ma_plan_schema.NON_PATCH_OWNERS
        )
        if (not set(forced_owners) <= failing_tasks or
                any(str(owner) not in valid_forced_owners
                    for owner in forced_owners.values())):
            raise ValueError(
                "generation profile forced_owners contains an out-of-scope "
                "task or invalid owner"
            )
        if (not set(forced_opportunities) <= failing_tasks or
                any(str(status) not in ma_plan_schema.OPPORTUNITY_STATES
                    for status in forced_opportunities.values())):
            raise ValueError(
                "generation profile forced_opportunities contains an "
                "out-of-scope task or invalid status"
            )
        if (not isinstance(spec["information_boundary"], dict) or
                not spec["information_boundary"]):
            raise ValueError(
                "generation profile information_boundary must be a non-empty object"
            )

        contract = exact_keys(
            spec["plan_contract"], {
                "max_clusters", "allow_non_patch_owners",
                "patch_cluster_rules", "companion_requirements",
            }, "generation profile plan_contract",
        )
        max_clusters = contract["max_clusters"]
        if (not isinstance(max_clusters, int) or
                isinstance(max_clusters, bool) or
                not 1 <= max_clusters <= 16):
            raise ValueError(
                "generation profile plan_contract.max_clusters must be "
                "an integer in 1..16"
            )
        if not isinstance(contract["allow_non_patch_owners"], bool):
            raise ValueError(
                "generation profile plan_contract.allow_non_patch_owners "
                "must be boolean"
            )
        rules = contract["patch_cluster_rules"]
        if not isinstance(rules, list) or not rules:
            raise ValueError(
                "generation profile plan_contract.patch_cluster_rules "
                "must be a non-empty list"
            )
        rule_ids: set[str] = set()
        for index, rule_value in enumerate(rules):
            label = (
                "generation profile plan_contract.patch_cluster_rules"
                f"[{index}]"
            )
            rule = exact_keys(rule_value, {
                "rule_id", "component", "failure_modes",
                "min_clusters", "max_clusters", "min_fixes", "max_fixes",
                "fix_rules",
            }, label)
            rule_id = str(rule["rule_id"])
            if (not RUN_ID_RE.fullmatch(rule_id) or rule_id in rule_ids):
                raise ValueError(
                    f"{label}.rule_id must be a unique safe identifier"
                )
            rule_ids.add(rule_id)
            component = str(rule["component"])
            if component not in ma_plan_schema.PATCHABLE_COMPONENTS:
                raise ValueError(
                    f"{label}.component must be patchable"
                )
            failure_modes = rule["failure_modes"]
            if (not isinstance(failure_modes, list) or not failure_modes or
                    len(failure_modes) != len(set(failure_modes)) or
                    any(
                        not isinstance(mode, str) or
                        not ma_plan_schema.FAILURE_MODE_RE.fullmatch(mode)
                        for mode in failure_modes
                    )):
                raise ValueError(
                    f"{label}.failure_modes must be a non-empty unique "
                    "list of safe snake_case labels"
                )
            for minimum_name, maximum_name in (
                    ("min_clusters", "max_clusters"),
                    ("min_fixes", "max_fixes")):
                minimum = rule[minimum_name]
                maximum = rule[maximum_name]
                if (not isinstance(minimum, int) or
                        isinstance(minimum, bool) or
                        not isinstance(maximum, int) or
                        isinstance(maximum, bool) or
                        minimum < 0 or maximum < minimum or
                        maximum > max_clusters):
                    raise ValueError(
                        f"{label} has invalid {minimum_name}/"
                        f"{maximum_name} bounds"
                    )
            fix_rules = rule["fix_rules"]
            if not isinstance(fix_rules, list) or not fix_rules:
                raise ValueError(
                    f"{label}.fix_rules must be a non-empty list"
                )
            seen_actions: set[str] = set()
            for fix_index, fix_rule_value in enumerate(fix_rules):
                fix_label = f"{label}.fix_rules[{fix_index}]"
                fix_rule = exact_keys(fix_rule_value, {
                    "action", "exact_fields", "required_text_fields",
                }, fix_label)
                action = str(fix_rule["action"])
                if (action not in
                        ma_plan_schema.ACTIONS_BY_COMPONENT[component] or
                        action in seen_actions):
                    raise ValueError(
                        f"{fix_label}.action must be a unique action legal "
                        f"for component {component}"
                    )
                seen_actions.add(action)
                exact_fields = fix_rule["exact_fields"]
                required_text = fix_rule["required_text_fields"]
                if (not isinstance(exact_fields, dict) or
                        "action" in exact_fields or
                        any(
                            not isinstance(key, str) or not key
                            for key in exact_fields
                        )):
                    raise ValueError(
                        f"{fix_label}.exact_fields must be an object with "
                        "safe non-action field names"
                    )
                if (not isinstance(required_text, list) or
                        len(required_text) != len(set(required_text)) or
                        any(
                            not isinstance(field, str) or
                            not re.fullmatch(
                                r"[A-Za-z_][A-Za-z0-9_]{0,63}", field
                            )
                            for field in required_text
                        ) or
                        "action" in required_text or
                        set(required_text) & set(exact_fields)):
                    raise ValueError(
                        f"{fix_label}.required_text_fields must be a unique "
                        "list disjoint from exact_fields"
                    )
        companions = contract["companion_requirements"]
        if not isinstance(companions, list):
            raise ValueError(
                "generation profile plan_contract.companion_requirements "
                "must be a list"
            )
        for index, companion_value in enumerate(companions):
            label = (
                "generation profile plan_contract.companion_requirements"
                f"[{index}]"
            )
            companion = exact_keys(companion_value, {
                "if_rule", "requires_rule", "same_evidence_cases",
            }, label)
            if (companion["if_rule"] not in rule_ids or
                    companion["requires_rule"] not in rule_ids or
                    companion["if_rule"] == companion["requires_rule"] or
                    not isinstance(companion["same_evidence_cases"], bool)):
                raise ValueError(
                    f"{label} must reference two distinct known rules and "
                    "use a boolean same_evidence_cases"
                )
        retrieval_rule_ids = {
            rule["rule_id"]
            for rule in rules
            if any(
                fix_rule["action"] in {
                    "set_delivery", "edit_delivery",
                } and
                fix_rule["exact_fields"].get("deliverer") ==
                    "need_driven_retrieval"
                for fix_rule in rule["fix_rules"]
            )
        }
        for rule in rules:
            if rule["rule_id"] not in retrieval_rule_ids:
                continue
            retrieval_fixes = [
                fix_rule for fix_rule in rule["fix_rules"]
                if fix_rule["action"] in {
                    "set_delivery", "edit_delivery",
                } and fix_rule["exact_fields"].get("deliverer") ==
                    "need_driven_retrieval"
            ]
            if (len(retrieval_fixes) != 1 or
                    not isinstance(
                        retrieval_fixes[0]["exact_fields"].get("cfg"),
                        dict,
                    )):
                raise ValueError(
                    "each need_driven_retrieval rule must lock exactly one "
                    "carrier fix with an exact cfg object"
                )
        activation_queries = spec["activation_queries"]
        if (not isinstance(activation_queries, dict) or
                set(activation_queries) != retrieval_rule_ids):
            raise ValueError(
                "generation profile activation_queries keys must exactly "
                "match need_driven_retrieval rule ids"
            )
        for rule_id, query_values in activation_queries.items():
            if not isinstance(query_values, list) or not query_values:
                raise ValueError(
                    f"generation profile activation_queries[{rule_id!r}] "
                    "must be a non-empty list"
                )
            query_ids: set[str] = set()
            for index, query_value in enumerate(query_values):
                label = (
                    f"generation profile activation_queries[{rule_id!r}]"
                    f"[{index}]"
                )
                query = exact_keys(
                    query_value, {"query_id", "text"}, label
                )
                query_id = str(query["query_id"])
                text = query["text"]
                if (not RUN_ID_RE.fullmatch(query_id) or
                        query_id in query_ids or
                        not isinstance(text, str) or not text.strip() or
                        "\r" in text or "\x00" in text):
                    raise ValueError(
                        f"{label} must contain a unique safe query_id and "
                        "non-empty canonical-LF text"
                    )
                query_ids.add(query_id)
                if text not in visible["evidence_md"]:
                    raise ValueError(
                        f"{label}.text must be copied verbatim from "
                        "visible_inputs.evidence_md"
                    )
                leaked = [
                    task for task in alias_to_task.values()
                    if task in text
                ]
                if leaked:
                    raise ValueError(
                        f"{label}.text discloses raw task id(s) {leaked}"
                    )

        if "comparison_lock" in spec:
            comparison_lock = spec["comparison_lock"]
            comparison_lock = exact_keys(
                comparison_lock, {
                    "arms", "meta_treatment", "downstream_treatment",
                    "max_concurrency", "runtime_tree_sha256", "evaluation",
                }, "generation profile comparison_lock",
            )
            arms = comparison_lock["arms"]
            if not isinstance(arms, list) or len(arms) != 2:
                raise ValueError(
                    "generation profile comparison_lock.arms must contain "
                    "exactly two preregistered arms"
                )
            arm_ids: set[str] = set()
            run_ids: set[str] = set()
            meta_models: set[str] = set()
            generation_orders: set[int] = set()
            model_pattern = re.compile(
                r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}"
            )
            for index, arm_value in enumerate(arms):
                label = (
                    f"generation profile comparison_lock.arms[{index}]"
                )
                arm = exact_keys(arm_value, {
                    "arm_id", "run_id", "meta_model", "generation_order",
                }, label)
                arm_id = str(arm["arm_id"])
                run_id = str(arm["run_id"])
                meta_model = str(arm["meta_model"])
                generation_order = arm["generation_order"]
                if (not isinstance(arm["arm_id"], str) or
                        not RUN_ID_RE.fullmatch(arm_id) or
                        arm_id in arm_ids):
                    raise ValueError(
                        f"{label}.arm_id must be a unique safe identifier"
                    )
                if (not isinstance(arm["run_id"], str) or
                        not RUN_ID_RE.fullmatch(run_id) or
                        run_id in run_ids):
                    raise ValueError(
                        f"{label}.run_id must be a unique safe identifier"
                    )
                if (not isinstance(arm["meta_model"], str) or
                        not model_pattern.fullmatch(meta_model) or
                        meta_model in meta_models):
                    raise ValueError(
                        f"{label}.meta_model must be a unique safe model id"
                    )
                if (not isinstance(generation_order, int) or
                        isinstance(generation_order, bool) or
                        generation_order not in {1, 2} or
                        generation_order in generation_orders):
                    raise ValueError(
                        f"{label}.generation_order must uniquely cover 1 and 2"
                    )
                arm_ids.add(arm_id)
                run_ids.add(run_id)
                meta_models.add(meta_model)
                generation_orders.add(generation_order)
            if generation_orders != {1, 2}:
                raise ValueError(
                    "generation profile comparison_lock generation_order "
                    "must exactly cover 1 and 2"
                )

            meta_treatment = exact_keys(
                comparison_lock["meta_treatment"],
                {"reasoning_effort", "temperature"},
                "generation profile comparison_lock.meta_treatment",
            )
            if (not isinstance(meta_treatment["reasoning_effort"], str) or
                    not RUN_ID_RE.fullmatch(
                        meta_treatment["reasoning_effort"])):
                raise ValueError(
                    "generation profile comparison_lock meta reasoning "
                    "must be a safe non-empty identifier"
                )
            if (not isinstance(meta_treatment["temperature"], (int, float)) or
                    isinstance(meta_treatment["temperature"], bool) or
                    not 0 <= meta_treatment["temperature"] <= 2):
                raise ValueError(
                    "generation profile comparison_lock meta temperature "
                    "must be numeric in [0, 2]"
                )

            downstream = exact_keys(
                comparison_lock["downstream_treatment"], {
                    "worker_model", "worker_reasoning_effort",
                    "worker_temperature", "simulator_model",
                    "simulator_reasoning_effort", "simulator_temperature",
                },
                "generation profile comparison_lock.downstream_treatment",
            )
            for field in ("worker_model", "simulator_model"):
                if (not isinstance(downstream[field], str) or
                        not model_pattern.fullmatch(downstream[field])):
                    raise ValueError(
                        "generation profile comparison_lock "
                        f"downstream_treatment.{field} is not a safe model id"
                    )
            for field in (
                    "worker_reasoning_effort",
                    "simulator_reasoning_effort"):
                if (not isinstance(downstream[field], str) or
                        not RUN_ID_RE.fullmatch(downstream[field])):
                    raise ValueError(
                        "generation profile comparison_lock "
                        f"downstream_treatment.{field} is not a safe "
                        "reasoning identifier"
                    )
            for field in ("worker_temperature", "simulator_temperature"):
                if (not isinstance(downstream[field], (int, float)) or
                        isinstance(downstream[field], bool) or
                        not 0 <= downstream[field] <= 2):
                    raise ValueError(
                        "generation profile comparison_lock "
                        f"downstream_treatment.{field} must be numeric "
                        "in [0, 2]"
                    )

            max_concurrency = comparison_lock["max_concurrency"]
            if (not isinstance(max_concurrency, int) or
                    isinstance(max_concurrency, bool) or
                    not 1 <= max_concurrency <= 256):
                raise ValueError(
                    "generation profile comparison_lock.max_concurrency "
                    "must be an integer in 1..256"
                )
            if not SHA256_RE.fullmatch(
                    str(comparison_lock["runtime_tree_sha256"])):
                raise ValueError(
                    "generation profile comparison_lock.runtime_tree_sha256 "
                    "must be a SHA-256 digest"
                )
            evaluation = exact_keys(
                comparison_lock["evaluation"],
                {"protocol_id", "evaluator"},
                "generation profile comparison_lock.evaluation",
            )
            if (not isinstance(evaluation["protocol_id"], str) or
                    not RUN_ID_RE.fullmatch(evaluation["protocol_id"]) or
                    not isinstance(evaluation["evaluator"], str) or
                    not re.fullmatch(
                        r"[A-Za-z][A-Za-z0-9_.]{0,127}",
                        evaluation["evaluator"])):
                raise ValueError(
                    "generation profile comparison_lock.evaluation must "
                    "bind a safe protocol_id and evaluator"
                )

        canonical_bytes = json.dumps(
            spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return {
            "path": path,
            "raw_bytes": raw_bytes,
            "raw_sha256": actual,
            "canonical_sha256":
                hashlib.sha256(canonical_bytes).hexdigest(),
            "spec": spec,
        }

    def _validate_generation_profile_treatment(self) -> None:
        """Bind a validated profile to the current CLI treatment."""
        spec = self.generation_profile_spec
        if spec is None:
            return
        if spec["domain"] != self.domain:
            raise ValueError(
                "generation profile domain differs from --domain"
            )
        replay = spec["replay"]
        if (str(replay["source_run_id"]) != str(self.replay_source_run) or
                int(replay["source_round"]) != int(self.replay_source_round)):
            raise ValueError(
                "generation profile replay source differs from CLI replay source"
            )
        expected_trials = {
            len(row) for row in spec["source"]["task_matrix"].values()
        }
        if expected_trials != {self.a.k}:
            raise ValueError(
                "generation profile task matrix trial count differs from --k"
            )
        configured_cap = int(self.a.cc_timeout)
        for phase, timeouts in spec["attempt_timeouts"].items():
            if any(timeout > configured_cap for timeout in timeouts):
                raise ValueError(
                    f"generation profile {phase} timeout exceeds --cc-timeout"
                )
        actual_tools = self._benchmark_profile().tool_capabilities(self.domain)
        unknown = sorted(set(spec["tool_allowlist"]) - set(actual_tools))
        if unknown:
            raise ValueError(
                f"generation profile tool_allowlist has unknown tools: {unknown}"
            )
        comparison_lock = spec.get("comparison_lock")
        if comparison_lock is None:
            self.generation_profile_comparison_arm = None
            return
        self.generation_profile_comparison_arm = None

        matched_arms = [
            arm for arm in comparison_lock["arms"]
            if arm["run_id"] == self.a.run_id
        ]
        if len(matched_arms) != 1:
            raise ValueError(
                "current --run-id does not uniquely match a preregistered "
                "comparison arm"
            )
        matched_arm = matched_arms[0]
        if matched_arm["meta_model"] != self.a.meta_model:
            raise ValueError(
                "current --meta-model differs from the preregistered "
                "comparison arm"
            )

        meta_treatment = comparison_lock["meta_treatment"]
        if (
                meta_treatment["reasoning_effort"] !=
                self.a.meta_reasoning or
                meta_treatment["temperature"] != META_TEMPERATURE):
            raise ValueError(
                "current Meta-Agent reasoning/temperature differs from "
                "comparison_lock"
            )
        downstream = comparison_lock["downstream_treatment"]
        actual_downstream = {
            "worker_model": self.a.worker_model,
            "worker_reasoning_effort": self.a.worker_reasoning,
            "worker_temperature": FORMAL_DOWNSTREAM_TEMPERATURE,
            "simulator_model": self.a.simulator_model,
            "simulator_reasoning_effort": self.a.simulator_reasoning,
            "simulator_temperature": FORMAL_DOWNSTREAM_TEMPERATURE,
        }
        if downstream != actual_downstream:
            raise ValueError(
                "current downstream worker/simulator treatment differs "
                "from comparison_lock"
            )
        if comparison_lock["max_concurrency"] != self.a.max_concurrency:
            raise ValueError(
                "current --max-concurrency differs from comparison_lock"
            )
        runtime_tree_sha256 = self._runtime_sources_record(
            getattr(self, "benchmark", "tau2")
        ).get("tree_sha256")
        if runtime_tree_sha256 != comparison_lock["runtime_tree_sha256"]:
            raise ValueError(
                "current runtime source tree differs from comparison_lock"
            )
        self.generation_profile_comparison_arm = copy.deepcopy(matched_arm)

    def _acquire_run_lock(self) -> None:
        lock_path = self.run_dir / ".driver.lock"
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            handle.close()
            raise RuntimeError(f"run-id {self.a.run_id!r} is already active") from exc
        self._run_lock = handle
        self._acquire_campaign_lock()

    def _acquire_campaign_lock(self) -> None:
        """One campaign per framework tree.

        Concurrent drivers scanning the same tree attribute each other's run
        products as stray writes — a TB2.1 eval writing its fingerprints
        while a banking CC session was open got that session killed for
        contamination.  The OS byte lock dies with the process, so there is
        no stale-lock handling.  RECURIS_ALLOW_CONCURRENT_CAMPAIGNS=1 opts out
        for deliberately isolated setups."""
        self._campaign_lock = None
        if os.environ.get("RECURIS_ALLOW_CONCURRENT_CAMPAIGNS") == "1":
            return
        lock_dir = ROOT / "logs"
        lock_dir.mkdir(parents=True, exist_ok=True)
        handle = (lock_dir / ".campaign.lock").open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            handle.close()
            self._release_run_lock()
            raise RuntimeError(
                "another Meta-Agent campaign is already running in this "
                "framework tree; concurrent campaigns poison each other's "
                "session-scope audits. Wait for it to finish, or set "
                "RECURIS_ALLOW_CONCURRENT_CAMPAIGNS=1 only with disjoint "
                "scan/write surfaces."
            ) from exc
        self._campaign_lock = handle

    def _release_run_lock(self) -> None:
        campaign = getattr(self, "_campaign_lock", None)
        if campaign is not None:
            try:
                campaign.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(campaign.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(campaign.fileno(), fcntl.LOCK_UN)
            finally:
                campaign.close()
                self._campaign_lock = None
        handle = getattr(self, "_run_lock", None)
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._run_lock = None

    @staticmethod
    def _default_state() -> dict:
        return {"round_done": -1, "best": None, "best_dev": None,
                "best_dev_seeds": None, "best_dev_benchmark_git_commit": None,
                "best_dev_benchmark_input_sha256": None,
                "bare_dev_reference": None,
                "sims": 0, "blocked": [], "changelog": [],
                "train_history": {}, "version_hashes": {},
                "round_outcomes": {}}

    @staticmethod
    def _split_list(splits: dict, name: str, *, required: bool = True,
                    benchmark: str = "tau2") -> list[str]:
        # Static with a benchmark parameter: split-audit tooling calls this
        # without a Driver instance (tau2 arms); driver internals pass
        # getattr(self, "benchmark", "tau2") so tb21 splits accept task-name ids.
        if name not in splits and not required:
            return []
        raw = splits.get(name)
        if not isinstance(raw, list):
            raise ValueError(f"split {name!r} must be a list")
        values = [str(value) for value in raw]
        # Retail/airline ids are numeric; knowledge domains use safe
        # identifiers like task_001. Both stay shell- and path-safe.
        if any(not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", value)
               for value in values):
            raise ValueError(f"split {name!r} contains an unsafe task id")
        if len(values) != len(set(values)):
            raise ValueError(f"split {name!r} contains duplicate task ids")
        return values

    def _validate_splits(self, *, require_nonempty: bool):
        named = {"train": set(self.train_ids), "dev": set(self.dev_ids),
                 "test": set(self.test_ids)}
        for left, right in (("train", "dev"), ("train", "test"), ("dev", "test")):
            overlap = named[left] & named[right]
            if overlap:
                raise ValueError(f"split leakage: {left}/{right} overlap: {sorted(overlap)}")
        if require_nonempty and not self.dev_ids:
            raise ValueError("dev split must be non-empty")
        if require_nonempty and self.a.rounds > 0 and not self.train_ids:
            raise ValueError("train split must be non-empty when evolution rounds are requested")

    def _validate_split_lineage(self) -> None:
        """Validate optional preregistration lineage without reading task content."""
        splits = self.split_metadata
        source_value = splits.get("source_split")
        if source_value:
            source_path = Path(str(source_value))
            if not source_path.is_absolute():
                source_path = ROOT / source_path
            source_path = source_path.resolve()
            if not source_path.is_file() or source_path == self.splits_path:
                raise ValueError("source_split must name a distinct existing split file")
            expected_hash = splits.get("source_split_sha256")
            if expected_hash is not None:
                actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
                if str(expected_hash).lower() != actual_hash:
                    raise ValueError("source_split_sha256 does not match source_split bytes")
            source = json.loads(source_path.read_text(encoding="utf-8-sig"))
            if not isinstance(source, dict):
                raise ValueError("source_split must contain a JSON object")
            for name, selected in (
                    ("train", self.train_ids), ("dev", self.dev_ids),
                    ("test", self.test_ids)):
                source_ids = set(self._split_list(
                    source, name, required=False,
                    benchmark=getattr(self, "benchmark", "tau2")))
                outside = set(selected) - source_ids
                if outside:
                    raise ValueError(
                        f"split {name!r} contains ids outside source_split: "
                        f"{sorted(outside)}"
                    )
        for metadata_name, selected in (
                ("excluded_prior_train", self.train_ids),
                ("excluded_prior_dev", self.dev_ids)):
            if metadata_name not in splits:
                continue
            excluded = self._split_list(
                splits, metadata_name,
                benchmark=getattr(self, "benchmark", "tau2"))
            overlap = set(excluded) & set(selected)
            if overlap:
                raise ValueError(
                    f"{metadata_name} overlaps the selected tranche: {sorted(overlap)}"
                )
        fixed = splits.get("fixed_budget")
        if fixed is not None:
            if not isinstance(fixed, dict):
                raise ValueError("fixed_budget must be an object")
            expected = {
                "k": self.a.k,
                "train_tasks": len(self.train_ids),
                "dev_tasks": len(self.dev_ids),
                "complete_entire_train_tranche": True,
            }
            for key, value in expected.items():
                if fixed.get(key) != value:
                    raise ValueError(
                        f"fixed_budget.{key}={fixed.get(key)!r}, expected {value!r}"
                    )
            if not str(fixed.get("stop_rule") or "").strip():
                raise ValueError("fixed_budget.stop_rule must be explicit")

    @staticmethod
    def _package_digest(root: Path) -> str:
        root = Path(root)
        if not root.is_dir():
            raise RuntimeError(f"package directory is missing: {root}")
        rows = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if (path.is_file() and "__pycache__" not in relative.parts and
                    path.suffix.lower() not in {".pyc", ".pyo"}):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                rows.append(f"{relative.as_posix()}:{digest}")
        if not rows:
            raise RuntimeError(f"package has no functional files: {root}")
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    def _validate_state(self) -> None:
        missing = set(self._default_state()) - set(self.state)
        if missing:
            raise RuntimeError(f"run state predates safe versioning; missing keys: {sorted(missing)}")
        if not isinstance(self.state["round_done"], int) or self.state["round_done"] < -1:
            raise RuntimeError("invalid state.round_done")
        if not isinstance(self.state["version_hashes"], dict):
            raise RuntimeError("invalid state.version_hashes")
        version_dirs = {p.name: p for p in self.versions_dir.iterdir()
                        if p.is_dir() and re.fullmatch(r"M\d+", p.name)}
        temp_dirs = [p for p in self.versions_dir.iterdir() if p.name.startswith(".M")]
        if temp_dirs:
            raise RuntimeError(f"incomplete version transaction(s) require inspection: {temp_dirs}")
        if set(version_dirs) != set(self.state["version_hashes"]):
            raise RuntimeError("immutable version directories do not match state.version_hashes")
        for name, path in version_dirs.items():
            self._assert_no_linklike(path, allow_missing=False)
            if self._package_digest(path) != self.state["version_hashes"][name]:
                raise RuntimeError(f"immutable version {name} failed its SHA-256 check")
        best = self.state.get("best")
        if best is None:
            if (version_dirs or self.state.get("best_dev") is not None or
                    self.state.get("best_dev_seeds") is not None or
                    self.state.get("best_dev_benchmark_git_commit") is not None or
                    self.state.get("best_dev_benchmark_input_sha256") is not None or
                    self.state.get("bare_dev_reference") is not None):
                raise RuntimeError("uninitialized state contains committed package data")
            return
        best_path = Path(best).resolve()
        if (best_path.parent != self.versions_dir.resolve() or
                best_path.name not in version_dirs or not best_path.is_dir()):
            raise RuntimeError(f"state.best is not an immutable version in this run: {best}")
        if not isinstance(self.state.get("best_dev"), dict):
            raise RuntimeError("initialized state has no best_dev matrix")
        if not isinstance(self.state.get("best_dev_seeds"), dict):
            raise RuntimeError("initialized state has no best_dev seed proof")
        if not re.fullmatch(
                r"[0-9a-f]{40}",
                str(self.state.get("best_dev_benchmark_git_commit") or "")):
            raise RuntimeError("initialized state has no valid benchmark commit proof")
        if not re.fullmatch(
                r"[0-9a-f]{64}",
                str(self.state.get("best_dev_benchmark_input_sha256") or "")):
            raise RuntimeError("initialized state has no valid benchmark input proof")
        bare_reference = self.state.get("bare_dev_reference")
        if bare_reference is not None:
            if not isinstance(bare_reference, dict):
                raise RuntimeError("bare_dev_reference must be an object or null")
            if not isinstance(bare_reference.get("matrix"), dict):
                raise RuntimeError("bare_dev_reference lacks a matrix")
            if not isinstance(bare_reference.get("seeds"), dict):
                raise RuntimeError("bare_dev_reference lacks paired seeds")
            if not re.fullmatch(
                    r"[0-9a-f]{40}", str(bare_reference.get("benchmark_git_commit") or "")):
                raise RuntimeError("bare_dev_reference lacks benchmark commit proof")
            if not re.fullmatch(
                    r"[0-9a-f]{64}", str(bare_reference.get("benchmark_input_sha256") or "")):
                raise RuntimeError("bare_dev_reference lacks benchmark input proof")
        provisional = self.state.get("provisional")
        if provisional is not None:
            if not isinstance(provisional, dict):
                raise RuntimeError("state.provisional must be an object or null")
            package = Path(str(provisional.get("package") or ""))
            if (package.resolve().parent != self.package_root.resolve() or
                    not package.is_dir()):
                raise RuntimeError(
                    f"state.provisional package is not in this run: {package}"
                )
            self._assert_no_linklike(package, allow_missing=False)
            if self._package_digest(package) != provisional.get("tree_sha256"):
                raise RuntimeError("provisional base failed its SHA-256 check")
            if not isinstance(provisional.get("round"), int):
                raise RuntimeError("state.provisional lacks its round number")

    def _verify_versions(self) -> None:
        self._validate_state()

    def _verify_generated_candidates(self) -> None:
        frozen_root = self.package_root / "generated_candidates"
        records = sorted(self.run_dir.glob("candidate_freeze_r*.json"))
        provenance = (
            json.loads(self.provenance_path.read_text(encoding="utf-8"))
            if self.provenance_path.is_file() else {}
        )
        disclosures = provenance.get("capability_disclosures", {})
        generation_profiles = provenance.get("generation_profiles", {})
        profiled_run = getattr(self, "generation_profile_spec", None) is not None
        if not frozen_root.exists():
            if records:
                raise RuntimeError("candidate freeze record exists without frozen package")
            return
        self._assert_no_linklike(frozen_root, allow_missing=False)
        directories = {
            path.name: path for path in frozen_root.iterdir()
            if path.is_dir() and re.fullmatch(r"C\d+", path.name)
        }
        if any(path.name.startswith(".C") for path in frozen_root.iterdir()):
            raise RuntimeError("incomplete generated-candidate freeze transaction")
        expected_names: set[str] = set()
        for record_path in records:
            match = re.fullmatch(r"candidate_freeze_r(\d+)\.json", record_path.name)
            if not match:
                raise RuntimeError(f"invalid candidate freeze record name: {record_path}")
            record = json.loads(record_path.read_text(encoding="utf-8"))
            rnd = int(match.group(1))
            if record.get("round") != rnd:
                raise RuntimeError(f"candidate freeze round mismatch: {record_path}")
            candidate = record.get("candidate") or {}
            name = f"C{rnd}"
            expected_names.add(name)
            frozen_dir = directories.get(name)
            if frozen_dir is None:
                raise RuntimeError(f"frozen generated candidate is missing: {name}")
            recorded_path = Path(str(candidate.get("frozen_path") or "")).resolve()
            if recorded_path != frozen_dir.resolve():
                raise RuntimeError(f"candidate freeze path mismatch: {record_path}")
            digest = candidate.get("tree_sha256")
            if (not re.fullmatch(r"[0-9a-f]{64}", str(digest or "")) or
                    self._package_digest(frozen_dir) != digest):
                raise RuntimeError(f"frozen generated candidate failed SHA-256: {name}")
            frozen_disclosure = (
                (record.get("generation_runtime") or {}).get(
                    "capability_disclosure"
                )
            )
            if frozen_disclosure != disclosures.get(str(rnd)):
                raise RuntimeError(
                    f"candidate freeze capability disclosure mismatch: {name}"
                )
            frozen_sessions = (
                (record.get("meta_agent") or {}).get("sessions")
            )
            source_sessions = provenance.get("meta_sessions", [])
            if (not isinstance(frozen_sessions, list) or
                    len(frozen_sessions) != len(source_sessions)):
                raise RuntimeError(
                    f"candidate freeze session proof mismatch: {name}"
                )
            required_session_keys = {
                "name", "tool_surface", "read_scope_violations",
                "edit_scope_violations", "ok",
            }
            for frozen_session, source_session in zip(
                    frozen_sessions, source_sessions):
                if (not isinstance(frozen_session, dict) or
                        not isinstance(source_session, dict) or
                        not required_session_keys <= set(frozen_session) or
                        frozen_session != {
                            key: copy.deepcopy(source_session.get(key))
                            for key in frozen_session
                        }):
                    raise RuntimeError(
                        f"candidate freeze session proof changed: {name}"
                    )
            frozen_profile = record.get("generation_profile")
            if profiled_run:
                profile_record = generation_profiles.get(str(rnd))
                profile_paths = self._generation_profile_paths(
                    rnd, self.run_dir / f"round_{rnd}"
                )
                if not profile_paths["manifest"].is_file():
                    raise RuntimeError(
                        f"candidate freeze generation profile is missing: {name}"
                    )
                profile_manifest = json.loads(
                    profile_paths["manifest"].read_text(
                        encoding="utf-8"
                    )
                )
                plan_outputs = (
                    frozen_profile.get("plan_outputs")
                    if isinstance(frozen_profile, dict) else None
                )
                scope = self.generation_profile_spec["plan_scope"]
                verification_context = {
                    "spec": self.generation_profile_spec,
                    "paths": profile_paths,
                    "manifest": profile_manifest,
                    "plan_outputs": plan_outputs,
                    "alias_to_task": copy.deepcopy(
                        self.generation_profile_spec["alias_to_task"]
                    ),
                    "failing_aliases": set(scope["failing_tasks"]),
                    "required_alias_coverage":
                        set(scope["required_task_coverage"]),
                }
                verified_plan_outputs = (
                    self._verify_generation_profile_plan_outputs(
                        verification_context
                    )
                )
                if (
                        (record.get("validated_plan") or {}).get(
                            "matched_rule_ids"
                        ) !=
                        verified_plan_outputs["matched_rule_ids"]):
                    raise RuntimeError(
                        f"candidate freeze matched-rule proof mismatch: {name}"
                    )
                activation_proof = (
                    frozen_profile.get("activation_query_proof")
                    if isinstance(frozen_profile, dict) else None
                )
                activation_path = profile_paths[
                    "activation_query_proof"
                ]
                if (
                        not isinstance(activation_proof, dict) or
                        activation_proof.get("path") !=
                        str(activation_path) or
                        not activation_path.is_file() or
                        self._sha256_file(activation_path) !=
                        activation_proof.get("sha256") or
                        json.loads(activation_path.read_text(
                            encoding="utf-8")) !=
                        activation_proof.get("report") or
                        activation_proof["report"].get("passed") is not True or
                        activation_proof["report"].get("offline") is not True or
                        activation_proof["report"].get("model_calls") != 0 or
                        activation_proof["report"].get("network_calls") != 0):
                    raise RuntimeError(
                        f"candidate freeze activation-query proof mismatch: "
                        f"{name}"
                    )
                expected_profile = {
                    "provenance": profile_record,
                    "input_manifest": profile_manifest,
                    "plan_outputs": verified_plan_outputs,
                    "activation_query_proof": activation_proof,
                }
                if "comparison_lock" in self.generation_profile_spec:
                    expected_profile["comparison_lock"] = copy.deepcopy(
                        self.generation_profile_spec["comparison_lock"]
                    )
                    expected_profile["matched_comparison_arm"] = (
                        copy.deepcopy(
                            self.generation_profile_comparison_arm
                        )
                    )
                if frozen_profile != expected_profile:
                    raise RuntimeError(
                        f"candidate freeze generation profile mismatch: {name}"
                    )
                if "locked-generation-profile" not in (
                        candidate.get("validation") or []):
                    raise RuntimeError(
                        f"profiled candidate lacks locked-profile validation: {name}"
                    )
            elif frozen_profile is not None:
                raise RuntimeError(
                    f"unprofiled candidate contains a generation profile: {name}"
                )
        if set(directories) != expected_names:
            raise RuntimeError(
                "generated candidate directories do not match candidate freeze records"
            )

    def _reconcile_commits(self) -> None:
        """Recover package commit and round-finalization crash windows."""
        for journal_path in sorted(self.run_dir.glob("commit_M*.json")):
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("status") in {"complete", "rolled_back"}:
                continue
            name = str(journal.get("version"))
            if not re.fullmatch(r"M\d+", name):
                raise RuntimeError(f"invalid commit journal: {journal_path}")
            version_dir = self.versions_dir / name
            temp_value = journal.get("temp")
            temp_dir = self.versions_dir / str(temp_value) if temp_value else None
            if not version_dir.exists():
                if temp_dir and temp_dir.exists():
                    self._safe_rmtree(temp_dir)
                journal["status"] = "rolled_back"
                journal["reconciled_at"] = _now()
                self._write_json_atomic(journal_path, journal)
                continue
            digest = self._package_digest(version_dir)
            if digest != journal.get("tree_sha256"):
                raise RuntimeError(f"committed version differs from journal: {version_dir}")
            finalization = journal.get("finalization")
            if not isinstance(finalization, dict):
                raise RuntimeError(f"commit journal lacks recoverable finalization: {journal_path}")
            self.state["version_hashes"][name] = digest
            self.state["best"] = str(version_dir)
            self.state["best_dev"] = journal["best_dev"]
            self.state["best_dev_seeds"] = journal["best_dev_seeds"]
            self.state["best_dev_benchmark_git_commit"] = journal[
                "best_dev_benchmark_git_commit"
            ]
            self.state["best_dev_benchmark_input_sha256"] = journal[
                "best_dev_benchmark_input_sha256"
            ]
            self.state["bare_dev_reference"] = journal.get("bare_dev_reference")
            entry = journal["changelog_entry"]
            if not any(c.get("round") == entry.get("round") for c in self.state["changelog"]):
                self.state["changelog"].append(entry)
            self.save_state()
            rnd = int(journal["round"])
            journal["status"] = "package_committed"
            self._write_json_atomic(journal_path, journal)
            self._finalize_round(rnd, finalization)
            journal["status"] = "complete"
            journal["reconciled_at"] = _now()
            self._write_json_atomic(journal_path, journal)

    def _reconcile_round_finalizations(self) -> None:
        pending: list[tuple[int, Path, dict]] = []
        for path in self.run_dir.glob("round_*_finalize.json"):
            match = re.fullmatch(r"round_(\d+)_finalize\.json", path.name)
            if not match:
                raise RuntimeError(f"invalid round finalization journal name: {path}")
            journal = json.loads(path.read_text(encoding="utf-8"))
            if journal.get("status") != "complete":
                pending.append((int(match.group(1)), path, journal))
        for rnd, path, journal in sorted(pending):
            if journal.get("round") != rnd or not isinstance(journal.get("payload"), dict):
                raise RuntimeError(f"invalid round finalization journal: {path}")
            self._finalize_round(rnd, journal["payload"])

    def _config_record(self) -> dict:
        if self.splits_path:
            split_record = {
                "path": str(self.splits_path),
                "sha256": hashlib.sha256(self.splits_path.read_bytes()).hexdigest(),
                "train": self.train_ids,
                "dev": self.dev_ids,
                "test": self.test_ids,
            }
        else:
            split_record = None
        if self.a.base in {"neutral", "bootstrap"}:
            base_record = {"kind": self.a.base}
        else:
            source = Path(self.a.base)
            if not source.is_absolute():
                source = ROOT / source
            source = source.resolve()
            base_record = {"kind": "package", "path": str(source),
                           "tree_sha256": self._package_digest(source)}
        config = {
            "driver_schema_version": DRIVER_SCHEMA_VERSION,
            "run_id": self.a.run_id,
            "domain": self.domain,
            "arm": self.arm,
            "benchmark_protocol": self.benchmark_protocol,
            "mode": (
                "qualification-only" if self.a.qualify_only else
                "generation-only" if self.generate_only else
                "experiment"
            ),
            "replay_source": (
                {
                    "schema_version": REPLAY_SCHEMA_VERSION,
                    "run_id": str(self.replay_source_run),
                    "round": self.replay_source_round,
                    "treatment": "frozen-train-evidence+fresh-contemporaneous-gates",
                }
                if self.replay_source_run else None
            ),
            "splits": split_record,
            "base": base_record,
            "worker": {"model": self.a.worker_model,
                       "reasoning_effort": self.a.worker_reasoning,
                       "temperature": FORMAL_DOWNSTREAM_TEMPERATURE,
                       "allowed_openai_params": FORMAL_ALLOWED_OPENAI_PARAMS},
            "simulator": {"model": self.a.simulator_model,
                           "reasoning_effort": self.a.simulator_reasoning,
                           "temperature": FORMAL_DOWNSTREAM_TEMPERATURE,
                           "allowed_openai_params": FORMAL_ALLOWED_OPENAI_PARAMS},
            "meta": {"model": self.a.meta_model,
                     "reasoning_effort": self.a.meta_reasoning,
                     "temperature": META_TEMPERATURE},
            "proxy_port": self.a.proxy_port,
            "evaluation": {
                "target_round": self.a.rounds,
                "k": self.a.k,
                "round_gate": self.a.round_gate,
                "reg_cap": self.a.reg_cap,
                "max_concurrency": self.a.max_concurrency,
                "eval_timeout": self.a.eval_timeout,
                "eval_resume_attempts": getattr(
                    self.a, "eval_resume_attempts", 1
                ),
                "max_sims": self.a.max_sims,
                "reuse_artifacts_run": getattr(
                    self.a, "reuse_artifacts_run", None
                ),
                "provisional_accept": bool(
                    getattr(self.a, "provisional_accept", False)
                ),
            },
            "recursive_loop": {
                "diagnosis_review": self.recursive_review,
                "same_task_history_rounds": HISTORY_EVIDENCE_ROUNDS,
                "targeted_activation_cases": self.recursive_review,
                "candidate_corrections": (
                    3 if self.recursive_review or
                    self.meta_workflow in {"continuous", "hierarchical"} else 0
                ),
            },
            "cc_timeout": self.a.cc_timeout,
        }
        # Absence means the legacy/default split workflow. Keeping that record
        # byte-for-byte compatible lets existing split runs remain resumable.
        if self.meta_workflow == "continuous":
            config["meta_workflow"] = "continuous"
            config["recursive_loop"]["shared_diagnosis_patch_context"] = True
        elif self.meta_workflow == "hierarchical":
            config["meta_workflow"] = "hierarchical"
            config["recursive_loop"].update({
                "parallel_diagnosis_workers": self.diagnosis_workers,
                "single_skill_memory_editor": True,
            })
        if self.generation_profile_spec is not None:
            config["generation_profile"] = {
                "source_path": str(self.generation_profile_spec_path),
                "expected_raw_sha256": self.generation_profile_spec_sha256,
                "canonical_sha256":
                    self.generation_profile_canonical_sha256,
                "schema_version":
                    self.generation_profile_spec["schema_version"],
                "profile_id": self.generation_profile_spec["profile_id"],
                "attempt_timeouts": copy.deepcopy(
                    self.generation_profile_spec["attempt_timeouts"]
                ),
            }
            if "comparison_lock" in self.generation_profile_spec:
                config["generation_profile"]["comparison_lock"] = (
                    copy.deepcopy(
                        self.generation_profile_spec["comparison_lock"]
                    )
                )
                config["generation_profile"]["matched_comparison_arm"] = (
                    copy.deepcopy(
                        self.generation_profile_comparison_arm
                    )
                )
        return config

    @staticmethod
    def _runtime_sources_record(benchmark: str = "tau2") -> dict:
        """Hash every mutable source that can change an experiment treatment.

        Static with a benchmark parameter: eval-only protocol tooling calls
        this without a Driver instance (their arms are tau2), while driver
        internals pass getattr(self, "benchmark", "tau2") so tb21 runs also bind the bridge
        sources in tb21/*.py.
        """
        paths: set[Path] = set()
        patterns = (
            "metaagent/*.py", "metaagent/*.sh",
            "metaagent/protocols/*.md",
            "metaagent_demo/*.py", "src/**/*.py",
        )
        for pattern in patterns:
            paths.update(path for path in ROOT.glob(pattern) if path.is_file())
        for relative in (
                "runner/run_tau2.py", "runner/run_emonly.py",
                "runner/formal_downstream.py", "pyproject.toml"):
            path = ROOT / relative
            if path.is_file():
                paths.add(path)
        files = {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)
        }
        if benchmark == "tau2":
            bench_root = TAU2_ROOT
            if bench_root.is_dir():
                external = {path for path in bench_root.glob("src/tau2/**/*.py")
                            if path.is_file()}
                for relative in ("pyproject.toml", "uv.lock"):
                    path = bench_root / relative
                    if path.is_file():
                        external.add(path)
                for path in sorted(external):
                    label = "@tau2-bench/" + path.relative_to(bench_root).as_posix()
                    files[label] = hashlib.sha256(path.read_bytes()).hexdigest()
        rows = [f"{name}:{digest}" for name, digest in files.items()]
        return {
            "tree_sha256": hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest(),
            "files": files,
        }

    @staticmethod
    def _git_value(*args, cwd: Path = ROOT) -> str:
        cwd = Path(cwd)
        if not cwd.is_dir():
            return "unavailable"
        try:
            result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                                    text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return "unavailable"
        return result.stdout.strip() if result.returncode == 0 else "unavailable"

    def _write_json_atomic(self, path: Path, data: dict):
        assert_mutation_paths_safe([path])
        if not path.resolve().is_relative_to(self.run_dir.resolve()):
            raise RuntimeError(f"refusing to write outside run dir: {path}")
        tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _write_text_atomic(self, path: Path, content: str) -> None:
        assert_mutation_paths_safe([path])
        if not path.resolve().is_relative_to(self.run_dir.resolve()):
            raise RuntimeError(f"refusing to write outside run dir: {path}")
        tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def _write_bytes_atomic(self, path: Path, content: bytes) -> None:
        assert_mutation_paths_safe([path])
        if not path.resolve().is_relative_to(self.run_dir.resolve()):
            raise RuntimeError(f"refusing to write outside run dir: {path}")
        tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(content)
        tmp.replace(path)

    def _init_provenance(self):
        config = self._config_record()
        runtime_sources = self._runtime_sources_record(
            getattr(self, "benchmark", "tau2")
        )
        if self.a.qualify_only:
            benchmark_inputs = None
        else:
            benchmark_inputs = _benchmark_input_manifest(
                self.domain, self.ds.data_dir
            )
        if self.provenance_path.exists():
            old = json.loads(self.provenance_path.read_text(encoding="utf-8"))
            if old.get("config") != config:
                raise RuntimeError("refusing to resume run with different model/reasoning/base config")
            if old.get("runtime_sources") != runtime_sources:
                reason = str(getattr(self.a, "accept_source_change", "") or "")
                if not reason:
                    raise RuntimeError("refusing to resume run with changed runtime/Meta-Agent sources")
                # Operator-authorized amendment: record the full file-level
                # delta in provenance, then adopt the new source record.  The
                # guard stays fail-closed by default; this path exists so a
                # crashed run can resume after a deliberate framework fix
                # without discarding sealed evaluation artifacts.  The
                # Meta-Agent cannot reach this path (it never relaunches the
                # driver), so self-modification containment is unchanged.
                old_files = (old.get("runtime_sources") or {}).get("files") or {}
                new_files = runtime_sources["files"]
                changed_files = sorted(
                    name for name in set(old_files) | set(new_files)
                    if old_files.get(name) != new_files.get(name)
                )
                amendment = {
                    "at": _now(),
                    "reason": reason,
                    "old_tree_sha256": (old.get("runtime_sources") or {}).get("tree_sha256"),
                    "new_tree_sha256": runtime_sources["tree_sha256"],
                    "changed_files": changed_files,
                }
                amendments = old.get("source_amendments")
                old["source_amendments"] = (
                    [*amendments, amendment] if isinstance(amendments, list)
                    else [amendment]
                )
                old["runtime_sources"] = runtime_sources
                self._write_json_atomic(self.provenance_path, old)
                print(
                    f"[{_now()}] provenance source amendment accepted: "
                    f"{len(changed_files)} file(s) changed "
                    f"({', '.join(changed_files[:8])}); reason: {reason}",
                    flush=True,
                )
            if old.get("benchmark_inputs") != benchmark_inputs:
                raise RuntimeError("refusing to resume run with changed benchmark input data")
            for session in old.get("meta_sessions", []):
                if not isinstance(session, dict):
                    raise RuntimeError("invalid meta session provenance")
                for path_key, hash_key in (
                        ("trace", "trace_sha256"), ("prompt", "prompt_sha256"),
                        ("settings", "settings_sha256"), ("stderr", "stderr_sha256")):
                    expected = session.get(hash_key)
                    if expected is None:
                        continue
                    artifact = Path(str(session.get(path_key) or ""))
                    if (not artifact.is_file() or
                            hashlib.sha256(artifact.read_bytes()).hexdigest() != expected):
                        raise RuntimeError(
                            f"meta session artifact changed or disappeared: {artifact}"
                        )
            if not isinstance(old.get("capability_disclosures", {}), dict):
                raise RuntimeError("invalid capability disclosure provenance")
            if self.generation_profile_spec is not None:
                if not isinstance(old.get("generation_profiles"), dict):
                    raise RuntimeError("invalid generation profile provenance")
            elif old.get("generation_profiles") not in (None, {}):
                raise RuntimeError(
                    "unprofiled run contains generation profile provenance"
                )
            return
        if (self.state_path.exists() or any(self.versions_dir.iterdir()) or
                any(self.run_dir.glob("commit_M*.json"))):
            raise RuntimeError("cannot initialize provenance for a pre-existing experiment state")
        champion = verify_champions()
        provenance = {
            "created_at": _now(),
            "config": config,
            "champion_tree_sha256": champion["tree_sha256"],
            "git_commit": self._git_value("rev-parse", "HEAD"),
            "git_dirty": bool(self._git_value("status", "--porcelain")),
            **{
                self.profile.commit_key: (
                    self._git_value(
                        "rev-parse", "HEAD", cwd=TAU2_ROOT)
                    if getattr(self, "benchmark", "tau2") == "tau2" else
                    (self.ds.benchmark_taskset_commit()
                     if self.ds is not None else "unavailable")
                ),
                self.profile.dirty_key: (
                    bool(self._git_value(
                        "status", "--porcelain", cwd=TAU2_ROOT))
                    if getattr(self, "benchmark", "tau2") == "tau2" else False
                ),
            },
            "runtime_sources": runtime_sources,
            "benchmark_inputs": benchmark_inputs,
            "proxy_health": None,
            "meta_resolved_models": [],
            "meta_sessions": [],
            "meta_api_calls": [],
            "downstream_artifacts": [],
            "capability_disclosures": {},
        }
        if self.generation_profile_spec is not None:
            provenance["generation_profiles"] = {}
        self._write_json_atomic(self.provenance_path, provenance)

    def _update_provenance(self, **updates):
        data = json.loads(self.provenance_path.read_text(encoding="utf-8"))
        data.update(updates)
        self._write_json_atomic(self.provenance_path, data)

    def _restore_downstream_artifacts(self) -> None:
        if self.ds is None:
            return
        provenance = json.loads(self.provenance_path.read_text(encoding="utf-8"))
        records = provenance.get("downstream_artifacts", [])
        if not isinstance(records, list):
            raise RuntimeError("provenance.downstream_artifacts must be a list")
        expected_inputs = provenance.get("benchmark_inputs")
        expected_benchmark_commit = provenance.get(self.profile.commit_key)
        for record in records:
            if not isinstance(record, dict):
                raise RuntimeError("invalid downstream artifact provenance record")
            if record.get("status", "success") not in {"success", "failed", "started"}:
                raise RuntimeError("invalid downstream artifact status in provenance")
            if record.get("status") == "success":
                if record.get("benchmark_git_commit") != expected_benchmark_commit:
                    raise RuntimeError(
                        "downstream artifact is not bound to the frozen "
                        f"{self.profile.commit_key}"
                    )
                if record.get("benchmark_inputs") != expected_inputs:
                    raise RuntimeError("downstream artifact benchmark input proof differs")
            for path_key, hash_key in (("results", "results_sha256"),
                                       ("fingerprint", "fingerprint_sha256")):
                expected = record.get(hash_key)
                if expected is None:
                    continue
                path = Path(str(record.get(path_key) or ""))
                if (not path.is_file() or
                        hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                    raise RuntimeError(
                        f"downstream artifact changed or disappeared on resume: {path}"
                    )
        self.ds.artifacts = list(records)
        self.ds._eval_i = max(
            [int(record.get("index", 0)) for record in records
             if isinstance(record.get("index", 0), int)] or [0]
        )

    def _record_proxy_health(self, health: dict) -> None:
        with self._cc_lock:
            data = json.loads(self.provenance_path.read_text(encoding="utf-8"))
            resolved = set(data.get("meta_resolved_models", []))
            resolved.update(health.get("seen_upstream_models", []))
            data["proxy_health"] = health
            data["meta_resolved_models"] = sorted(resolved)
            self._write_json_atomic(self.provenance_path, data)

    def _safe_rmtree(self, path: Path):
        lexical = Path(path).absolute()
        assert_mutation_paths_safe([lexical])
        if not lexical.resolve().is_relative_to(self.run_dir.resolve()):
            raise RuntimeError(f"refusing to remove outside run dir: {path}")
        if lexical.exists():
            self._assert_tree_unlinked(lexical)
            shutil.rmtree(lexical)

    def _assert_tree_unlinked(self, root: Path) -> None:
        self._assert_no_linklike(root, allow_missing=False)
        stack = [root]
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_symlink() or self._is_linklike(path):
                        raise RuntimeError(f"refusing linked path inside mutable tree: {path}")
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _assert_file_under_unlinked(cls, path: Path, root: Path) -> Path:
        """Resolve one frozen source artifact without following a link boundary."""
        lexical_root = Path(os.path.abspath(str(root)))
        lexical_path = Path(os.path.abspath(str(path)))
        try:
            relative = lexical_path.relative_to(lexical_root)
        except ValueError as exc:
            raise RuntimeError(
                f"replay artifact escapes its frozen namespace: {lexical_path}"
            ) from exc
        current = lexical_root
        cls._assert_no_linklike(current, allow_missing=False)
        for part in relative.parts:
            current = current / part
            cls._assert_no_linklike(current, allow_missing=False)
        if not lexical_path.is_file():
            raise RuntimeError(f"frozen replay artifact is missing: {lexical_path}")
        if not lexical_path.resolve().is_relative_to(lexical_root.resolve()):
            raise RuntimeError(f"unsafe replay artifact resolution: {lexical_path}")
        return lexical_path

    @staticmethod
    def _lock_inactive_run(source_dir: Path):
        """Acquire the source driver's byte lock so the snapshot cannot race."""
        lock_path = Path(source_dir) / ".driver.lock"
        if not lock_path.is_file():
            raise RuntimeError(f"replay source has no driver lock: {lock_path}")
        handle = lock_path.open("r+b")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            handle.close()
            raise RuntimeError(
                f"replay source run is active and cannot be snapshotted: {source_dir}"
            ) from exc
        return handle

    @staticmethod
    def _unlock_inactive_run(handle) -> None:
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _copy_frozen_file(self, source: Path, destination: Path) -> str:
        """Copy exact bytes once; an existing destination must already match."""
        source = Path(source)
        destination = Path(destination)
        digest = self._sha256_file(source)
        assert_mutation_paths_safe([destination])
        if not destination.resolve().is_relative_to(self.run_dir.resolve()):
            raise RuntimeError(f"refusing replay copy outside run dir: {destination}")
        if destination.exists():
            if (not destination.is_file() or
                    self._sha256_file(destination) != digest):
                raise RuntimeError(
                    f"existing replay evidence differs from frozen source: {destination}"
                )
            return digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.replay.tmp"
        )
        try:
            with source.open("rb") as src, temp.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if self._sha256_file(temp) != digest:
                raise RuntimeError(f"replay copy hash mismatch: {destination}")
            temp.replace(destination)
        finally:
            if temp.exists():
                temp.unlink()
        return digest

    @staticmethod
    def _replay_config_projection(config: dict) -> dict:
        split = config.get("splits") or {}
        evaluation = config.get("evaluation") or {}
        return {
            "domain": config.get("domain"),
            "arm": config.get("arm"),
            "benchmark_protocol": config.get("benchmark_protocol"),
            "base": config.get("base"),
            "splits": {
                key: split.get(key)
                for key in ("sha256", "train", "dev", "test")
            },
            "worker": config.get("worker"),
            "simulator": config.get("simulator"),
            "evaluation": {
                key: evaluation.get(key)
                for key in (
                    "k", "round_gate", "reg_cap", "max_concurrency",
                    "eval_timeout", "eval_resume_attempts",
                )
            },
        }

    def _allowed_replay_runtime_changes(self) -> set[str]:
        """Return explicitly preregistered replay drift for this framework version."""
        allowed = {
            "src/recuris/metaagent/proxy/anthropic_openai_proxy.py",
            "src/recuris/metaagent/driver.py",
            "src/recuris/metaagent/launchers/run_claude_code.sh",
        }
        if self.generate_only:
            # Recovery-only drift makes the later, separate evaluation
            # resumable.  The v6 files are the intentional generation
            # treatment: they add carrier reachability and event timing rules.
            # Both backend arms must run under this exact same runtime.
            allowed.update({
                "src/recuris/metaagent/downstream.py",
                "src/recuris/metaagent/settle.py",
                "src/recuris/metaagent/eval_run_guard.py",
                "src/recuris/adapters/tau2/run.py",
                "src/recuris/metaagent/activation_probe.py",
                "src/recuris/metaagent/lint.py",
                "src/recuris/metaagent/plan_schema.py",
                "src/recuris/metaagent/reachability.py",
                "src/recuris/metaagent/protocols/capability_manual.md",
                "src/recuris/metaagent/protocols/tau2_capabilities.md",
                "src/recuris/metaagent/protocols/diagnosis.md",
                "src/recuris/metaagent/protocols/patch.md",
            })
        return allowed

    def _build_replay_snapshot(
            self, *, materialize_evidence: bool = True,
    ) -> tuple[dict, object, dict, Path]:
        """Validate and reparse a sealed round-1 train artifact under current code."""
        if self.replay_source_dir is None or self.ds is None:
            raise RuntimeError("replay source/evaluator is unavailable")
        source_dir = self.replay_source_dir
        self._assert_tree_unlinked(source_dir)
        required = {
            "state": source_dir / "state.json",
            "provenance": source_dir / "provenance.json",
            "ledger": source_dir / "ledger.jsonl",
            "finalization": source_dir / "round_1_finalize.json",
        }
        for label, path in required.items():
            if not path.is_file() or self._is_linklike(path):
                raise RuntimeError(f"replay source lacks sealed {label}: {path}")
        source_state = json.loads(required["state"].read_text(encoding="utf-8"))
        source_provenance = json.loads(
            required["provenance"].read_text(encoding="utf-8")
        )
        finalization = json.loads(
            required["finalization"].read_text(encoding="utf-8")
        )
        source_outcome = (
            (source_state.get("round_outcomes") or {}).get("1")
        )
        if (source_state.get("round_done") != 1 or
                finalization.get("status") != "complete" or
                finalization.get("round") != 1 or
                (finalization.get("payload") or {}).get("outcome") != source_outcome):
            raise RuntimeError("replay source round 1 is not atomically sealed")
        if source_outcome not in {
                "no-plan", "harness-failure", "upstream-model-failure",
                "invalid-plan"}:
            raise RuntimeError(
                f"replay source outcome is not a diagnosis-only failure: {source_outcome!r}"
            )
        if source_state.get("blocked") not in ([], None):
            raise RuntimeError("replay schema v1 requires no post-round blocked mutations")

        source_config = source_provenance.get("config")
        current_provenance = json.loads(
            self.provenance_path.read_text(encoding="utf-8")
        )
        current_config = current_provenance.get("config")
        if (not isinstance(source_config, dict) or
                self._replay_config_projection(source_config) !=
                self._replay_config_projection(current_config)):
            raise RuntimeError(
                "replay source and destination differ in downstream/split/gate treatment"
            )
        if source_config.get("replay_source") is not None:
            raise RuntimeError("nested replay sources are forbidden")
        if source_provenance.get("tau2_git_commit") != current_provenance.get(
                "tau2_git_commit"):
            raise RuntimeError("Tau2 commit changed since the frozen source run")
        if source_provenance.get("benchmark_inputs") != current_provenance.get(
                "benchmark_inputs"):
            raise RuntimeError("benchmark inputs changed since the frozen source run")

        source_runtime = (source_provenance.get("runtime_sources") or {}).get(
            "files") or {}
        current_runtime = (current_provenance.get("runtime_sources") or {}).get(
            "files") or {}
        changed_runtime = sorted(
            name for name in set(source_runtime) | set(current_runtime)
            if source_runtime.get(name) != current_runtime.get(name)
        )
        allowed_runtime_changes = self._allowed_replay_runtime_changes()
        unexpected_runtime = sorted(
            set(changed_runtime) - allowed_runtime_changes
        )
        if unexpected_runtime:
            raise RuntimeError(
                "replay source has treatment-relevant runtime drift outside the "
                f"preregistered Meta-Agent framework patch: {unexpected_runtime}"
            )

        source_m0 = (
            source_dir / "packages" / "versions" / "M0"
        )
        expected_best = source_m0.resolve()
        if (Path(str(source_state.get("best") or "")).resolve() != expected_best or
                set(source_state.get("version_hashes") or {}) != {"M0"}):
            raise RuntimeError("replay source did not remain on its original M0")
        self._assert_tree_unlinked(source_m0)
        source_m0_sha256 = self._package_digest(source_m0)
        if (source_state["version_hashes"]["M0"] != source_m0_sha256 or
                any(int(item.get("round", -1)) >= 1
                    for item in (source_state.get("changelog") or []))):
            raise RuntimeError("replay source package history changed during round 1")

        artifacts = [
            record for record in source_provenance.get("downstream_artifacts", [])
            if isinstance(record, dict) and record.get("tag") == "r1_train"
        ]
        if len(artifacts) != 1:
            raise RuntimeError("replay source must contain exactly one r1_train artifact")
        train_artifact = artifacts[0]
        if (train_artifact.get("status") != "success" or
                train_artifact.get("agent_implementation") != "recuris_agent" or
                train_artifact.get("requested_task_ids") != self.train_ids or
                train_artifact.get("num_trials") != self.a.k or
                train_artifact.get("package_tree_sha256") != source_m0_sha256 or
                train_artifact.get("benchmark_protocol") != self.benchmark_protocol or
                train_artifact.get("benchmark_git_commit") !=
                    source_provenance.get("tau2_git_commit") or
                train_artifact.get("benchmark_inputs") !=
                    source_provenance.get("benchmark_inputs")):
            raise RuntimeError("frozen r1_train artifact treatment metadata is inconsistent")
        if train_artifact.get("worker") != {
                "model": self.a.worker_model,
                "reasoning_effort": self.a.worker_reasoning,
                "llm_args": self.ds.worker_llm_args_record,
        } or train_artifact.get("simulator") != {
                "model": self.a.simulator_model,
                "reasoning_effort": self.a.simulator_reasoning,
                "llm_args": self.ds.simulator_llm_args_record,
        }:
            raise RuntimeError("frozen r1_train used a different downstream treatment")

        results_root = (
            ROOT / "tau2data" / "simulations" / "metaagent" /
            str(self.replay_source_run)
        )
        fingerprint_root = (
            ROOT / "tau2data" / "simulations" / "metaagent_fingerprints" /
            str(self.replay_source_run)
        )
        results_path = self._assert_file_under_unlinked(
            Path(str(train_artifact.get("results") or "")), results_root,
        )
        fingerprint_path = self._assert_file_under_unlinked(
            Path(str(train_artifact.get("fingerprint") or "")), fingerprint_root,
        )
        for path, expected_hash, label in (
                (results_path, train_artifact.get("results_sha256"), "results"),
                (fingerprint_path, train_artifact.get("fingerprint_sha256"),
                 "fingerprint")):
            if (not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash or "")) or
                    self._sha256_file(path) != expected_hash):
                raise RuntimeError(f"frozen train {label} hash is invalid")

        require_embedding = bool(
            (train_artifact.get("fingerprint_embedding") or {}).get("required")
        )
        if not require_embedding:
            raise RuntimeError(
                "replay requires trajectory-embedded fingerprint reconciliation"
            )
        train_result = self.ds._parse(
            results_path, str(fingerprint_path), self.train_ids, self.a.k,
            expected_agent="recuris_agent", require_fingerprint=True,
            expected_benchmark_commit=source_provenance["tau2_git_commit"],
            benchmark_inputs=source_provenance["benchmark_inputs"],
            require_embedded_fingerprint=True,
        )
        train_result.artifact = copy.deepcopy(train_artifact)
        if (train_result.matrix !=
                (source_state.get("train_history") or {}).get("1")):
            raise RuntimeError("reparsed train matrix differs from sealed source state")
        if train_artifact.get("trial_seeds") != train_result.seeds:
            raise RuntimeError("reparsed train seed map differs from artifact provenance")
        if not train_result.failing:
            raise RuntimeError("frozen source has no failing tasks to diagnose")

        source_round = source_dir / "round_1"
        evidence_sources = {
            "round_1/state.md": source_round / "state.md",
            "round_1/evidence.md": source_round / "evidence.md",
        }
        for task in sorted(train_result.failing, key=_task_sort_key):
            evidence_sources[f"round_1/evidence/task_{task}.md"] = (
                source_round / "evidence" / f"task_{task}.md"
            )
        actual_task_files = {
            path.name for path in (source_round / "evidence").glob("task_*.md")
            if path.is_file()
        }
        expected_task_files = {
            f"task_{task}.md" for task in train_result.failing
        }
        if actual_task_files != expected_task_files:
            raise RuntimeError("source evidence files do not match reparsed failing tasks")
        copied_evidence: dict[str, str] = {}
        for relative, source in evidence_sources.items():
            source = self._assert_file_under_unlinked(source, source_dir)
            if materialize_evidence:
                destination = self.run_dir / relative
                copied_evidence[relative] = self._copy_frozen_file(
                    source, destination,
                )
            else:
                copied_evidence[relative] = self._sha256_file(source)

        sealed_hashes = {
            label: self._sha256_file(path)
            for label, path in required.items()
        }
        postmortem = source_dir / "POSTMORTEM_meta_stream_idle.md"
        if postmortem.is_file() and not self._is_linklike(postmortem):
            sealed_hashes["postmortem_annotation"] = self._sha256_file(postmortem)
        matrix_sha256 = hashlib.sha256(json.dumps(
            train_result.matrix, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        seeds_sha256 = hashlib.sha256(json.dumps(
            train_result.seeds, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        failure_owners = copy.deepcopy(train_result.failure_owners or {})
        failure_owners_sha256 = hashlib.sha256(json.dumps(
            failure_owners, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        snapshot = {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "mode": "frozen-train-evidence+fresh-contemporaneous-gates",
            "source_run_id": str(self.replay_source_run),
            "source_round": 1,
            "source_path": str(source_dir),
            "source_outcome": source_outcome,
            "source_sealed_files": sealed_hashes,
            "source_config_sha256": self._sha256_file(required["provenance"]),
            "source_runtime_tree_sha256": (
                source_provenance.get("runtime_sources") or {}
            ).get("tree_sha256"),
            "destination_runtime_tree_sha256": (
                current_provenance.get("runtime_sources") or {}
            ).get("tree_sha256"),
            "changed_runtime_files": changed_runtime,
            "allowed_runtime_changes": sorted(allowed_runtime_changes),
            "base_package": {
                "source": str(source_m0),
                "tree_sha256": source_m0_sha256,
            },
            "train_artifact": {
                key: copy.deepcopy(train_artifact.get(key))
                for key in (
                    "index", "token", "tag", "status", "results",
                    "results_sha256", "fingerprint", "fingerprint_sha256",
                    "package_tree_sha256", "agent_implementation",
                    "requested_task_ids", "num_trials", "base_seed",
                    "trial_seeds", "benchmark_git_commit",
                    "benchmark_protocol", "benchmark_inputs", "worker",
                    "simulator", "process_attempts",
                    "fingerprint_reconciliation", "fingerprint_embedding",
                )
            },
            "reparsed_proof": {
                "matrix_sha256": matrix_sha256,
                "seeds_sha256": seeds_sha256,
                "failing_tasks": sorted(train_result.failing, key=_task_sort_key),
                "opportunity_summary": train_result.opportunity_summary,
                "failure_owners": failure_owners,
                "failure_owners_sha256": failure_owners_sha256,
                "benchmark_git_commit": train_result.benchmark_git_commit,
                "benchmark_input_sha256":
                    train_result.benchmark_input_sha256,
            },
            "copied_evidence": copied_evidence,
            "simulation_accounting": {
                "source_run_total": source_state.get("sims"),
                "reused_train_simulations": len(self.train_ids) * self.a.k,
                "destination_initial_charge": 0,
                "future_candidate_gates": "fresh-and-charged",
            },
        }
        return snapshot, train_result, source_state, source_m0

    @staticmethod
    def _replay_snapshot_sha256(snapshot: dict) -> str:
        return hashlib.sha256(json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def preflight_replay(self) -> None:
        """Validate replay and profile inputs without exposing or writing them."""
        if not self.replay_source_run:
            return
        source_lock = self._lock_inactive_run(self.replay_source_dir)
        try:
            snapshot, train_result, _source_state, source_m0 = (
                self._build_replay_snapshot(materialize_evidence=False)
            )
            if getattr(self, "generation_profile_spec", None) is not None:
                rnd = int(self.replay_source_round)
                self._assert_generation_profile_resume_safe(rnd)
                self._profile_source_proof(
                    rnd,
                    self.replay_source_dir / f"round_{rnd}",
                    train_result,
                )
                self._build_capability_disclosure(
                    self.generation_profile_spec
                )
                manifest_path = source_m0 / "manifest.yaml"
                if (not manifest_path.is_file() or
                        self._is_linklike(manifest_path)):
                    raise RuntimeError(
                        "generation profile source M0 manifest is missing"
                    )
            self._replay_preflight_snapshot_sha256 = (
                self._replay_snapshot_sha256(snapshot)
            )
        finally:
            self._unlock_inactive_run(source_lock)

    def prepare_replay(self) -> None:
        """Install/verify source M0 and cache the exact frozen train result."""
        if not self.replay_source_run:
            return
        source_lock = self._lock_inactive_run(self.replay_source_dir)
        try:
            snapshot, train_result, source_state, source_m0 = (
                self._build_replay_snapshot(materialize_evidence=False)
            )
            preflight_sha256 = getattr(
                self, "_replay_preflight_snapshot_sha256", None,
            )
            if (preflight_sha256 is not None and
                    self._replay_snapshot_sha256(snapshot) !=
                    preflight_sha256):
                raise RuntimeError(
                    "replay source changed after model-free preflight"
                )
            for relative, expected_sha256 in (
                    snapshot["copied_evidence"].items()):
                source = self._assert_file_under_unlinked(
                    self.replay_source_dir / relative,
                    self.replay_source_dir,
                )
                actual_sha256 = self._copy_frozen_file(
                    source, self.run_dir / relative,
                )
                if actual_sha256 != expected_sha256:
                    raise RuntimeError(
                        f"replay evidence changed during copy: {relative}"
                    )
            replay_path = self.run_dir / "replay_source.json"
            if replay_path.exists():
                recorded = json.loads(replay_path.read_text(encoding="utf-8"))
                if recorded != snapshot:
                    raise RuntimeError(
                        "frozen replay source or copied evidence changed on resume"
                    )
            else:
                self._write_json_atomic(replay_path, snapshot)
            provenance = json.loads(
                self.provenance_path.read_text(encoding="utf-8")
            )
            if (provenance.get("replay_source") is not None and
                    provenance["replay_source"] != snapshot):
                raise RuntimeError("provenance replay snapshot changed on resume")
            provenance["replay_source"] = snapshot
            self._write_json_atomic(self.provenance_path, provenance)

            destination_m0 = self.versions_dir / "M0"
            source_digest = snapshot["base_package"]["tree_sha256"]
            if self.state.get("best") is None:
                if destination_m0.exists():
                    raise RuntimeError("uninitialized replay already contains M0")
                temp_name = f".M0.replay.{uuid.uuid4().hex}.tmp"
                temp_dir = self.versions_dir / temp_name
                journal_path = (
                    self.run_dir / f"commit_M0_replay_{uuid.uuid4().hex[:10]}.json"
                )
                replay_record_sha256 = self._sha256_file(replay_path)
                changelog_entry = copy.deepcopy(
                    (source_state.get("changelog") or [{
                        "round": 0,
                        "summary": "frozen source M0",
                        "net_pp": 0.0,
                    }])[0]
                )
                changelog_entry["round"] = 0
                finalization = {
                    "outcome": "replay-base",
                    "ledger": {
                        "round": 0,
                        "phase": "frozen-evidence-replay",
                        "source_run_id": str(self.replay_source_run),
                        "source_round": 1,
                        "replay_source_sha256": replay_record_sha256,
                        "downstream_simulations_charged": 0,
                    },
                }
                journal = {
                    "status": "prepared",
                    "round": 0,
                    "version": "M0",
                    "temp": temp_name,
                    "tree_sha256": source_digest,
                    "best_dev": copy.deepcopy(source_state["best_dev"]),
                    "best_dev_seeds":
                        copy.deepcopy(source_state["best_dev_seeds"]),
                    "best_dev_benchmark_git_commit":
                        source_state["best_dev_benchmark_git_commit"],
                    "best_dev_benchmark_input_sha256":
                        source_state["best_dev_benchmark_input_sha256"],
                    "bare_dev_reference":
                        copy.deepcopy(source_state.get("bare_dev_reference")),
                    "changelog_entry": changelog_entry,
                    "finalization": finalization,
                    "created_at": _now(),
                }
                self._write_json_atomic(journal_path, journal)
                shutil.copytree(source_m0, temp_dir)
                if self._package_digest(temp_dir) != source_digest:
                    raise RuntimeError("replay M0 copy failed its package digest")
                temp_dir.replace(destination_m0)
                self.state.update({
                    "round_done": -1,
                    "best": str(destination_m0),
                    "best_dev": journal["best_dev"],
                    "best_dev_seeds": journal["best_dev_seeds"],
                    "best_dev_benchmark_git_commit":
                        journal["best_dev_benchmark_git_commit"],
                    "best_dev_benchmark_input_sha256":
                        journal["best_dev_benchmark_input_sha256"],
                    "bare_dev_reference": journal["bare_dev_reference"],
                    "sims": 0,
                    "blocked": [],
                    "changelog": [changelog_entry],
                    "train_history": {},
                    "version_hashes": {"M0": source_digest},
                    "round_outcomes": {},
                })
                self.save_state()
                journal["status"] = "package_committed"
                self._write_json_atomic(journal_path, journal)
                self._finalize_round(0, finalization)
                journal["status"] = "complete"
                journal["completed_at"] = _now()
                self._write_json_atomic(journal_path, journal)
            else:
                if (not destination_m0.is_dir() or
                        self._package_digest(destination_m0) != source_digest):
                    raise RuntimeError("destination replay M0 differs from frozen source")
                if self.state.get("sims", 0) < 0:
                    raise RuntimeError("invalid replay simulation accounting")
            self._replay_train_result = train_result
            self._replay_snapshot = snapshot
            self._validate_state()
        finally:
            self._unlock_inactive_run(source_lock)

    def _prepare_candidate(self, rnd: int, source: str | Path | None = None):
        self.cand_dir = self.package_root / f"candidate_r{rnd}"
        self._safe_rmtree(self.cand_dir)
        if source is not None:
            source_path = Path(source).resolve()
            self._assert_tree_unlinked(source_path)
            shutil.copytree(source_path, self.cand_dir)
        else:
            for kind in ("knowledge", "procedure", "action_result"):
                (self.cand_dir / "em" / kind).mkdir(parents=True, exist_ok=True)
        return self.cand_dir

    @staticmethod
    def _canonical_json_sha256(value: object) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _generation_profile_paths(self, rnd: int, rdir: Path) -> dict:
        visible_root = rdir / "generation_profile_visible"
        control_root = rdir / "generation_profile_control"
        return {
            "visible_root": visible_root,
            "control_root": control_root,
            "evidence": visible_root / "evidence.md",
            "policy": visible_root / "policy.md",
            "capability": visible_root / "capability.md",
            "plan_protocol": visible_root / "plan_protocol.md",
            "patch_protocol": visible_root / "patch_protocol.md",
            "tool_capabilities": visible_root / "tool_capabilities.json",
            "spec": control_root / "spec.json",
            "manifest": control_root / "input_manifest.json",
            "accepted_alias_plan": control_root / "accepted_alias_plan.json",
            "mapped_plan": control_root / "mapped_plan.json",
            "activation_query_proof":
                control_root / "activation_query_proof.json",
            "attempts": control_root / "attempts",
        }

    def _write_frozen_profile_bytes(self, path: Path, content: bytes) -> None:
        if path.exists():
            if not path.is_file() or path.read_bytes() != content:
                raise RuntimeError(
                    f"frozen generation profile artifact changed: {path}"
                )
            return
        self._write_bytes_atomic(path, content)

    def _profile_source_proof(
            self, rnd: int, rdir: Path, train_res=None) -> dict:
        spec = self.generation_profile_spec
        if spec is None:
            raise RuntimeError("generation profile source proof requested without spec")
        source = spec["source"]
        matrix = (
            getattr(train_res, "matrix", None)
            if train_res is not None else
            (self.state.get("train_history") or {}).get(str(rnd))
        )
        if not isinstance(matrix, dict):
            raise RuntimeError(
                "generation profile cannot verify the frozen train matrix"
            )
        for task, expected_row in source["task_matrix"].items():
            if list(matrix.get(task) or []) != list(expected_row):
                raise RuntimeError(
                    f"generation profile source matrix mismatch for task {task}"
                )
        if train_res is not None:
            actual_failing = {str(task) for task in train_res.failing}
            missing = {
                spec["alias_to_task"][alias]
                for alias in spec["plan_scope"]["failing_tasks"]
            } - actual_failing
            if missing:
                raise RuntimeError(
                    "generation profile plan scope contains non-failing source "
                    f"tasks: {sorted(missing)}"
                )

        evidence_proof: dict[str, dict] = {}
        for task, expected_hash in source["evidence_sha256"].items():
            path = rdir / "evidence" / f"task_{task}.md"
            if not path.is_file() or self._is_linklike(path):
                raise RuntimeError(
                    f"generation profile source evidence is missing: {path}"
                )
            actual_hash = self._sha256_file(path)
            if actual_hash != expected_hash:
                raise RuntimeError(
                    f"generation profile source evidence hash mismatch for task {task}"
                )
            evidence_proof[task] = {
                "path": str(path),
                "sha256": actual_hash,
            }

        policy_path = (
            TAU2_ROOT / "data" / "tau2" /
            "domains" / self.domain / "policy.md"
        )
        if (not policy_path.is_file() or self._is_linklike(policy_path) or
                self._sha256_file(policy_path) != source["policy_sha256"]):
            raise RuntimeError(
                "generation profile full source policy failed its locked SHA-256"
            )
        return {
            "task_matrix": copy.deepcopy(source["task_matrix"]),
            "evidence": evidence_proof,
            "policy": {
                "path": str(policy_path),
                "sha256": source["policy_sha256"],
            },
        }

    def _build_generation_profile_manifest(
            self, rnd: int, rdir: Path, visible_hashes: dict) -> dict:
        spec = self.generation_profile_spec
        if spec is None:
            raise RuntimeError("cannot build generation profile manifest without spec")
        best = Path(str(self.state.get("best") or ""))
        manifest_path = best / "manifest.yaml"
        if not best.is_dir() or not manifest_path.is_file():
            raise RuntimeError(
                "generation profile requires a current M0 manifest"
            )
        best_name = best.name
        base_tree_sha256 = (
            self.state.get("version_hashes", {}).get(best_name)
        )
        if (not SHA256_RE.fullmatch(str(base_tree_sha256 or "")) or
                self._package_digest(best) != base_tree_sha256):
            raise RuntimeError(
                "generation profile base package failed its frozen tree hash"
            )
        prompt_templates = {
            "diagnosis_sha256": hashlib.sha256(
                PROFILE_DIAGNOSIS_PROMPT_TEMPLATE.encode("utf-8")
            ).hexdigest(),
            "patch_sha256": hashlib.sha256(
                PROFILE_PATCH_PROMPT_TEMPLATE.encode("utf-8")
            ).hexdigest(),
        }
        substantive = {
            "visible_artifacts": copy.deepcopy(visible_hashes),
            "base_manifest_sha256": self._sha256_file(manifest_path),
            "base_package_tree_sha256": base_tree_sha256,
            "prompt_templates": prompt_templates,
        }
        substantive_sha256 = self._canonical_json_sha256(substantive)
        treatment = {
            "raw_spec_sha256": self.generation_profile_spec_sha256,
            "canonical_spec_sha256":
                self.generation_profile_canonical_sha256,
            "substantive_input_sha256": substantive_sha256,
            "source": copy.deepcopy(spec["source"]),
            "replay": copy.deepcopy(spec["replay"]),
            "tool_allowlist": copy.deepcopy(spec["tool_allowlist"]),
            "attempt_timeouts": copy.deepcopy(spec["attempt_timeouts"]),
            "plan_scope": copy.deepcopy(spec["plan_scope"]),
            "alias_to_task": copy.deepcopy(spec["alias_to_task"]),
            "plan_contract": copy.deepcopy(spec["plan_contract"]),
            "activation_queries":
                copy.deepcopy(spec["activation_queries"]),
            "information_boundary":
                copy.deepcopy(spec["information_boundary"]),
        }
        comparison_lock = spec.get("comparison_lock")
        comparison_arm = getattr(
            self, "generation_profile_comparison_arm", None
        )
        if comparison_lock is not None:
            if not isinstance(comparison_arm, dict):
                raise RuntimeError(
                    "generation profile comparison lock lacks a matched arm"
                )
            treatment["comparison_lock"] = copy.deepcopy(comparison_lock)
            treatment["matched_comparison_arm"] = copy.deepcopy(
                comparison_arm
            )
        manifest = {
            "schema_version": GENERATION_PROFILE_SCHEMA_VERSION,
            "profile_id": spec["profile_id"],
            "domain": spec["domain"],
            "round": rnd,
            "raw_spec_sha256": self.generation_profile_spec_sha256,
            "canonical_spec_sha256":
                self.generation_profile_canonical_sha256,
            "substantive": substantive,
            "substantive_input_sha256": substantive_sha256,
            "treatment_sha256": self._canonical_json_sha256(treatment),
            "replay": copy.deepcopy(spec["replay"]),
            "source": copy.deepcopy(spec["source"]),
            "tool_allowlist": copy.deepcopy(spec["tool_allowlist"]),
            "attempt_timeouts": copy.deepcopy(spec["attempt_timeouts"]),
            "plan_scope": copy.deepcopy(spec["plan_scope"]),
            "alias_to_task": copy.deepcopy(spec["alias_to_task"]),
            "plan_contract": copy.deepcopy(spec["plan_contract"]),
            "activation_queries":
                copy.deepcopy(spec["activation_queries"]),
            "information_boundary":
                copy.deepcopy(spec["information_boundary"]),
        }
        if comparison_lock is not None:
            manifest["comparison_lock"] = copy.deepcopy(comparison_lock)
            manifest["matched_comparison_arm"] = copy.deepcopy(
                comparison_arm
            )
        return manifest

    def _generation_profile_provenance_record(
            self, rnd: int, paths: dict, manifest: dict,
            visible_hashes: dict) -> dict:
        return {
            "schema_version": GENERATION_PROFILE_SCHEMA_VERSION,
            "round": rnd,
            "profile_id": manifest["profile_id"],
            "source_spec_path": str(self.generation_profile_spec_path),
            "expected_raw_spec_sha256":
                self.generation_profile_spec_sha256,
            "materialized_spec_path": str(paths["spec"]),
            "materialized_spec_sha256":
                self._sha256_file(paths["spec"]),
            "manifest_path": str(paths["manifest"]),
            "manifest_sha256": self._sha256_file(paths["manifest"]),
            "substantive_input_sha256":
                manifest["substantive_input_sha256"],
            "treatment_sha256": manifest["treatment_sha256"],
            "visible_artifacts": {
                name: {
                    "path": str(paths[name]),
                    "sha256": digest,
                }
                for name, digest in visible_hashes.items()
            },
        }

    def _materialize_generation_profile(
            self, rnd: int, rdir: Path, train_res) -> dict | None:
        spec = getattr(self, "generation_profile_spec", None)
        if spec is None:
            return None
        self._profile_source_proof(rnd, rdir, train_res)
        paths = self._generation_profile_paths(rnd, rdir)
        for root in (paths["visible_root"], paths["control_root"],
                     paths["attempts"]):
            self._assert_no_linklike(root, allow_missing=True)
            root.mkdir(parents=True, exist_ok=True)
        self._write_frozen_profile_bytes(
            paths["spec"], self.generation_profile_spec_bytes,
        )
        visible = spec["visible_inputs"]
        for logical, source_key in (
                ("evidence", "evidence_md"),
                ("policy", "policy_md"),
                ("plan_protocol", "plan_protocol_md"),
                ("patch_protocol", "patch_protocol_md")):
            self._write_frozen_profile_bytes(
                paths[logical], visible[source_key].encode("utf-8"),
            )
        disclosure = self._materialize_capability_disclosure(
            rnd, rdir, profile=spec, profile_root=paths["visible_root"],
        )
        if (disclosure["manual_path"] != paths["capability"] or
                disclosure["tool_capabilities_path"] !=
                paths["tool_capabilities"]):
            raise RuntimeError(
                "generation profile capability materialization path mismatch"
            )
        visible_hashes = {
            name: self._sha256_file(paths[name])
            for name in (
                "evidence", "policy", "capability", "plan_protocol",
                "patch_protocol", "tool_capabilities",
            )
        }
        manifest = self._build_generation_profile_manifest(
            rnd, rdir, visible_hashes,
        )
        if paths["manifest"].exists():
            existing = json.loads(
                paths["manifest"].read_text(encoding="utf-8")
            )
            if existing != manifest:
                raise RuntimeError(
                    "frozen generation profile input manifest changed on resume"
                )
        else:
            self._write_json_atomic(paths["manifest"], manifest)
        record = self._generation_profile_provenance_record(
            rnd, paths, manifest, visible_hashes,
        )
        provenance = json.loads(
            self.provenance_path.read_text(encoding="utf-8")
        )
        records = provenance.setdefault("generation_profiles", {})
        previous = records.get(str(rnd))
        if previous is not None and previous != record:
            raise RuntimeError(
                "generation profile provenance changed on resume"
            )
        records[str(rnd)] = record
        self._write_json_atomic(self.provenance_path, provenance)
        alias_to_task = copy.deepcopy(spec["alias_to_task"])
        scope = spec["plan_scope"]
        return {
            "spec": spec,
            "paths": paths,
            "manifest": manifest,
            "record": record,
            "capability_disclosure": disclosure,
            "alias_to_task": alias_to_task,
            "failing_aliases": set(scope["failing_tasks"]),
            "failing_tasks": {
                alias_to_task[alias] for alias in scope["failing_tasks"]
            },
            "required_alias_coverage":
                set(scope["required_task_coverage"]),
            "required_task_coverage": {
                alias_to_task[alias]
                for alias in scope["required_task_coverage"]
            },
            "forced_alias_owners": copy.deepcopy(scope["forced_owners"]),
            "forced_owners": {
                alias_to_task[alias]: owner
                for alias, owner in scope["forced_owners"].items()
            },
            "forced_alias_opportunities":
                copy.deepcopy(scope["forced_opportunities"]),
            "forced_opportunities": {
                alias_to_task[alias]: opportunity
                for alias, opportunity in
                scope["forced_opportunities"].items()
            },
            "diagnosis_timeouts":
                list(spec["attempt_timeouts"]["diagnosis_seconds"]),
            "patch_timeouts":
                list(spec["attempt_timeouts"]["patch_seconds"]),
        }

    def _assert_generation_profile_model_surface_opaque(
            self, prompt: str, readable_paths: list[Path]) -> None:
        """Reject any profiled model surface that contains a raw task id."""
        spec = getattr(self, "generation_profile_spec", None)
        if spec is None:
            return
        raw_ids = sorted(
            {str(value) for value in spec["alias_to_task"].values()},
            key=len, reverse=True,
        )

        def reject_text(value: str, label: str) -> None:
            leaked = [task for task in raw_ids if task in value]
            if leaked:
                raise RuntimeError(
                    f"locked profile model surface {label} discloses raw "
                    f"task id(s) {leaked}"
                )

        reject_text(prompt, "prompt")
        seen_files: set[Path] = set()
        for raw_path in readable_paths:
            path = Path(raw_path)
            try:
                display = path.resolve().relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                display = str(path.resolve()).replace("\\", "/")
            reject_text(display, "path")
            files = (
                [path] if path.is_file() else
                [item for item in sorted(path.rglob("*")) if item.is_file()]
                if path.is_dir() else []
            )
            for file_path in files:
                resolved = file_path.resolve()
                if resolved in seen_files:
                    continue
                seen_files.add(resolved)
                try:
                    relative = resolved.relative_to(ROOT.resolve()).as_posix()
                except ValueError:
                    relative = str(resolved).replace("\\", "/")
                reject_text(relative, "artifact path")
                content = file_path.read_bytes()
                leaked = [
                    task for task in raw_ids
                    if task.encode("utf-8") in content
                ]
                if leaked:
                    raise RuntimeError(
                        "locked profile readable artifact discloses raw "
                        f"task id(s) {leaked}: {relative}"
                    )

    def _audit_generation_profile_d_plan_raw_ids(
            self, generation_context: dict, *, plan_path: Path,
            rnd: int, attempt: int) -> None:
        """Seal a profiled D arm before parsing or retrying a contaminated plan."""
        if not plan_path.is_file():
            return
        plan_bytes = plan_path.read_bytes()
        raw_ids = sorted(
            {
                str(value)
                for value in generation_context["alias_to_task"].values()
            },
            key=len, reverse=True,
        )
        leaked = [
            task for task in raw_ids
            if task.encode("utf-8") in plan_bytes
        ]
        if not leaked:
            return
        contamination_path = (
            generation_context["paths"]["attempts"] /
            f"d{attempt}_raw_id_violation.json"
        )
        self._write_json_atomic(
            contamination_path, {
                "schema_version": 1,
                "round": rnd,
                "attempt": attempt,
                "phase": "D",
                "fatal": True,
                "retry_allowed": False,
                "violation": "raw-task-id",
                "raw_task_ids": leaked,
                "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
            },
        )
        raise ProfileArmIneligible("D")

    def _validate_and_map_generation_profile_plan(
            self, alias_plan: object, generation_context: dict
            ) -> tuple[dict | None, list[str]]:
        """Validate the locked action contract on aliases, then map ids."""
        errors: list[str] = []
        if not isinstance(alias_plan, dict):
            return None, ["profile plan top level must be a JSON object"]
        clusters = alias_plan.get("clusters")
        if not isinstance(clusters, list) or not clusters:
            return None, ["profile plan.clusters must be a non-empty list"]
        contract = generation_context["spec"]["plan_contract"]
        if len(clusters) > contract["max_clusters"]:
            errors.append(
                "profile plan has too many clusters: "
                f"{len(clusters)} > {contract['max_clusters']}"
            )
        raw_ids = {
            str(value)
            for value in generation_context["alias_to_task"].values()
        }
        serialized = json.dumps(
            alias_plan, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        leaked = sorted(task for task in raw_ids if task in serialized)
        if leaked:
            errors.append(
                f"profile alias plan discloses raw task id(s) {leaked}"
            )

        rules = {
            rule["rule_id"]: rule
            for rule in contract["patch_cluster_rules"]
        }
        rule_counts = {rule_id: 0 for rule_id in rules}
        cluster_rules: list[str | None] = []
        evidence_by_rule: dict[str, list[set[str]]] = {
            rule_id: [] for rule_id in rules
        }
        allowed_aliases = set(generation_context["failing_aliases"])
        covered_aliases: set[str] = set()
        non_patch = ma_plan_schema.NON_PATCH_OWNERS | {"CEILING"}
        non_patch_cluster_count = 0

        def fix_matches_rule(fix: object, fix_rule: dict) -> bool:
            if not isinstance(fix, dict):
                return False
            expected_keys = (
                {"action"} |
                set(fix_rule["exact_fields"]) |
                set(fix_rule["required_text_fields"])
            )
            return (
                fix.get("action") == fix_rule["action"] and
                set(fix) == expected_keys and
                all(
                    fix.get(key) == expected
                    for key, expected in
                    fix_rule["exact_fields"].items()
                ) and
                all(
                    isinstance(fix.get(field), str) and
                    bool(fix[field].strip())
                    for field in fix_rule["required_text_fields"]
                )
            )

        def fixes_match_rule(fixes: object, rule: dict) -> bool:
            return (
                isinstance(fixes, list) and
                rule["min_fixes"] <= len(fixes) <= rule["max_fixes"] and
                all(
                    sum(
                        fix_matches_rule(fix, fix_rule)
                        for fix_rule in rule["fix_rules"]
                    ) == 1
                    for fix in fixes
                )
            )

        for cluster_index, cluster in enumerate(clusters):
            label = f"profile clusters[{cluster_index}]"
            if not isinstance(cluster, dict):
                errors.append(f"{label} must be an object")
                cluster_rules.append(None)
                continue
            evidence = cluster.get("evidence_tasks")
            evidence_aliases: set[str] = set()
            if (not isinstance(evidence, list) or not evidence or
                    any(not isinstance(alias, str) for alias in evidence)):
                errors.append(
                    f"{label}.evidence_tasks must be non-empty case aliases"
                )
            else:
                evidence_aliases = set(evidence)
                if len(evidence_aliases) != len(evidence):
                    errors.append(
                        f"{label}.evidence_tasks contains duplicates"
                    )
                unknown = evidence_aliases - allowed_aliases
                if unknown:
                    errors.append(
                        f"{label}.evidence_tasks has unknown/non-failing "
                        f"case aliases {sorted(unknown)}"
                    )
                covered_aliases.update(evidence_aliases & allowed_aliases)
            component = str(cluster.get("component") or "")
            fixes = cluster.get("fixes")
            if component in non_patch:
                non_patch_cluster_count += 1
                cluster_rules.append(None)
                if not contract["allow_non_patch_owners"]:
                    errors.append(
                        f"{label} uses a non-patch owner forbidden by contract"
                    )
                if fixes != []:
                    errors.append(
                        f"{label} non-patch owner requires fixes=[]"
                    )
                continue
            candidate_rules = [
                rule for rule in rules.values()
                if component == rule["component"] and
                str(cluster.get("failure_mode") or "") in
                set(rule["failure_modes"])
            ]
            matching_rules = [
                rule for rule in candidate_rules
                if fixes_match_rule(fixes, rule)
            ]
            if len(matching_rules) != 1:
                errors.append(
                    f"{label} matches {len(matching_rules)} complete profile "
                    "patch rules after applying the exact fix whitelist; "
                    "expected exactly one"
                )
                cluster_rules.append(None)
                continue
            rule = matching_rules[0]
            rule_id = rule["rule_id"]
            cluster_rules.append(rule_id)
            rule_counts[rule_id] += 1
            evidence_by_rule[rule_id].append(evidence_aliases)
            if not isinstance(fixes, list):
                errors.append(f"{label}.fixes must be a list")
                continue
            if not rule["min_fixes"] <= len(fixes) <= rule["max_fixes"]:
                errors.append(
                    f"{label}.fixes count {len(fixes)} is outside locked "
                    f"range {rule['min_fixes']}..{rule['max_fixes']}"
                )
            for fix_index, fix in enumerate(fixes):
                fix_label = f"{label}.fixes[{fix_index}]"
                if not isinstance(fix, dict):
                    errors.append(f"{fix_label} must be an object")
                    continue
                matched = False
                for fix_rule in rule["fix_rules"]:
                    if fix_matches_rule(fix, fix_rule):
                        matched = True
                        break
                if not matched:
                    errors.append(
                        f"{fix_label} is outside the locked action whitelist "
                        f"for rule {rule_id!r}"
                    )
        for rule_id, rule in rules.items():
            count = rule_counts[rule_id]
            if not rule["min_clusters"] <= count <= rule["max_clusters"]:
                errors.append(
                    f"profile rule {rule_id!r} cluster count {count} is "
                    f"outside {rule['min_clusters']}..{rule['max_clusters']}"
                )
        if non_patch_cluster_count and len(clusters) != 1:
            errors.append(
                "a non-patch owner/CEILING must be the plan's only cluster"
            )
        for companion in contract["companion_requirements"]:
            antecedents = evidence_by_rule[companion["if_rule"]]
            if not antecedents:
                continue
            companions = evidence_by_rule[companion["requires_rule"]]
            if not companions:
                errors.append(
                    f"profile rule {companion['if_rule']!r} requires "
                    f"companion rule {companion['requires_rule']!r}"
                )
                continue
            if companion["same_evidence_cases"]:
                for antecedent in antecedents:
                    if not any(
                            antecedent == candidate
                            for candidate in companions):
                        errors.append(
                            f"profile companion {companion['if_rule']!r} -> "
                            f"{companion['requires_rule']!r} must use the same "
                            "evidence case aliases"
                        )
        missing = (
            set(generation_context["required_alias_coverage"]) -
            covered_aliases
        )
        if missing:
            errors.append(
                "locked generation profile requires plan coverage for case "
                f"aliases {sorted(missing)}"
            )
        if errors:
            return None, errors
        mapped_plan = copy.deepcopy(alias_plan)
        alias_to_task = generation_context["alias_to_task"]
        for cluster in mapped_plan["clusters"]:
            cluster["evidence_tasks"] = [
                alias_to_task[alias]
                for alias in cluster["evidence_tasks"]
            ]
        generation_context["_last_plan_rule_matches"] = list(cluster_rules)
        generation_context["matched_rule_ids"] = sorted({
            rule_id for rule_id in cluster_rules
            if rule_id is not None
        })
        return mapped_plan, []

    def _freeze_generation_profile_plan_outputs(
            self, generation_context: dict, alias_plan_path: Path,
            alias_plan: dict, mapped_plan: dict) -> dict:
        paths = generation_context["paths"]
        alias_bytes = alias_plan_path.read_bytes()
        self._write_frozen_profile_bytes(
            paths["accepted_alias_plan"], alias_bytes
        )
        if paths["mapped_plan"].exists():
            existing_mapped = json.loads(
                paths["mapped_plan"].read_text(encoding="utf-8")
            )
            if existing_mapped != mapped_plan:
                raise RuntimeError(
                    "frozen mapped profile plan changed"
                )
        else:
            self._write_json_atomic(paths["mapped_plan"], mapped_plan)
        record = {
            "alias_plan_path": str(alias_plan_path),
            "alias_plan_sha256": self._sha256_file(alias_plan_path),
            "accepted_alias_plan_path":
                str(paths["accepted_alias_plan"]),
            "accepted_alias_plan_sha256":
                self._sha256_file(paths["accepted_alias_plan"]),
            "mapped_plan_path": str(paths["mapped_plan"]),
            "mapped_plan_sha256": self._sha256_file(paths["mapped_plan"]),
            "alias_to_task": copy.deepcopy(
                generation_context["alias_to_task"]
            ),
            "alias_plan_canonical_sha256":
                self._canonical_json_sha256(alias_plan),
            "mapped_plan_canonical_sha256":
                self._canonical_json_sha256(mapped_plan),
            "matched_rule_ids": copy.deepcopy(
                generation_context.get("matched_rule_ids") or []
            ),
        }
        generation_context["plan_outputs"] = record
        generation_context["alias_plan"] = copy.deepcopy(alias_plan)
        generation_context["mapped_plan"] = copy.deepcopy(mapped_plan)
        return record

    def _verify_generation_profile_plan_outputs(
            self, generation_context: dict) -> dict:
        record = generation_context.get("plan_outputs")
        if not isinstance(record, dict):
            raise RuntimeError(
                "locked generation profile has no accepted plan outputs"
            )
        paths = generation_context["paths"]
        expected_paths = {
            "alias_plan_path": str(
                self.run_dir /
                f"round_{generation_context['manifest']['round']}" /
                "plan.json"
            ),
            "accepted_alias_plan_path":
                str(paths["accepted_alias_plan"]),
            "mapped_plan_path": str(paths["mapped_plan"]),
        }
        if (any(record.get(key) != value
                for key, value in expected_paths.items()) or
                record.get("alias_to_task") !=
                generation_context["alias_to_task"]):
            raise RuntimeError(
                "locked generation profile plan-output paths/mapping changed"
            )
        alias_path = Path(record["alias_plan_path"])
        if (
                not alias_path.is_file() or
                not paths["accepted_alias_plan"].is_file() or
                alias_path.read_bytes() !=
                paths["accepted_alias_plan"].read_bytes() or
                self._sha256_file(alias_path) !=
                record["alias_plan_sha256"] or
                not paths["mapped_plan"].is_file() or
                self._sha256_file(paths["mapped_plan"]) !=
                record["mapped_plan_sha256"]):
            raise RuntimeError(
                "locked generation profile plan outputs failed integrity"
            )
        alias_plan = json.loads(alias_path.read_text(encoding="utf-8"))
        mapped_plan, errors = self._validate_and_map_generation_profile_plan(
            alias_plan, generation_context
        )
        persisted_mapped = json.loads(
            paths["mapped_plan"].read_text(encoding="utf-8")
        )
        if errors or mapped_plan != persisted_mapped:
            raise RuntimeError(
                f"locked generation profile plan mapping changed: {errors}"
            )
        expected = {
            **record,
            "alias_plan_sha256": self._sha256_file(alias_path),
            "accepted_alias_plan_sha256":
                self._sha256_file(paths["accepted_alias_plan"]),
            "mapped_plan_sha256": self._sha256_file(paths["mapped_plan"]),
            "alias_plan_canonical_sha256":
                self._canonical_json_sha256(alias_plan),
            "mapped_plan_canonical_sha256":
                self._canonical_json_sha256(mapped_plan),
            "matched_rule_ids": copy.deepcopy(
                generation_context.get("matched_rule_ids") or []
            ),
        }
        if expected != record:
            raise RuntimeError(
                "locked generation profile plan-output record changed"
            )
        return record

    def _probe_generation_profile_activation_queries(
            self, generation_context: dict, plan: dict) -> dict:
        """Run locked visible queries through the real lexical selector."""
        self._verify_generation_profile_plan_outputs(generation_context)
        matched_rules = set(
            generation_context["plan_outputs"]["matched_rule_ids"]
        )
        query_spec = generation_context["spec"]["activation_queries"]
        active_retrieval_rules = sorted(matched_rules & set(query_spec))
        report = {
            "schema_version": 1,
            "offline": True,
            "network_calls": 0,
            "model_calls": 0,
            "matched_retrieval_rules": active_retrieval_rules,
            "probes": [],
        }
        if not active_retrieval_rules:
            report["passed"] = True
            return report

        rules = {
            rule["rule_id"]: rule
            for rule in generation_context["spec"]["plan_contract"][
                "patch_cluster_rules"
            ]
        }
        companion_rules: dict[str, set[str]] = {
            rule_id: set() for rule_id in rules
        }
        for companion in generation_context["spec"]["plan_contract"][
                "companion_requirements"]:
            left = companion["if_rule"]
            right = companion["requires_rule"]
            companion_rules[left].add(right)
            companion_rules[right].add(left)
        relevant_knowledge_rules = set()
        for retrieval_rule in active_retrieval_rules:
            relevant_knowledge_rules.update(
                companion_rules.get(retrieval_rule, set())
            )
        cluster_rule_ids = generation_context.get(
            "_last_plan_rule_matches"
        ) or []
        target_card_ids = sorted({
            str(fix["card_id"])
            for index, cluster in enumerate(plan.get("clusters", []))
            if index < len(cluster_rule_ids) and
            cluster_rule_ids[index] in relevant_knowledge_rules
            for fix in cluster.get("fixes", [])
            if isinstance(fix, dict) and
            fix.get("action") == "add_card" and
            fix.get("em_type") == "knowledge" and fix.get("card_id")
        })
        if not target_card_ids:
            report.update({
                "passed": False,
                "error":
                    "matched retrieval rule has no companion new knowledge card",
            })
            return report

        try:
            manifest = self._manifest_data(self.cand_dir)
            delivery_specs = [
                item for item in (manifest.get("delivery") or [])
                if isinstance(item, dict)
            ]
            skill_memory = activation_probe.load_skill_memory(
                self.cand_dir
            )
            for rule_id in active_retrieval_rules:
                rule = rules[rule_id]
                carrier_fixes = [
                    fix_rule for fix_rule in rule["fix_rules"]
                    if fix_rule["action"] in {
                        "set_delivery", "edit_delivery",
                    } and
                    fix_rule["exact_fields"].get("deliverer") ==
                        "need_driven_retrieval"
                ]
                if len(carrier_fixes) != 1:
                    raise RuntimeError(
                        f"retrieval rule {rule_id!r} has no unique carrier"
                    )
                exact = carrier_fixes[0]["exact_fields"]
                carrier = exact["deliverer"]
                cfg = copy.deepcopy(exact["cfg"])
                actual = [
                    item for item in delivery_specs
                    if item.get("use") == carrier and
                    (item.get("cfg") or {}) == cfg
                ]
                if len(actual) != 1:
                    raise RuntimeError(
                        f"candidate lacks the exact {rule_id!r} carrier/config"
                    )
                deliverer = activation_probe.builtin.make_deliverer(
                    carrier, cfg
                )
                for query in query_spec[rule_id]:
                    ledger = activation_probe.Ledger(
                        skill_memory.wm_schema
                    )
                    ledger.apply_model_update(
                        [{"description": query["text"]}], set()
                    )
                    trigger = activation_probe.TriggerContext(
                        event=activation_probe.Event.INTENT_RECORDED.value,
                        turn=1,
                        incoming_kind="user",
                        incoming_text=query["text"],
                        extra={"delivered_docs": set()},
                    )
                    actions = deliverer.select(
                        trigger, ledger, skill_memory.em, draft=None
                    )
                    selected = sorted({
                        str(action.doc_id)
                        for action in actions
                        if isinstance(
                            action, activation_probe.CoverWithDoc
                        )
                    })
                    activated = bool(
                        set(target_card_ids) & set(selected)
                    )
                    report["probes"].append({
                        "rule_id": rule_id,
                        "query_id": query["query_id"],
                        "query_sha256": hashlib.sha256(
                            query["text"].encode("utf-8")
                        ).hexdigest(),
                        "carrier": carrier,
                        "cfg": cfg,
                        "target_new_knowledge_cards":
                            target_card_ids,
                        "selected_cards": selected,
                        "activated": activated,
                    })
        except Exception as exc:
            report.update({
                "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return report
        report["passed"] = bool(report["probes"]) and all(
            probe["activated"] for probe in report["probes"]
        )
        return report

    def _verify_generation_profiles(self) -> None:
        if not self.provenance_path.is_file():
            return
        provenance = json.loads(
            self.provenance_path.read_text(encoding="utf-8")
        )
        records = provenance.get("generation_profiles")
        if getattr(self, "generation_profile_spec", None) is None:
            if records not in (None, {}):
                raise RuntimeError(
                    "unprofiled run contains generation profile records"
                )
            return
        if not isinstance(records, dict):
            raise RuntimeError("profiled run lacks generation profile records")
        for key, record in records.items():
            if not isinstance(record, dict) or not str(key).isdigit():
                raise RuntimeError("invalid generation profile provenance record")
            rnd = int(key)
            rdir = self.run_dir / f"round_{rnd}"
            paths = self._generation_profile_paths(rnd, rdir)
            for root in (paths["visible_root"], paths["control_root"]):
                self._assert_no_linklike(root, allow_missing=False)
            if (not paths["spec"].is_file() or
                    paths["spec"].read_bytes() !=
                    self.generation_profile_spec_bytes):
                raise RuntimeError(
                    "materialized generation profile spec failed its raw hash"
                )
            expected_visible = {
                "evidence": self.generation_profile_spec[
                    "visible_inputs"]["evidence_md"].encode("utf-8"),
                "policy": self.generation_profile_spec[
                    "visible_inputs"]["policy_md"].encode("utf-8"),
                "plan_protocol": self.generation_profile_spec[
                    "visible_inputs"]["plan_protocol_md"].encode("utf-8"),
                "patch_protocol": self.generation_profile_spec[
                    "visible_inputs"]["patch_protocol_md"].encode("utf-8"),
            }
            disclosure = self._build_capability_disclosure(
                self.generation_profile_spec
            )
            expected_visible.update({
                "capability": disclosure["manual_bytes"],
                "tool_capabilities":
                    disclosure["tool_capabilities_bytes"],
            })
            for name, expected_bytes in expected_visible.items():
                path = paths[name]
                if (not path.is_file() or self._is_linklike(path) or
                        path.read_bytes() != expected_bytes):
                    raise RuntimeError(
                        f"generation profile visible artifact changed: {name}"
                    )
            self._profile_source_proof(rnd, rdir)
            visible_hashes = {
                name: self._sha256_file(paths[name])
                for name in expected_visible
            }
            expected_manifest = self._build_generation_profile_manifest(
                rnd, rdir, visible_hashes,
            )
            if (not paths["manifest"].is_file() or
                    json.loads(paths["manifest"].read_text(
                        encoding="utf-8")) != expected_manifest):
                raise RuntimeError(
                    "generation profile input manifest failed verification"
                )
            expected_record = self._generation_profile_provenance_record(
                rnd, paths, expected_manifest, visible_hashes,
            )
            if record != expected_record:
                raise RuntimeError(
                    "generation profile provenance record failed verification"
                )

    def _assert_generation_profile_resume_safe(self, rnd: int) -> None:
        """Fail closed instead of granting extra D/P attempts after a crash."""
        if getattr(self, "generation_profile_spec", None) is None:
            return
        if self.state.get("round_done", -1) >= rnd:
            return
        provenance = json.loads(
            self.provenance_path.read_text(encoding="utf-8")
        )
        prior_sessions = [
            session for session in provenance.get("meta_sessions", [])
            if isinstance(session, dict) and
            re.fullmatch(fr"r{rnd}_[dp]\d+", str(session.get("name") or ""))
        ]
        artifact_patterns = (
            f"r{rnd}_d*_prompt.txt", f"r{rnd}_p*_prompt.txt",
            f"r{rnd}_d*.jsonl", f"r{rnd}_p*.jsonl",
        )
        artifacts = [
            path for pattern in artifact_patterns
            for path in self.run_dir.glob(pattern)
        ]
        if prior_sessions or artifacts:
            raise RuntimeError(
                "locked generation profile cannot resume an incomplete round "
                "after a D/P model session; start a fresh run-id"
            )

    def _formal_protocol(self, name: str, artifact: Path) -> str:
        path = FORMAL_PROTOCOL_DIR / f"{name}.md"
        if not path.is_file():
            raise RuntimeError(f"required formal protocol is missing: {path}")
        text = path.read_text(encoding="utf-8")
        forbidden = ("tau2_retail", "skill_memories/tau2_airline",
                     "skill_memories/skillflow")
        if any(token in text for token in forbidden):
            raise RuntimeError(f"formal protocol contains a champion reference: {path}")
        self._write_text_atomic(artifact, text)
        return text

    @staticmethod
    def _compose_capability_prompt_block(
            manual_text: str, tool_capabilities_text: str) -> str:
        return (
            "=== BEGIN RECURIS PUBLIC CAPABILITY CONTRACT ===\n"
            + manual_text.rstrip("\n")
            + "\n\n=== CURRENT DOMAIN TOOL CAPABILITIES "
              "(PUBLIC INTERFACE METADATA ONLY) ===\n"
            + tool_capabilities_text.rstrip("\n")
            + "\n=== END RECURIS PUBLIC CAPABILITY CONTRACT ==="
        )

    def _benchmark_profile(self) -> "BenchmarkProfile":
        """Profile accessor tolerant of partially-constructed drivers.

        Split-audit and disclosure tooling build Driver objects without
        running __init__; they are always tau2 arms, so default there."""
        profile = getattr(self, "profile", None)
        if profile is None:
            profile = BenchmarkProfile(getattr(self, "benchmark", "tau2"))
            self.profile = profile
        return profile

    def _build_capability_disclosure(
            self, profile: dict | None = None) -> dict:
        """Build the task-answer-free public contract shown to the Meta-Agent."""
        if profile is None:
            extension_path = self._benchmark_profile().capability_extension_path
            capability_paths = (
                CAPABILITY_MANUAL_PATH,
                extension_path,
            )
            missing = [path for path in capability_paths if not path.is_file()]
            if missing:
                raise RuntimeError(
                    f"required capability contract is missing: {missing}"
                )
            manual_bytes = (
                CAPABILITY_MANUAL_PATH.read_bytes().rstrip(b"\n")
                + b"\n\n"
                + extension_path.read_bytes().rstrip(b"\n")
                + b"\n"
            )
            selected_tools = None
        else:
            manual_text_value = (
                (profile.get("visible_inputs") or {}).get("capability_md")
            )
            if not isinstance(manual_text_value, str):
                raise RuntimeError(
                    "generation profile has no visible capability manual"
                )
            manual_bytes = manual_text_value.encode("utf-8")
            selected_tools = set(profile.get("tool_allowlist") or [])
        manual_text = manual_bytes.decode("utf-8", errors="strict")
        if "\r" in manual_text:
            raise RuntimeError("capability manual must use canonical LF line endings")
        forbidden = (
            "skill_memories/", "src/recuris/", "tau2_retail",
            "task 28", "task 34", "task 38", "champion", "claude",
        )
        lowered = manual_text.lower()
        if any(token in lowered for token in forbidden):
            raise RuntimeError(
                "capability manual contains a historical implementation or task reference"
            )

        tools = []
        for name, raw in sorted(self._benchmark_profile().tool_capabilities(self.domain).items()):
            if selected_tools is not None and name not in selected_tools:
                continue
            tool_type = str(raw.get("tool_type") or "").split(".")[-1].lower()
            tools.append({
                "mutates_state": bool(raw.get("mutates_state")),
                "name": str(name),
                "tool_type": tool_type,
            })
        if selected_tools is not None and {
                row["name"] for row in tools} != selected_tools:
            raise RuntimeError(
                "generation profile tool allowlist cannot be projected exactly"
            )
        tool_data = {
            "schema_version": CAPABILITY_DISCLOSURE_SCHEMA_VERSION,
            "tools": tools,
        }
        tools_text = json.dumps(
            tool_data, ensure_ascii=False, indent=2, sort_keys=True,
        ) + "\n"
        tools_bytes = tools_text.encode("utf-8")
        block = self._compose_capability_prompt_block(manual_text, tools_text)
        block_bytes = block.encode("utf-8")
        if len(block_bytes) > MAX_CAPABILITY_PROMPT_BLOCK_BYTES:
            raise RuntimeError(
                "capability prompt block exceeds the bounded command-line budget: "
                f"{len(block_bytes)} > {MAX_CAPABILITY_PROMPT_BLOCK_BYTES}"
            )
        return {
            "manual_bytes": manual_bytes,
            "manual_text": manual_text,
            "manual_sha256": hashlib.sha256(manual_bytes).hexdigest(),
            "tool_capabilities": tool_data,
            "tool_capabilities_bytes": tools_bytes,
            "tool_capabilities_text": tools_text,
            "tool_capabilities_sha256": hashlib.sha256(tools_bytes).hexdigest(),
            "prompt_block": block,
            "prompt_block_sha256": hashlib.sha256(block_bytes).hexdigest(),
        }

    def _materialize_capability_disclosure(
            self, rnd: int, rdir: Path, profile: dict | None = None,
            profile_root: Path | None = None) -> dict:
        """Freeze one public contract per round and bind it into provenance."""
        disclosure = self._build_capability_disclosure(profile)
        if profile is None:
            manual_path = rdir / "capability_manual.md"
            tools_path = rdir / "tool_capabilities.json"
        else:
            if profile_root is None:
                raise RuntimeError(
                    "profile capability disclosure requires a visible root"
                )
            manual_path = profile_root / "capability.md"
            tools_path = profile_root / "tool_capabilities.json"
        for path, content in (
                (manual_path, disclosure["manual_bytes"]),
                (tools_path, disclosure["tool_capabilities_bytes"])):
            if path.exists():
                if not path.is_file() or path.read_bytes() != content:
                    raise RuntimeError(
                        f"frozen capability disclosure changed on resume: {path}"
                    )
            else:
                self._write_bytes_atomic(path, content)

        record = {
            "schema_version": CAPABILITY_DISCLOSURE_SCHEMA_VERSION,
            "round": rnd,
            "manual_path": str(manual_path),
            "manual_sha256": disclosure["manual_sha256"],
            "tool_capabilities_path": str(tools_path),
            "tool_capabilities_sha256":
                disclosure["tool_capabilities_sha256"],
            "prompt_block_sha256": disclosure["prompt_block_sha256"],
            "projection_fields": ["name", "tool_type", "mutates_state"],
            "delivery":
                "embedded-first-in-D-and-P-plus-exact-read-only-copies",
        }
        if profile is not None:
            record["generation_profile"] = {
                "profile_id": profile["profile_id"],
                "raw_spec_sha256": self.generation_profile_spec_sha256,
            }
        provenance = json.loads(
            self.provenance_path.read_text(encoding="utf-8")
        )
        records = provenance.setdefault("capability_disclosures", {})
        previous = records.get(str(rnd))
        if previous is not None and previous != record:
            raise RuntimeError("capability disclosure provenance changed on resume")
        records[str(rnd)] = record
        self._write_json_atomic(self.provenance_path, provenance)
        disclosure.update({
            "manual_path": manual_path,
            "tool_capabilities_path": tools_path,
            "record": record,
        })
        return disclosure

    def _verify_capability_disclosures(self) -> None:
        if not self.provenance_path.is_file():
            return
        provenance = json.loads(
            self.provenance_path.read_text(encoding="utf-8")
        )
        records = provenance.get("capability_disclosures", {})
        if not isinstance(records, dict):
            raise RuntimeError("invalid capability disclosure provenance")
        for key, record in records.items():
            if not isinstance(record, dict) or str(record.get("round")) != str(key):
                raise RuntimeError("invalid capability disclosure record")
            manual_path = Path(str(record.get("manual_path") or ""))
            tools_path = Path(str(record.get("tool_capabilities_path") or ""))
            for path in (manual_path, tools_path):
                if (not path.is_file() or
                        not path.resolve().is_relative_to(self.run_dir.resolve())):
                    raise RuntimeError(
                        f"capability disclosure is missing or outside the run: {path}"
                    )
            manual_bytes = manual_path.read_bytes()
            tools_bytes = tools_path.read_bytes()
            if hashlib.sha256(manual_bytes).hexdigest() != record.get(
                    "manual_sha256"):
                raise RuntimeError("frozen capability manual failed SHA-256")
            if hashlib.sha256(tools_bytes).hexdigest() != record.get(
                    "tool_capabilities_sha256"):
                raise RuntimeError("frozen tool capabilities failed SHA-256")
            block = self._compose_capability_prompt_block(
                manual_bytes.decode("utf-8", errors="strict"),
                tools_bytes.decode("utf-8", errors="strict"),
            )
            if hashlib.sha256(block.encode("utf-8")).hexdigest() != record.get(
                    "prompt_block_sha256"):
                raise RuntimeError("capability prompt block failed SHA-256")

    # ---------- plumbing ----------
    def log(self, msg):
        line = f"[{_now()}] {msg}"
        with self._cc_lock:
            print(line, flush=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def save_state(self):
        self._write_json_atomic(self.state_path, self.state)

    def _ledger_event_path(self, event_id: str) -> Path:
        if not isinstance(event_id, str) or not event_id:
            raise RuntimeError("ledger event_id must be a non-empty string")
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        return self.ledger_events_dir / f"{digest}.json"

    def _load_ledger_events(self) -> list[dict]:
        events: list[dict] = []
        seen: set[str] = set()
        self._assert_no_linklike(self.ledger_events_dir, allow_missing=False)
        for path in sorted(self.ledger_events_dir.glob("*.json")):
            self._assert_no_linklike(path, allow_missing=False)
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"invalid atomic ledger event: {path}") from exc
            event_id = rec.get("event_id") if isinstance(rec, dict) else None
            if (not isinstance(event_id, str) or not event_id or
                    path != self._ledger_event_path(event_id)):
                raise RuntimeError(f"invalid atomic ledger event identity: {path}")
            if event_id in seen:
                raise RuntimeError(f"duplicate atomic ledger event: {event_id}")
            if not isinstance(rec.get("ts"), str) or not rec["ts"]:
                raise RuntimeError(f"atomic ledger event lacks timestamp: {path}")
            seen.add(event_id)
            events.append(rec)
        return sorted(events, key=lambda rec: (rec["ts"], rec["event_id"]))

    def _sync_ledger(self) -> None:
        content = "".join(
            json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
            for rec in self._load_ledger_events()
        )
        self._write_text_atomic(self.ledger_path, content)

    def _recover_ledger_aggregate(self) -> None:
        events = self._load_ledger_events()
        if not events:
            if (self.ledger_path.exists() and
                    self.ledger_path.read_text(encoding="utf-8").strip()):
                raise RuntimeError(
                    "legacy ledger has no atomic event artifacts; refusing unsafe resume"
                )
            return
        # JSONL is a derived view. Rebuild it even when a crash tore its last
        # line; the atomically replaced per-event files are authoritative.
        self._sync_ledger()

    def ledger(self, rec: dict):
        event_id = rec.get("event_id") if isinstance(rec, dict) else None
        path = self._ledger_event_path(event_id)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"invalid atomic ledger event: {path}") from exc
            comparable = {key: value for key, value in existing.items() if key != "ts"}
            if comparable != rec:
                raise RuntimeError(f"conflicting ledger event: {event_id}")
            self._sync_ledger()
            return
        self._write_json_atomic(path, {"ts": _now(), **rec})
        self._sync_ledger()

    def _ledger_once(self, event_id: str, rec: dict) -> None:
        self.ledger({"event_id": event_id, **rec})

    def _lesson_exists(self, rnd: int) -> bool:
        artifact = self.run_dir / f"round_{rnd}_lesson.txt"
        if not artifact.exists():
            return False
        prefix = f"Round {rnd} (run {self.a.run_id}):"
        text = artifact.read_text(encoding="utf-8")
        if not text.startswith(prefix) or not text.endswith("\n") or len(text.strip()) <= len(prefix):
            raise RuntimeError(f"invalid atomic lesson artifact: {artifact}")
        self._sync_lessons()
        return True

    def _sync_lessons(self) -> None:
        lessons: list[tuple[int, str]] = []
        for artifact in self.run_dir.glob("round_*_lesson.txt"):
            match = re.fullmatch(r"round_(\d+)_lesson\.txt", artifact.name)
            if not match:
                raise RuntimeError(f"invalid lesson artifact name: {artifact}")
            text = artifact.read_text(encoding="utf-8")
            expected = f"Round {int(match.group(1))} (run {self.a.run_id}):"
            if not text.startswith(expected) or not text.endswith("\n"):
                raise RuntimeError(f"invalid atomic lesson artifact: {artifact}")
            lessons.append((int(match.group(1)), text))
        self._write_text_atomic(self.lessons_path,
                                "".join(text for _, text in sorted(lessons)))

    def _finalize_round(self, rnd: int, payload: dict) -> None:
        """Idempotently persist ledger/lesson/state for one terminal round."""
        if payload.get("outcome") is None:
            raise RuntimeError("round finalization requires an outcome")
        journal_path = self.run_dir / f"round_{rnd}_finalize.json"
        if journal_path.exists():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("round") != rnd or journal.get("payload") != payload:
                raise RuntimeError(f"conflicting round finalization journal: {journal_path}")
            if journal.get("status") == "complete":
                if (self.state.get("round_done", -1) < rnd or
                        self.state.get("round_outcomes", {}).get(str(rnd)) !=
                        payload["outcome"]):
                    raise RuntimeError(f"completed finalization disagrees with state: {journal_path}")
                return
        else:
            journal = {"status": "prepared", "round": rnd, "payload": payload,
                       "created_at": _now()}
            self._write_json_atomic(journal_path, journal)

        if self.state["round_done"] < rnd - 1:
            raise RuntimeError(
                f"cannot finalize round {rnd} after round {self.state['round_done']} (gap)"
            )
        ledger_rec = payload.get("ledger")
        if ledger_rec is not None:
            self._ledger_once(f"round:{rnd}:{payload['outcome']}", ledger_rec)
        lesson = payload.get("lesson")
        if lesson is not None and not self._lesson_exists(rnd):
            self.append_lesson(rnd, lesson)
        for blocked in payload.get("blocked_additions", []):
            if blocked not in self.state["blocked"]:
                self.state["blocked"].append(blocked)
        self.state["round_done"] = max(self.state["round_done"], rnd)
        self.state["round_outcomes"][str(rnd)] = payload["outcome"]
        self.save_state()
        journal["status"] = "complete"
        journal["completed_at"] = _now()
        self._write_json_atomic(journal_path, journal)

    def _finalize_profile_arm_ineligible(
            self, rnd: int, exc: ProfileArmIneligible) -> str:
        """Atomically seal a contaminated locked-profile arm without detail."""
        if getattr(self, "generation_profile_spec", None) is None:
            raise RuntimeError(
                "profile arm ineligibility requires a locked generation profile"
            )
        if rnd != 1:
            raise RuntimeError(
                "generation-profile schema v1 may finalize ineligibility "
                "only for round 1"
            )
        if self.state.get("sims") != 0:
            raise RuntimeError(
                "profile arm ineligibility cannot be finalized after "
                "downstream simulations"
            )
        self._finalize_round(rnd, {
            "outcome": "profile-ineligible",
            "ledger": {
                "round": rnd,
                "phase": exc.phase,
                "reason_code": exc.reason_code,
            },
        })
        return "profile-ineligible"

    def _count_sims(self, n_tasks, k):
        proposed = self.state["sims"] + n_tasks * k
        if proposed > self.a.max_sims:
            raise RuntimeError(f"sim budget exceeded ({proposed}/{self.a.max_sims})")
        self.state["sims"] = proposed

    def ensure_proxy(self):
        import httpx
        url = f"http://127.0.0.1:{self.a.proxy_port}/health"

        def health():
            try:
                response = httpx.get(url, timeout=5, trust_env=False)
                return response.json() if response.status_code == 200 else None
            except Exception:
                return None

        current = health()
        if current:
            if (current.get("model") != self.a.meta_model or
                    current.get("reasoning") != self.a.meta_reasoning or
                    current.get("temperature") != META_TEMPERATURE):
                raise RuntimeError(
                    f"proxy :{self.a.proxy_port} has {current.get('model')}/"
                    f"{current.get('reasoning')}/T={current.get('temperature')}, expected "
                    f"{self.a.meta_model}/{self.a.meta_reasoning}/T={META_TEMPERATURE}; "
                    "refusing to mix experiments"
                )
            self._record_proxy_health(current)
            return current

        self.log(f"proxy down -> starting :{self.a.proxy_port} "
                 f"model={self.a.meta_model} reasoning={self.a.meta_reasoning}")
        env = dict(os.environ)
        env.update({"PROXY_PORT": str(self.a.proxy_port),
                    "RECURIS_PROXY_MODEL": self.a.meta_model,
                    "PROXY_REASONING_EFFORT": self.a.meta_reasoning,
                    "PROXY_TEMPERATURE": str(META_TEMPERATURE)})
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                 getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(self.run_dir / "proxy.log", "a", encoding="utf-8") as logf:
            self.proxy_proc = subprocess.Popen(
                [PY, "-m", "recuris.metaagent.proxy.anthropic_openai_proxy"],
                cwd=str(ROOT), env=env,
                stdout=logf, stderr=logf, creationflags=flags)
        self.proxy_owned = True
        for _ in range(30):
            time.sleep(2)
            current = health()
            if current:
                if (current.get("model") != self.a.meta_model or
                        current.get("reasoning") != self.a.meta_reasoning or
                        current.get("temperature") != META_TEMPERATURE):
                    self.stop_proxy()
                    raise RuntimeError(f"new proxy reported unexpected config: {current}")
                self._record_proxy_health(current)
                return current
            if self.proxy_proc.poll() is not None:
                break
        self.stop_proxy()
        raise RuntimeError(
            f"cannot start the translating proxy on :{self.a.proxy_port}")

    def stop_proxy(self):
        if not self.proxy_owned or self.proxy_proc is None:
            return
        if self.proxy_proc.poll() is None:
            self.proxy_proc.terminate()
            try:
                self.proxy_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                subprocess.run(_tree_kill_cmd(self.proxy_proc.pid),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    self.proxy_proc.wait(timeout=10)
                except Exception:
                    pass
        self.proxy_owned = False

    def integrity(self):
        # A settled package must stay byte-identical to what it was when it was
        # measured. The provenance comparison further down covers the framework
        # sources themselves, so between them nothing under evaluation can move
        # mid-run without the campaign stopping.
        try:
            verify_champions()
        except Exception as exc:
            raise RuntimeError(f"INTEGRITY CHECK FAILED:\n{exc}") from exc
        self._verify_versions()
        self._verify_capability_disclosures()
        self._verify_generation_profiles()
        self._verify_generated_candidates()
        if self.provenance_path.exists():
            provenance = json.loads(self.provenance_path.read_text(encoding="utf-8"))
            expected = provenance.get("runtime_sources")
            if expected != self._runtime_sources_record(
                    getattr(self, "benchmark", "tau2")):
                raise RuntimeError("runtime/Meta-Agent sources changed during this run")

    def inventory(self) -> dict:
        inv = {}
        # tb21_runs is TB2.1 eval run PRODUCT (harbor jobs, fingerprint
        # ledgers), never benchmark input — inputs are bound separately by
        # _benchmark_input_manifest.  Scanning it attributed a concurrent
        # TB eval's writes to whichever CC session was open (mine #16), the
        # same class the data/simulations exclusion below already covers for
        # tau2.
        volatile_roots = {".git", "logs", "tau2data", ".embed_cache", ".venv"}
        roots = [ROOT, TAU2_ROOT]
        for scan_root in roots:
            if not scan_root.is_dir():
                continue
            for current, dirs, files in os.walk(scan_root, followlinks=False):
                current_path = Path(current)
                relative_dir = current_path.relative_to(scan_root)
                kept_dirs = []
                for name in dirs:
                    child = current_path / name
                    if name in volatile_roots or name in {"__pycache__", ".pytest_cache"}:
                        continue
                    if self._is_linklike(child):
                        continue
                    if (scan_root == ROOT and relative_dir.parts == ("ma_runs",) and
                            name != self.a.run_id):
                        continue
                    if (scan_root != ROOT and relative_dir.parts == ("data",) and
                            name == "simulations"):
                        # Tau2 simulation output: run PRODUCT written by any
                        # concurrent runner/run_tau2.py evaluation, never
                        # benchmark input (inputs are bound separately by
                        # _benchmark_input_manifest).  Excluded so full-set
                        # evals running beside a Meta-Agent session do not
                        # register as stray writes.
                        continue
                    kept_dirs.append(name)
                dirs[:] = kept_dirs
                for name in files:
                    p = current_path / name
                    if name == ".driver.lock" or p.suffix.lower() in {".pyc", ".pyo"}:
                        continue
                    if self._is_linklike(p):
                        raise RuntimeError(f"linked file appeared in inventory scope: {p}")
                    inv[str(p.resolve())] = hashlib.sha256(p.read_bytes()).hexdigest()
        return inv

    def inventory_diff(self, before: dict, allowed_prefixes: list) -> list:
        after = self.inventory()
        changed = [p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)]
        allowed = [Path(ap).resolve() for ap in allowed_prefixes]
        return [p for p in changed
                if not any(Path(p).resolve().is_relative_to(ap) for ap in allowed)]

    # ---------- CC sessions ----------
    @staticmethod
    def _permission_rule(tool: str, path: Path, *, directory: bool) -> str:
        relative = path.resolve().relative_to(ROOT.resolve()).as_posix()
        target = f"{relative}/**" if directory else relative
        return f"{tool}({target})"

    def _cc_settings(
            self, readable_paths: list[Path], writable_paths: list[Path],
            artifact: Path, denied_read_paths: list[Path] | None = None,
            denied_directory_reads: list[Path] | None = None
            ) -> tuple[list[str], list[str]]:
        read_rules: list[str] = []
        resolved_readable_paths: list[Path] = []
        policy_path = self.profile.policy_doc_path(self.domain).resolve()
        for raw in readable_paths:
            path = Path(raw)
            resolved = path.resolve()
            resolved_readable_paths.append(resolved)
            if resolved == policy_path and resolved.is_file():
                read_rules.append(
                    f"Read({self.profile.policy_doc_display(self.domain)})"
                )
            elif resolved.is_relative_to(ROOT.resolve()):
                if not resolved.is_relative_to(self.run_dir.resolve()):
                    raise RuntimeError(f"CC readable path is outside this run: {path}")
                if resolved == self.run_dir.resolve():
                    raise RuntimeError("CC may not receive whole-run Read access")
                # Never expose run-control state, held-out split provenance, or
                # prior model traces.  Each phase receives evidence artifacts
                # explicitly instead of broad Read(ma_runs/<run>/**) access.
                forbidden_names = {
                    "state.json", "provenance.json", "ledger.jsonl", "driver.log",
                    "proxy.log", ".driver.lock",
                }
                if resolved.name in forbidden_names or resolved.suffix in {".jsonl", ".err"}:
                    raise RuntimeError(f"CC may not read run-control artifact: {path}")
                directory = path.is_dir() or (not path.exists() and not path.suffix)
                read_rules.append(self._permission_rule("Read", path, directory=directory))
            else:
                raise RuntimeError(f"CC readable path is outside its phase scope: {path}")

        writable_rules: list[str] = []
        for raw in writable_paths:
            path = Path(raw)
            assert_mutation_paths_safe([path])
            resolved = path.resolve()
            if not resolved.is_relative_to(self.run_dir.resolve()):
                raise RuntimeError(f"CC writable path is outside this run: {path}")
            if resolved.is_relative_to(self.versions_dir.resolve()):
                raise RuntimeError(f"CC may not write immutable versions: {path}")
            directory = path.is_dir() or (not path.exists() and not path.suffix)
            writable_rules.extend([
                self._permission_rule("Edit", path, directory=directory),
            ])
        deny = ["Read(.env)", f"Read({TAU2_PREFIX}/.env)",
                "Read(ma_runs/*/state.json)", "Read(ma_runs/*/provenance.json)",
                "Read(ma_runs/*/ledger.jsonl)", "Read(ma_runs/*/*.jsonl)",
                "Read(ma_runs/*/*.err)", "Read(ma_runs/*/driver.log)",
                "Read(ma_runs/*/proxy.log)", "Read(ma_runs/*/.driver.lock)",
                "Read(ma_runs/*/replay_source.json)",
                "Read(ma_runs/*/round_*_finalize.json)",
                "Read(ma_runs/*/commit_*.json)",
                "Read(ma_runs/*/ledger_events/**)",
                "Read(ma_runs/*/*_prompt.txt)", "Read(ma_runs/*/*_settings.json)",
                f"Read({TAU2_PREFIX}/data/tau2/domains/*/tasks*.json)",
                f"Read({TAU2_PREFIX}/data/tau2/domains/*/split_tasks.json)",
                f"Read({TAU2_PREFIX}/data/tau2/domains/*/db.json)",
                f"Read({TAU2_PREFIX}/data/tau2/domains/*/task_issues/**)",
                "Edit(ma_runs/*/qualification_forbidden_probe.txt)",
                "Write(ma_runs/*/qualification_forbidden_probe.txt)",
                f"Edit({TAU2_PREFIX}/**)", f"Write({TAU2_PREFIX}/**)"]

        # Claude Code's built-in Read tool is not allow-only merely because
        # exact paths appear in permissions.allow/--allowedTools.  Explicitly
        # deny every non-phase workspace root, including ALL historical Skill
        # Memories, and audit the trace again after the session.
        protected_roots = [
            ".claude/**", ".embed_cache/**", ".git/**", ".github/**",
            ".openai/**", "data/**", "docs/**", "experiments/**", "logs/**",
            "metaagent/**", "metaagent_demo/**", "runner/**",
            "skill_memories/**", "src/**",
            "tau2data/**",
        ]
        for root in protected_roots:
            deny.extend([f"Read({root})", f"Edit({root})", f"Write({root})"])

        # Catch newly added top-level files/directories without relying on a
        # hand-maintained list.  ma_runs is handled separately because the
        # current run contains the explicitly allowed phase artifacts.
        if ROOT.is_dir():
            runtime_root = getattr(
                getattr(self, "profile", None), "runtime_root", None)
            policy_carveout = (
                runtime_root.resolve()
                if runtime_root is not None and runtime_root.is_dir() else None
            )
            for child in sorted(ROOT.iterdir(), key=lambda path: path.name):
                if child.name == "ma_runs":
                    continue
                if policy_carveout is not None and child.resolve() == policy_carveout:
                    # Handled by the policy sibling-walk below: everything under
                    # the benchmark's runtime root except policy/<domain>.md is
                    # denied individually so the policy allow rule is not
                    # shadowed by a tree deny.
                    continue
                target = child.name + ("/**" if child.is_dir() else "")
                deny.extend([f"Read({target})", f"Edit({target})", f"Write({target})"])

        # Other runs (including preregistrations and previous traces) are never
        # inputs to this session.  Only the current run can contain an allowed
        # artifact.
        ma_runs_root = ROOT / "ma_runs"
        if ma_runs_root.is_dir():
            for child in sorted(ma_runs_root.iterdir(), key=lambda path: path.name):
                if child.resolve() == self.run_dir.resolve():
                    continue
                relative = child.relative_to(ROOT).as_posix()
                target = relative + ("/**" if child.is_dir() else "")
                deny.extend([f"Read({target})", f"Edit({target})",
                             f"Write({target})"])

        # Immutable versions remain readable only when an exact version path is
        # passed for the phase; they are never writable.
        deny.extend(["Edit(ma_runs/*/packages/versions/**)",
                     "Write(ma_runs/*/packages/versions/**)"])

        # The policy tree is reachable solely for the current domain policy.
        # Deny every sibling along that path so benchmark code, tasks, DBs, and
        # other domains cannot become accidental Meta-Agent evidence.
        if True:
            policy_walk_root = TAU2_ROOT.resolve()
            policy_walk_prefix = f"{TAU2_PREFIX}/"
            walk_enabled = policy_walk_root.is_dir() and policy_path.is_file()
        else:
            benchmark = getattr(self, "benchmark", "tau2")
            policy_walk_root = (ROOT / benchmark).resolve()
            policy_walk_prefix = f"{benchmark}/"
            if not policy_path.is_file():
                # Fail-closed: E8.3 carved the runtime root out of the
                # top-level deny walk expecting this sibling walk to deny
                # everything except the policy doc.  Without the doc there is
                # no legitimate CC session — never fall through to a walk-less
                # permission set.
                raise RuntimeError(
                    f"{benchmark} policy doc is missing: {policy_path}"
                )
            walk_enabled = True
        if walk_enabled:
            current = policy_walk_root
            for keep_name in policy_path.relative_to(policy_walk_root).parts:
                for child in sorted(current.iterdir(), key=lambda path: path.name):
                    if child.name == keep_name:
                        continue
                    relative = child.relative_to(policy_walk_root).as_posix()
                    target = policy_walk_prefix + relative
                    if child.is_dir():
                        target += "/**"
                    deny.extend([f"Read({target})", f"Edit({target})",
                                 f"Write({target})"])
                current = current / keep_name

        # A locked generation profile needs pre-disclosure denial, not merely
        # post-hoc trace auditing. Denied paths may be current-run artifacts or
        # the one otherwise reachable full domain policy.
        for raw in denied_read_paths or []:
            path = Path(raw)
            resolved = path.resolve()
            directory = path.is_dir() or (
                not path.exists() and not path.suffix
            )
            if any(
                    allowed == resolved or
                    (directory and allowed.is_relative_to(resolved))
                    for allowed in resolved_readable_paths):
                raise RuntimeError(
                    "CC denied path shadows an explicitly readable path: "
                    f"{path}"
                )
            if resolved.is_relative_to(self.run_dir.resolve()):
                relative = resolved.relative_to(ROOT.resolve()).as_posix()
                target = relative + ("/**" if directory else "")
                deny.extend([
                    f"Read({target})", f"Edit({target})", f"Write({target})",
                ])
            elif resolved == policy_path and resolved.is_file():
                target = self.profile.policy_doc_display(self.domain)
                deny.extend([
                    f"Read({target})", f"Edit({target})", f"Write({target})",
                ])
            else:
                raise RuntimeError(
                    f"CC denied path is outside the locked phase scope: {path}"
                )
        for raw in denied_directory_reads or []:
            path = Path(raw)
            resolved = path.resolve()
            if not resolved.is_relative_to(self.run_dir.resolve()):
                raise RuntimeError(
                    f"CC exact denied directory is outside this run: {path}"
                )
            # Claude Code treats an exact directory deny as shadowing allowed
            # descendants.  Keep such an ancestor post-hoc audited instead of
            # placing the conflicting rule in permissions.deny.
            if any(
                    allowed == resolved or allowed.is_relative_to(resolved)
                    for allowed in resolved_readable_paths):
                continue
            target = resolved.relative_to(ROOT.resolve()).as_posix()
            deny.extend([
                f"Read({target})", f"Edit({target})", f"Write({target})",
            ])

        # Preserve order for audit readability while removing duplicate rules.
        deny = list(dict.fromkeys(deny))
        settings = {
            "permissions": {
                "defaultMode": "dontAsk",
                "additionalDirectories": self.profile.additional_directories(),
                "allow": [*read_rules, *writable_rules],
                "deny": deny,
            }
        }
        self._write_json_atomic(artifact, settings)
        return read_rules, writable_rules

    def _violation_is_contamination(self, resolved: str | None) -> bool:
        """Red-zone test for an out-of-scope path.

        Fatal (drives a hard stop) only when disclosure or mutation of the
        path would invalidate the experiment: benchmark answer/task data,
        historical champion packages and their write-ups, sibling runs, raw
        unsanitized simulations, secrets, and this run's control ledger.
        Everything else out of scope is a session failure handled by the
        normal retry path, not a driver death."""
        if not resolved:
            return False  # malformed call: nothing was disclosed
        try:
            path = Path(resolved).resolve()
        except Exception:
            return False
        if path.name.lower().endswith(".env"):
            return True
        benchmark_root = TAU2_ROOT.resolve()
        policy_path = (benchmark_root / "data" / "tau2" / "domains" /
                       self.domain / "policy.md")
        if path.is_relative_to(benchmark_root):
            return path != policy_path
        for red_root in ("skill_memories", "tau2data", "docs", "experiments"):
            if path.is_relative_to((ROOT / red_root).resolve()):
                return True
        ma_runs_root = (ROOT / "ma_runs").resolve()
        run_root = self.run_dir.resolve()
        if path.is_relative_to(ma_runs_root):
            if not path.is_relative_to(run_root):
                return True
            name = path.name
            if (name in {"state.json", "provenance.json"} or
                    name.startswith("ledger") or name.startswith("commit_") or
                    path.suffix == ".jsonl" or
                    "ledger_events" in path.parts):
                return True
        return False

    def _cc_read_scope_violations(self, tool_calls: list[dict],
                                  readable_paths: list[Path],
                                  permission_denials: list[dict],
                                  non_disclosing_read_ids: set[str] | None = None
                                  ) -> list[dict]:
        """Return successful Read calls outside the exact phase scope."""
        allowed: list[tuple[Path, bool]] = []
        for raw in readable_paths:
            path = Path(raw)
            is_dir = path.is_dir() or (not path.exists() and not path.suffix)
            allowed.append((path.resolve(), is_dir))
        denied_ids = {
            str(item.get("tool_use_id") or "")
            for item in permission_denials
            if isinstance(item, dict)
        }
        failed_ids = {
            str(value) for value in (non_disclosing_read_ids or set())
        }
        violations = []
        for call in tool_calls:
            call_id = str(call.get("id") or "")
            if (
                call.get("name") != "Read"
                or call_id in denied_ids
                or call_id in failed_ids
            ):
                continue
            raw = (call.get("input") or {}).get("file_path")
            if not isinstance(raw, str) or not raw.strip():
                violations.append({
                    "tool_use_id": call_id,
                    "file_path": raw,
                    "reason": "Read call has no valid file_path",
                })
                continue
            lexical = Path(raw)
            if not lexical.is_absolute():
                lexical = ROOT / lexical
            try:
                resolved = lexical.resolve()
            except Exception as exc:
                violations.append({
                    "tool_use_id": call_id,
                    "file_path": raw,
                    "reason": f"cannot resolve Read path: {exc}",
                })
                continue
            in_scope = any(
                resolved == scope or (is_dir and resolved.is_relative_to(scope))
                for scope, is_dir in allowed
            )
            if not in_scope:
                violations.append({
                    "tool_use_id": call_id,
                    "file_path": raw,
                    "resolved_path": str(resolved),
                    "reason": "successful Read is outside phase readable_paths",
                })
        return violations

    def _cc_edit_scope_violations(
            self, tool_calls: list[dict], writable_paths: list[Path],
            permission_denials: list[dict],
            non_executing_edit_ids: set[str] | None = None) -> list[dict]:
        """Audit every non-denied Edit attempt, including failed edits."""
        allowed: list[tuple[Path, bool]] = []
        for raw in writable_paths:
            path = Path(raw)
            is_dir = path.is_dir() or (
                not path.exists() and not path.suffix
            )
            allowed.append((path.resolve(), is_dir))
        denied_ids = {
            str(item.get("tool_use_id") or "")
            for item in permission_denials
            if isinstance(item, dict)
        }
        unexecuted_ids = {
            str(value) for value in (non_executing_edit_ids or set())
        }
        violations: list[dict] = []
        for call in tool_calls:
            call_id = str(call.get("id") or "")
            if (
                    call.get("name") != "Edit"
                    or call_id in denied_ids
                    or call_id in unexecuted_ids
            ):
                continue
            raw = (call.get("input") or {}).get("file_path")
            if not isinstance(raw, str) or not raw.strip():
                violations.append({
                    "tool_use_id": call_id,
                    "file_path": raw,
                    "reason": "Edit call has no valid file_path",
                })
                continue
            lexical = Path(raw)
            if not lexical.is_absolute():
                lexical = ROOT / lexical
            try:
                resolved = lexical.resolve()
            except Exception as exc:
                violations.append({
                    "tool_use_id": call_id,
                    "file_path": raw,
                    "reason": f"cannot resolve Edit path: {exc}",
                })
                continue
            in_scope = any(
                resolved == scope or
                (is_dir and resolved.is_relative_to(scope))
                for scope, is_dir in allowed
            )
            if not in_scope:
                violations.append({
                    "tool_use_id": call_id,
                    "file_path": raw,
                    "resolved_path": str(resolved),
                    "reason":
                        "non-denied Edit is outside phase writable_paths",
                })
        return violations

    @staticmethod
    def _cc_search_scope_violations(
            tool_calls: list[dict], readable_paths: list[Path],
            permission_denials: list[dict]) -> list[dict]:
        """Require Glob/Grep searches to name an allowed directory."""
        allowed_dirs = [
            path.resolve()
            for raw in readable_paths
            for path in [Path(raw)]
            if path.is_dir() or (not path.exists() and not path.suffix)
        ]
        denied_ids = {
            str(item.get("tool_use_id") or "")
            for item in permission_denials
            if isinstance(item, dict)
        }
        violations: list[dict] = []
        for call in tool_calls:
            call_id = str(call.get("id") or "")
            if call.get("name") not in {"Glob", "Grep"} or call_id in denied_ids:
                continue
            raw = (call.get("input") or {}).get("path")
            if not isinstance(raw, str) or not raw.strip():
                violations.append({
                    "tool_use_id": call_id,
                    "tool": call.get("name"),
                    "path": raw,
                    "reason": "search call must provide an explicit path",
                })
                continue
            lexical = Path(raw)
            if not lexical.is_absolute():
                lexical = ROOT / lexical
            try:
                resolved = lexical.resolve()
            except Exception as exc:
                violations.append({
                    "tool_use_id": call_id,
                    "tool": call.get("name"),
                    "path": raw,
                    "reason": f"cannot resolve search path: {exc}",
                })
                continue
            if not any(
                    resolved == scope or resolved.is_relative_to(scope)
                    for scope in allowed_dirs):
                violations.append({
                    "tool_use_id": call_id,
                    "tool": call.get("name"),
                    "path": raw,
                    "resolved_path": str(resolved),
                    "reason": "search call is outside phase-readable directories",
                })
        return violations

    @staticmethod
    def _is_non_disclosing_read_error(message: str) -> bool:
        """Recognize errors that provably return no file contents."""
        lowered = str(message or "").lower()
        return (
            "eisdir" in lowered
            or "illegal operation on a directory" in lowered
            or "is a directory" in lowered
            or "enoent" in lowered
            or "no such file" in lowered
            or "does not exist" in lowered
            or "file not found" in lowered
        )

    @staticmethod
    def _is_non_executing_edit_error(message: str) -> bool:
        """Recognize client-side argument failures before Edit can run."""
        lowered = str(message or "").lower()
        return (
            "inputvalidationerror" in lowered
            and (
                "could not be parsed as json" in lowered
                or "json parse failed" in lowered
            )
        )

    @staticmethod
    def _parse_cc_trace(out: Path) -> dict:
        records, invalid = [], 0
        for line in out.read_text(encoding="utf-8", errors="strict").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                invalid += 1
        init_tools, observed_tools, tool_calls, tool_results = [], [], [], []
        models, results = set(), []
        for record in records:
            if record.get("type") == "system" and record.get("subtype") == "init":
                init_tools = list(record.get("tools") or [])
                if record.get("model"):
                    models.add(str(record["model"]))
            if record.get("type") == "assistant":
                message = record.get("message") or {}
                if message.get("model"):
                    models.add(str(message["model"]))
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        observed_tools.append(str(block.get("name")))
                        tool_calls.append({"id": str(block.get("id") or ""),
                                           "name": str(block.get("name")),
                                           "input": block.get("input") or {}})
            if record.get("type") == "user":
                message = record.get("message") or {}
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_results.append({
                            "tool_use_id": str(block.get("tool_use_id") or ""),
                            "is_error": bool(block.get("is_error")),
                            "content": str(block.get("content") or ""),
                        })
            if record.get("type") == "result":
                results.append(record)
        calls_by_id = {call["id"]: call for call in tool_calls if call["id"]}
        trace_permission_denials = []
        failed_read_results = []
        failed_edit_results = []
        for result in tool_results:
            lowered = result["content"].lower()
            call = calls_by_id.get(result["tool_use_id"], {})
            if result["is_error"] and call.get("name") == "Read":
                failed_read_results.append({
                    "tool_use_id": result["tool_use_id"],
                    "input": call.get("input"),
                    "message": result["content"][:1000],
                    "non_disclosing":
                        Driver._is_non_disclosing_read_error(result["content"]),
                })
            if result["is_error"] and call.get("name") == "Edit":
                raw_path = (call.get("input") or {}).get("file_path")
                failed_edit_results.append({
                    "tool_use_id": result["tool_use_id"],
                    "input": call.get("input"),
                    "message": result["content"][:1000],
                    "non_executing": (
                        (not isinstance(raw_path, str) or not raw_path.strip())
                        and Driver._is_non_executing_edit_error(
                            result["content"]
                        )
                    ),
                })
            if (result["is_error"] and "permission" in lowered and
                    ("denied" in lowered or "not allowed" in lowered)):
                trace_permission_denials.append({
                    "source": "tool_result",
                    "tool_use_id": result["tool_use_id"],
                    "tool": call.get("name"),
                    "input": call.get("input"),
                    "message": result["content"],
                })
        return {"invalid_json_lines": invalid, "init_tools": init_tools,
                "observed_tools": observed_tools, "tool_calls": tool_calls,
                "trace_permission_denials": trace_permission_denials,
                "failed_read_results": failed_read_results,
                "failed_edit_results": failed_edit_results,
                "models": sorted(models),
                "result_records": results}

    @staticmethod
    def _cc_repeated_read_without_edit(out: Path) -> bool:
        """Detect a stalled session without interpreting model reasoning."""
        if not out.is_file() or not out.stat().st_size:
            return False
        trace = Driver._parse_cc_trace(out)
        calls = trace.get("tool_calls") or []
        if any(call.get("name") == "Edit" for call in calls):
            return False
        seen: set[str] = set()
        for call in calls:
            if call.get("name") != "Read":
                continue
            signature = json.dumps(
                call.get("input") or {}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
            if signature in seen:
                return True
            seen.add(signature)
        return False

    @staticmethod
    def _cc_failure_class(*, timed_out: bool, returncode: int | None,
                           invalid_json_lines: int, result_records: list[dict],
                           strays: list, upstream_ok: bool,
                           read_scope_violations: list | None = None,
                           search_scope_violations: list | None = None,
                           edit_scope_violations: list | None = None,
                           profile_tool_surface_ok: bool | None = None,
                           no_progress: bool = False,
                           ) -> tuple[str | None, str | None]:
        """Classify a failed Claude Code session without inspecting hidden reasoning."""
        final = result_records[-1] if result_records else {}
        result_is_error = bool(final.get("is_error"))
        result_error = None
        if result_is_error:
            result_error = str(
                final.get("result") or final.get("error") or
                final.get("message") or "Claude Code result marked is_error"
            )[:1000]
        if read_scope_violations:
            return ("read-scope-violation",
                    json.dumps(read_scope_violations, ensure_ascii=False)[:1000])
        if search_scope_violations:
            return ("search-scope-violation",
                    json.dumps(search_scope_violations, ensure_ascii=False)[:1000])
        if edit_scope_violations:
            return ("edit-scope-violation",
                    json.dumps(edit_scope_violations, ensure_ascii=False)[:1000])
        if profile_tool_surface_ok is False:
            return "tool-surface-mismatch", result_error
        if strays:
            return "scope-violation", result_error
        if no_progress:
            return "no-progress", result_error
        if timed_out:
            return "harness-timeout", result_error
        if not upstream_ok:
            return "upstream-model-mismatch", result_error
        if invalid_json_lines:
            return "invalid-trace", result_error
        if not result_records:
            return "missing-result", result_error
        if result_is_error:
            lowered = (result_error or "").lower()
            if "stream idle timeout" in lowered:
                return "stream-idle", result_error
            if "api error" in lowered or "upstream" in lowered:
                return "upstream-api-error", result_error
            return "result-error", result_error
        if returncode != 0:
            return "launcher-nonzero", result_error
        return None, None

    @staticmethod
    def _upstream_model_count_delta(
            before_health: dict, after_health: dict) -> tuple[dict[str, int],
                                                              list[str]]:
        """Return per-model counter increments for exactly one CC session."""
        errors: list[str] = []

        def normalize(health: dict, label: str) -> dict[str, int]:
            raw = (
                health.get("upstream_model_counts")
                if isinstance(health, dict) else None
            )
            if not isinstance(raw, dict):
                errors.append(
                    f"{label} proxy health lacks upstream_model_counts"
                )
                return {}
            counts: dict[str, int] = {}
            for model, count in raw.items():
                if (not isinstance(model, str) or not model or
                        not isinstance(count, int) or
                        isinstance(count, bool) or count < 0):
                    errors.append(
                        f"{label} proxy health has an invalid model counter"
                    )
                    continue
                counts[model] = count
            return counts

        before = normalize(before_health, "pre-session")
        after = normalize(after_health, "post-session")
        delta: dict[str, int] = {}
        for model in sorted(set(before) | set(after)):
            previous = before.get(model, 0)
            current = after.get(model, 0)
            if current < previous:
                errors.append(
                    "proxy upstream model counter decreased during session"
                )
                continue
            if current > previous:
                delta[model] = current - previous
        return delta, errors

    @staticmethod
    def _profile_session_binding_errors(
            session: dict, *, model: str, reasoning: str,
            temperature: float) -> list[str]:
        """Validate model treatment for successful and failed profile attempts."""
        errors: list[str] = []
        if session.get("requested_model") != model:
            errors.append("requested_model mismatch")
        if session.get("requested_reasoning_effort") != reasoning:
            errors.append("requested_reasoning_effort mismatch")
        if session.get("requested_temperature") != temperature:
            errors.append("requested_temperature mismatch")
        upstream = session.get("upstream_models")
        resolved = session.get("resolved_models")
        if not isinstance(upstream, list) or not isinstance(resolved, list):
            errors.append("upstream/resolved model proof must be lists")
            return errors
        before_counts = session.get("upstream_model_counts_before")
        after_counts = session.get("upstream_model_counts_after")
        recorded_delta = session.get("upstream_model_count_delta")
        if (not isinstance(before_counts, dict) or
                not isinstance(after_counts, dict) or
                not isinstance(recorded_delta, dict)):
            errors.append("per-session upstream counter proof is missing")
            derived_delta: dict[str, int] = {}
        else:
            derived_delta, counter_errors = (
                Driver._upstream_model_count_delta(
                    {"upstream_model_counts": before_counts},
                    {"upstream_model_counts": after_counts},
                )
            )
            errors.extend(counter_errors)
            if recorded_delta != derived_delta:
                errors.append("per-session upstream counter delta mismatch")
        observed_models = sorted(derived_delta)
        observed_chunks = sum(derived_delta.values())
        if upstream != observed_models or resolved != observed_models:
            errors.append("upstream/resolved models do not match session delta")
        if session.get("upstream_chunk_count") != observed_chunks:
            errors.append("upstream chunk count does not match session delta")
        if session.get("upstream_counter_errors"):
            errors.append("session contains upstream counter errors")
        observed_response = observed_chunks > 0
        if observed_response:
            if upstream != [model] or resolved != [model]:
                errors.append("resolved upstream model mismatch")
            if session.get("upstream_model_verified") is not True:
                errors.append("upstream model was not verified")
        elif session.get("upstream_model_verified") not in {False, None}:
            errors.append(
                "empty upstream attempt has inconsistent verification"
            )
        if session.get("ok") is True and (
                upstream != [model] or resolved != [model] or
                session.get("upstream_model_verified") is not True):
            errors.append(
                "successful session lacks exact verified upstream model"
            )
        return errors

    def cc_session(self, name: str, prompt: str, tools: str, timeout: int,
                   writable_paths: list[Path], readable_paths: list[Path],
                   capability_disclosure: dict | None = None,
                   denied_read_paths: list[Path] | None = None,
                   denied_directory_reads: list[Path] | None = None,
                   prompt_template_sha256: str | None = None,
                   audit_inventory: bool = True,
                   audit_integrity: bool = True,
                   detect_no_progress: bool = True) -> bool:
        # A session may always read back what it is allowed to write:
        # "edit, then re-read to verify" is standard tool behavior and
        # discloses nothing the session did not itself produce.
        readable_paths = [*readable_paths,
                          *[w for w in writable_paths if w not in readable_paths]]
        if audit_integrity:
            self.integrity()
        pre_session_health = self.ensure_proxy()
        session_key = f"{name}_{uuid.uuid4().hex[:10]}"
        pf = self.run_dir / f"{session_key}_prompt.txt"
        out = self.run_dir / f"{session_key}.jsonl"
        err = self.run_dir / f"{session_key}.err"
        settings_path = self.run_dir / f"{session_key}_settings.json"
        # Preserve the exact LF-normalized capability block bytes recorded in
        # provenance, including on Windows where text-mode writes use CRLF.
        pf.write_bytes(prompt.encode("utf-8"))
        read_rules, writable_rules = self._cc_settings(
            readable_paths, writable_paths, settings_path,
            denied_read_paths=denied_read_paths,
            denied_directory_reads=denied_directory_reads)
        # RECURIS_SESSION_LAUNCHER swaps in a different coding agent behind the
        # identical eight-argument contract (prompt file, transcript path, tool
        # set, permission rules, model, proxy port, reasoning, settings). The
        # audit and the proxy model proof read the same stream-json transcript
        # whichever harness produced it. See launchers/README.md.
        launcher = os.environ.get(
            "RECURIS_SESSION_LAUNCHER", str(HERE / "launchers" / "run_claude_code.sh"))
        tool_names = [tool for tool in tools.split() if tool]
        dsh_context = (
            Path(launcher).name.lower() == "run_dsh.sh" and
            os.environ.get("RECURIS_DSH_CONTEXT", "1").strip().lower()
            not in {"0", "false", "off", "no"} and
            prompt_template_sha256 is None and
            name != "qualification" and
            "Read" in tool_names
        )
        if dsh_context and "Context" not in tool_names:
            tool_names.append("Context")
        allowed_rules = []
        for tool in tool_names:
            if tool == "Read":
                allowed_rules.extend(read_rules)
            elif tool in {"Glob", "Grep"}:
                # Claude Code applies Read deny rules to built-in search tools;
                # the trace is also path-audited after the session.
                allowed_rules.append(tool)
            elif tool == "Context" and dsh_context:
                # Context has no workspace path of its own. The DSH adapter
                # limits it to run-local history/scratch state and read-only
                # child agents; their filesystem tools retain the rules below.
                pass
            elif tool not in {"Edit", "Write"}:
                raise RuntimeError(f"unscoped Claude Code tool is forbidden: {tool}")
        allowed_rules.extend(writable_rules)
        cmd = [self.bash, launcher, str(pf), str(out),
               ",".join(tool_names), " ".join(allowed_rules), self.a.meta_model,
               str(self.a.proxy_port), self.a.meta_reasoning, str(settings_path)]
        self.log(f"CC session {name} start (tools: {' '.join(tool_names)})")
        before = self.inventory() if audit_inventory else None
        p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        outp = ""
        timed_out = False
        no_progress = False

        def stop_session() -> None:
            subprocess.run(_tree_kill_cmd(p.pid),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                p.wait(timeout=15)
            except Exception:
                pass

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                stop_session()
                break
            poll_seconds = min(30.0, remaining)
            try:
                outp, _ = p.communicate(timeout=poll_seconds)
                break
            except subprocess.TimeoutExpired:
                if (detect_no_progress and
                        self._cc_repeated_read_without_edit(out)):
                    no_progress = True
                    stop_session()
                    break
                if poll_seconds >= remaining - 0.05:
                    timed_out = True
                    stop_session()
                    break
        allowed_changes = [*writable_paths, out, err, self.run_dir / "proxy.log"]
        strays = (
            self.inventory_diff(before, allowed_changes)
            if before is not None else []
        )
        # Keep driver-owned control writes outside the observed CC session
        # window.  Logging the timeout before this diff makes driver.log look
        # like an agent scope violation and aborts the retry path.
        if no_progress:
            self.log(
                f"CC session {name} NO PROGRESS after repeated unchanged Read"
            )
        elif timed_out:
            self.log(f"CC session {name} TIMEOUT after {timeout}s")
        trace = (self._parse_cc_trace(out) if out.is_file() and out.stat().st_size else
                  {"invalid_json_lines": 0, "init_tools": [], "observed_tools": [],
                   "tool_calls": [], "trace_permission_denials": [],
                   "failed_read_results": [],
                   "failed_edit_results": [],
                   "models": [], "result_records": []})
        result_records = trace.pop("result_records")
        permission_denials = (list(result_records[-1].get("permission_denials") or [])
                              if result_records else [])
        permission_denials.extend(trace.get("trace_permission_denials", []))
        failed_read_results = trace.get("failed_read_results", [])
        read_scope_violations = self._cc_read_scope_violations(
            trace["tool_calls"], readable_paths, permission_denials,
            {
                str(item.get("tool_use_id") or "")
                for item in failed_read_results
                if isinstance(item, dict) and item.get("non_disclosing") is True
            },
        )
        search_scope_violations = self._cc_search_scope_violations(
            trace["tool_calls"], readable_paths, permission_denials,
        )
        edit_scope_violations = self._cc_edit_scope_violations(
            trace["tool_calls"], writable_paths, permission_denials,
            {
                str(item.get("tool_use_id") or "")
                for item in trace.get("failed_edit_results", [])
                if isinstance(item, dict) and item.get("non_executing") is True
            },
        )
        profiled_session = prompt_template_sha256 is not None
        profile_tool_surface_ok = (
            len(trace["init_tools"]) == 2 and
            set(trace["init_tools"]) == {"Read", "Edit"}
            if profiled_session else None
        )
        profile_phase = None
        if profiled_session:
            phase_match = re.fullmatch(r"r\d+_([dp])\d+", name)
            if phase_match is not None:
                profile_phase = phase_match.group(1).upper()
        ok = (not timed_out and p.returncode == 0 and trace["invalid_json_lines"] == 0 and
               bool(result_records) and not bool(result_records[-1].get("is_error")) and
               not strays and not read_scope_violations and
               not search_scope_violations and
               not edit_scope_violations and
               profile_tool_surface_ok is not False)
        # Post-session checks run even after timeout or launcher failure.
        if audit_integrity:
            self.integrity()
        health = self.ensure_proxy()
        self._record_proxy_health(health)
        upstream_delta, upstream_counter_errors = (
            self._upstream_model_count_delta(pre_session_health, health)
        )
        upstream_models = sorted(upstream_delta)
        upstream_chunk_count = sum(upstream_delta.values())
        upstream_ok = (
            not upstream_counter_errors and upstream_chunk_count > 0 and
            upstream_models == [self.a.meta_model]
        )
        ok = ok and upstream_ok
        failure_class, result_error = self._cc_failure_class(
            timed_out=timed_out,
            returncode=p.returncode,
            invalid_json_lines=trace["invalid_json_lines"],
            result_records=result_records,
            strays=strays,
            upstream_ok=upstream_ok,
            read_scope_violations=read_scope_violations,
            search_scope_violations=search_scope_violations,
            edit_scope_violations=edit_scope_violations,
            profile_tool_surface_ok=profile_tool_surface_ok,
            no_progress=no_progress,
        )
        if ok:
            failure_class, result_error = None, None
        final_result = result_records[-1] if result_records else {}
        session_record = {
            "name": name, "session_key": session_key, "timestamp": _now(),
            "requested_model": self.a.meta_model,
            "requested_reasoning_effort": self.a.meta_reasoning,
            "requested_temperature": META_TEMPERATURE,
            "upstream_models": upstream_models,
            "upstream_model_counts_before": copy.deepcopy(
                pre_session_health.get("upstream_model_counts")
                if isinstance(pre_session_health, dict) else None
            ),
            "upstream_model_counts_after": copy.deepcopy(
                health.get("upstream_model_counts")
                if isinstance(health, dict) else None
            ),
            "upstream_model_count_delta": copy.deepcopy(upstream_delta),
            "upstream_chunk_count": upstream_chunk_count,
            "upstream_counter_errors": list(upstream_counter_errors),
            "trace_models": trace["models"],
            "resolved_models": upstream_models,
            "tool_surface": trace["init_tools"],
            "observed_tools": trace["observed_tools"],
            "tool_calls": trace["tool_calls"],
            "permission_denials": permission_denials,
            "failed_read_results": failed_read_results,
            "failed_edit_results": trace.get("failed_edit_results", []),
            "launcher_returncode": p.returncode, "timed_out": timed_out,
            "no_progress": no_progress,
            "stray_writes": strays, "upstream_model_verified": upstream_ok,
            "read_scope_violations": read_scope_violations,
            "search_scope_violations": search_scope_violations,
            "edit_scope_violations": edit_scope_violations,
            "result_subtype": final_result.get("subtype"),
            "result_is_error": (bool(final_result.get("is_error"))
                                if result_records else None),
            "failure_class": failure_class,
            "failure_detail": result_error,
            "ok": ok, "trace": str(out),
            "trace_sha256": (hashlib.sha256(out.read_bytes()).hexdigest()
                              if out.is_file() else None),
            "prompt": str(pf),
            "prompt_sha256": hashlib.sha256(pf.read_bytes()).hexdigest(),
            "settings": str(settings_path),
            "settings_sha256": hashlib.sha256(settings_path.read_bytes()).hexdigest(),
            "stderr": str(err),
            "stderr_sha256": (hashlib.sha256(err.read_bytes()).hexdigest()
                              if err.is_file() else None),
            "capability_disclosure": (
                copy.deepcopy(capability_disclosure.get("record"))
                if capability_disclosure is not None else None
            ),
        }
        if prompt_template_sha256 is not None:
            if not SHA256_RE.fullmatch(str(prompt_template_sha256)):
                raise RuntimeError(
                    "profiled CC session has an invalid prompt-template hash"
                )
            session_record.update({
                "timeout_seconds": timeout,
                "prompt_template_sha256": prompt_template_sha256,
                "denied_read_paths": [
                    str(Path(path)) for path in (denied_read_paths or [])
                ],
                "denied_directory_reads": [
                    str(Path(path)) for path in (denied_directory_reads or [])
                ],
                "profile_tool_surface_verified":
                    profile_tool_surface_ok,
            })
        with self._cc_lock:
            provenance = json.loads(
                self.provenance_path.read_text(encoding="utf-8")
            )
            provenance.setdefault("meta_sessions", []).append(session_record)
            provenance["meta_resolved_models"] = sorted(
                set(provenance.get("meta_resolved_models", [])) |
                set(session_record["resolved_models"])
            )
            self._write_json_atomic(self.provenance_path, provenance)
            self.last_cc_session = session_record
            self._cc_sessions_by_name[name] = session_record
        self.log(f"CC session {name} done (launcher said: {(outp or '').strip()[-80:]})")
        # Two-tier verdict.  Contamination-critical violations (answer keys,
        # champion knowledge, sibling runs, secrets, control ledger) remain a
        # hard stop.  Any other out-of-scope access fails THIS SESSION and
        # flows into the existing retry paths instead of killing the driver
        # (observed 2026-08-07: a reducer Glob of its own round directory
        # killed a whole run).
        fatal_error = None
        soft_error = None

        def record(label: str, violations: list) -> None:
            nonlocal fatal_error, soft_error
            if not violations:
                return
            fatal = [
                v for v in violations
                if self._violation_is_contamination(
                    v.get("resolved_path") if isinstance(v, dict) else str(v)
                )
            ]
            if fatal:
                if fatal_error is None:
                    fatal_error = (
                        f"CC session {label} (contamination-critical): "
                        f"{fatal[:10]}"
                    )
            elif soft_error is None:
                soft_error = f"CC session {label}: {violations[:10]}"

        record("read outside its scope", read_scope_violations)
        record("searched outside its scope", search_scope_violations)
        record("attempted Edit outside its scope", edit_scope_violations)
        if profile_tool_surface_ok is False:
            fatal_error = fatal_error or (
                "locked-profile CC session exposed a tool surface other than "
                "exactly Read/Edit"
            )
        record("wrote outside its scope", strays)
        if fatal_error is not None:
            if profile_phase is not None:
                raise ProfileArmIneligible(profile_phase)
            raise RuntimeError(fatal_error)
        if soft_error is not None:
            if profile_phase is not None:
                # Locked-profile arms keep their stricter all-or-nothing rule.
                raise ProfileArmIneligible(profile_phase)
            self.log(
                f"CC session {name} non-critical scope violation "
                f"(session failed, driver continues): {soft_error[:300]}"
            )
            return False
        return ok

    # ---------- evaluation ----------
    def evaluate(self, pkg, task_ids, k, tag, *, tag_must_match: bool = True):
        if self.generate_only:
            raise RuntimeError(
                "downstream evaluation is forbidden in --generate-only mode"
            )
        if self.ds is None:
            raise RuntimeError("downstream evaluator is unavailable in qualification-only mode")
        self.integrity()
        # Phase-level recovery: a sealed artifact whose full identity matches
        # (tag, tasks, k, package tree hash, agent, models, protocol) is the
        # same evaluation — reparse it instead of re-running and re-charging.
        # Covers both crash resume within a round and cross-run reuse of the
        # deterministic round-0/round-1 baselines.
        reused = self._reuse_initialization_eval(
            pkg, task_ids, k, tag, tag_must_match=tag_must_match)
        if reused is not None:
            mean = (statistics.mean(
                [statistics.mean(v) for v in reused.matrix.values()])
                if reused.matrix else 0.0)
            self.log(
                f"eval[{tag}] {Path(pkg).name if pkg else 'bare'} on "
                f"{len(task_ids)} tasks k={k}: {mean*100:.1f}% (reused, 0 sims)"
            )
            return reused
        self._count_sims(len(task_ids), k)
        self.save_state()
        t0 = time.time()
        try:
            res = self.ds.evaluate(pkg, task_ids, seeds=k, tag=tag)
        finally:
            # Persist both successful evaluations and charged failed attempts.
            # Integrity is rechecked even when Tau2 raises.
            self.integrity()
            provenance = json.loads(self.provenance_path.read_text(encoding="utf-8"))
            provenance["downstream_artifacts"] = list(self.ds.artifacts)
            self._write_json_atomic(self.provenance_path, provenance)
        mean = (statistics.mean([statistics.mean(v) for v in res.matrix.values()])
                if res.matrix else 0.0)
        self.log(f"eval[{tag}] {Path(pkg).name if pkg else 'bare'} on {len(task_ids)} tasks "
                 f"k={k}: {mean*100:.1f}% ({time.time()-t0:.0f}s)")
        return res

    def _reuse_initialization_eval(self, pkg, task_ids, k, tag,
                                   *, tag_must_match: bool = True):
        """Reparse one sealed evaluation instead of charging it again.

        A sealed artifact is the same evaluation when every identity field
        matches; the search covers this run's artifacts (crash resume within
        a round) plus an optional prior run's artifacts (--reuse-artifacts-run,
        valid for the deterministic from-zero baselines because the neutral
        M0 tree hash is identical across runs)."""
        if self.ds is None:
            return None
        requested = [str(task) for task in task_ids]
        skill_mode = pkg is not None
        package_digest = self._package_digest(Path(pkg)) if skill_mode else None
        expected_agent = (
            self.profile.skill_agent if skill_mode else self.profile.bare_agent
        )
        worker = {
            "model": self.a.worker_model,
            "reasoning_effort": self.a.worker_reasoning,
            "llm_args": self.ds.worker_llm_args_record,
        }
        simulator = {
            "model": self.a.simulator_model,
            "reasoning_effort": self.a.simulator_reasoning,
            "llm_args": self.ds.simulator_llm_args_record,
        }
        candidates = [
            *self.ds.artifacts,
            *getattr(self, "_foreign_artifacts", []),
        ]
        matches = [
            artifact for artifact in candidates
            if (
                artifact.get("status") == "success" and
                (artifact.get("tag") == tag if tag_must_match else True) and
                artifact.get("requested_task_ids") == requested and
                artifact.get("num_trials") == k and
                artifact.get("package_tree_sha256") == package_digest and
                artifact.get("agent_implementation") == expected_agent and
                artifact.get("benchmark_protocol") == self.benchmark_protocol and
                artifact.get("worker") == worker and
                artifact.get("simulator") == simulator
            )
        ]
        if not matches:
            return None
        # Never choose the best-looking duplicate. The first sealed treatment is
        # the deterministic checkpoint if an older run launched the same tag twice.
        artifact = min(matches, key=lambda item: int(item.get("index", 0)))
        results_path = Path(str(artifact.get("results") or ""))
        fingerprint_path = Path(str(artifact.get("fingerprint") or ""))
        for path, expected_hash, label in (
                (results_path, artifact.get("results_sha256"), "results"),
                (fingerprint_path, artifact.get("fingerprint_sha256"), "fingerprint")):
            if expected_hash is None and label == "fingerprint" and not skill_mode:
                continue
            if (not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash or "")) or
                    not path.is_file() or self._sha256_file(path) != expected_hash):
                raise RuntimeError(
                    f"sealed initialization {label} artifact is invalid: {path}"
                )
        require_embedded = bool(
            skill_mode and getattr(self.ds, "resume_attempts", 0) > 0
        )
        result = self.ds._parse(
            results_path, str(fingerprint_path), requested, k,
            expected_agent=expected_agent,
            require_fingerprint=skill_mode,
            expected_benchmark_commit=artifact.get("benchmark_git_commit"),
            benchmark_inputs=artifact.get("benchmark_inputs"),
            require_embedded_fingerprint=require_embedded,
        )
        if (result.seeds != artifact.get("trial_seeds") or
                result.benchmark_git_commit != artifact.get("benchmark_git_commit") or
                result.benchmark_input_sha256 !=
                (artifact.get("benchmark_inputs") or {}).get("combined_sha256")):
            raise RuntimeError(
                f"sealed initialization artifact proof changed while reparsing {tag}"
            )
        result.artifact = copy.deepcopy(artifact)
        origin = artifact.get("reused_from_run")
        self.log(
            f"eval[{tag}] REUSED sealed artifact "
            f"index={artifact.get('index')}"
            + (f" from run {origin}" if origin else "")
        )
        return result

    # ---------- memory files ----------
    @staticmethod
    def _package_diff(base: Path, candidate: Path) -> str:
        """Return an auditable unified diff between two Skill Memory packages."""
        base = Path(base)
        candidate = Path(candidate)
        if not base.is_dir() or not candidate.is_dir():
            raise RuntimeError("round review requires both base and candidate packages")

        def package_files(root: Path) -> dict[str, Path]:
            return {
                path.relative_to(root).as_posix(): path
                for path in root.rglob("*")
                if (path.is_file() and "__pycache__" not in path.relative_to(root).parts
                    and path.suffix.lower() not in {".pyc", ".pyo"})
            }

        base_files = package_files(base)
        candidate_files = package_files(candidate)
        chunks: list[str] = []
        for relative in sorted(set(base_files) | set(candidate_files)):
            left_path = base_files.get(relative)
            right_path = candidate_files.get(relative)
            try:
                left = (left_path.read_text(encoding="utf-8").splitlines(keepends=True)
                        if left_path else [])
                right = (right_path.read_text(encoding="utf-8").splitlines(keepends=True)
                         if right_path else [])
            except UnicodeDecodeError:
                left_hash = (hashlib.sha256(left_path.read_bytes()).hexdigest()
                             if left_path else "missing")
                right_hash = (hashlib.sha256(right_path.read_bytes()).hexdigest()
                              if right_path else "missing")
                chunks.append(
                    f"Binary file changed: {relative} ({left_hash} -> {right_hash})\n"
                )
                continue
            chunks.extend(difflib.unified_diff(
                left, right,
                fromfile=f"base/{relative}" if left_path else "/dev/null",
                tofile=f"candidate/{relative}" if right_path else "/dev/null",
            ))
        return "".join(chunks) or "(no package changes)\n"

    def _latest_round_review(self) -> str:
        latest: tuple[int, Path] | None = None
        completed = int(self.state.get("round_done", -1))
        for path in self.run_dir.glob("round_*/round_review.md"):
            match = re.fullmatch(r"round_(\d+)", path.parent.name)
            if not match:
                continue
            rnd = int(match.group(1))
            if rnd <= completed and (latest is None or rnd > latest[0]):
                latest = (rnd, path)
        return (latest[1].read_text(encoding="utf-8").strip()
                if latest else "(no validated round review yet)")

    def _write_round_review(self, rnd: int, plan: dict, verdict: dict) -> str:
        """Bind the exact package change to its paired validation outcome."""
        rdir = self.run_dir / f"round_{rnd}"
        review_path = rdir / "round_review.md"
        if review_path.is_file():
            return review_path.read_text(encoding="utf-8")

        package_diff = self._package_diff(Path(self._working_base_package()), self.cand_dir)
        diff_path = rdir / "skill_memory_diff.patch"
        if diff_path.is_file():
            if diff_path.read_text(encoding="utf-8") != package_diff:
                raise RuntimeError(f"conflicting round package diff: {diff_path}")
        else:
            self._write_text_atomic(diff_path, package_diff)

        state_path = rdir / "state.md"
        prior_memory = (state_path.read_text(encoding="utf-8")
                        if state_path.is_file() else "(none)")
        repair_path = rdir / "repair_evidence.md"
        repair_evidence = (repair_path.read_text(encoding="utf-8")
                           if repair_path.is_file() else "(none)")
        regression_path = rdir / "regression_evidence.md"
        regression_evidence = (regression_path.read_text(encoding="utf-8")
                               if regression_path.is_file() else "(none)")
        visible_diff = package_diff[:MAX_ROUND_REVIEW_PROMPT_DIFF_CHARS]
        if len(package_diff) > len(visible_diff):
            visible_diff += "\n...[prompt copy truncated; exact diff remains in artifact]...\n"

        prompt = f"""You are the post-round reviewer for a recursive Skill Memory evolution loop.

Write a concise plain-Markdown review of round {rnd}. Bind the exact package edit
to the paired validation outcome. Compare it with prior validated memory and call
out any rejected intervention that was repeated without a materially different
mechanism or activation boundary. Stay under 500 words; summarize mechanisms
rather than restating complete card text, hashes, or the full plan.

Use exactly these headings:
# Round {rnd} evolution review
## Change
## Observed outcome
## Attribution
## Next-round constraint

Separate measured facts from causal hypotheses and state confidence. A rejected
aggregate candidate is evidence that the complete candidate should not replace
the baseline; it is not proof that every individual edit was harmful. Treat the
complete Skill Memory as the iteration unit. Do not propose component/card
ablations or isolated per-card trials. Do not invent benchmark-specific causal
rules. The next-round constraint must say what must not be repeated unchanged
and what evidence a materially different revision must address.

Review the causal chain, not just the prose diff: exact reachable pre-failure
event -> trigger -> delivered/checked content -> immediate worker behavior ->
paired outcome. If activation or the behavior transition is not observed, label
that link unproven instead of claiming the rule worked. Compare this chain with
prior rejected interventions; renamed cards or narrower wording are still a
semantic repeat when the reachable trigger and expected behavior are unchanged.
In `## Next-round constraint`, name the rejected mechanism as this compact chain
so the next round can distinguish a real mechanism change from paraphrasing.

Numerical discipline: repair_base/repair_cand and net_pp are already aggregate
rates. Report them as given (or as percentages) and never infer a numerator or
denominator unless the input explicitly provides one. Paired repair evidence
lists only trials whose binary outcome changed; an omitted task or trial means no
changed cell was recorded, not that it was unevaluated.

PRIOR VALIDATED CROSS-ROUND MEMORY:
{prior_memory}

PLAN:
{json.dumps(plan, ensure_ascii=False, indent=1)}

GATE VERDICT:
{json.dumps(verdict, ensure_ascii=False, indent=1)}

EXACT SKILL MEMORY DIFF:
```diff
{visible_diff}
```

PAIRED REPAIR EVIDENCE:
{repair_evidence}

CELLS LOST SINCE LAST ROUND (paired, same seed; a cell with no new mechanism
contact is environment divergence and must not be attributed to the package):
{regression_evidence}
"""
        try:
            import openai
            client = openai.OpenAI(
                api_key=_read_env("OPENAI_API_KEY"),
                base_url=_read_env("OPENAI_BASE_URL"), timeout=180,
            )
            response = client.chat.completions.create(
                model=self.a.meta_model, temperature=0.0,
                reasoning_effort=self.a.meta_reasoning,
                messages=[{"role": "user", "content": prompt}],
            )
            actual_model = getattr(response, "model", None)
            if actual_model != self.a.meta_model:
                raise MetaTreatmentError(
                    f"round review resolved {actual_model!r}, "
                    f"expected {self.a.meta_model!r}"
                )
            review = (response.choices[0].message.content or "").strip()
            if not review:
                raise RuntimeError("round review model returned empty content")
            if not review.startswith(f"# Round {rnd} evolution review"):
                review = f"# Round {rnd} evolution review\n\n" + review
            review = review[:MAX_ROUND_REVIEW_CHARS].rstrip() + "\n"
            provenance = json.loads(
                self.provenance_path.read_text(encoding="utf-8")
            )
            resolved = set(provenance.get("meta_resolved_models", []))
            resolved.add(actual_model)
            calls = list(provenance.get("meta_api_calls", []))
            calls.append({
                "phase": "round_review", "round": rnd,
                "requested_model": self.a.meta_model,
                "resolved_model": actual_model,
                "reasoning_effort": self.a.meta_reasoning,
                "temperature": 0.0, "timestamp": _now(),
            })
            self._update_provenance(
                meta_resolved_models=sorted(resolved), meta_api_calls=calls,
            )
        except MetaTreatmentError:
            raise
        except Exception as exc:
            review = f"""# Round {rnd} evolution review

## Change
The exact baseline-to-candidate change is recorded in `skill_memory_diff.patch`.

## Observed outcome
The gate returned `{verdict.get('gate_stage', 'unknown')}` with accept={verdict.get('accept')}.

## Attribution
Automated causal review was unavailable: {type(exc).__name__}. The measured gate
outcome remains valid, but no additional causal claim is supported.

## Next-round constraint
Do not repeat this candidate unchanged. Read the exact diff and paired evidence,
then make a materially different whole-Skill-Memory revision that addresses the
observed regressions without discarding measured improvements.
"""
            self.log(f"round review fallback used: {type(exc).__name__}")
        self._write_text_atomic(review_path, review)
        return review

    def read_lessons(self) -> str:
        return (self.lessons_path.read_text(encoding="utf-8")
                if self.lessons_path.exists() else "(no lessons yet)")

    def append_lesson(self, rnd: int, ground_truth: dict):
        """Phase L: doubao distills ONE lesson line from the round's ground truth."""
        try:
            import openai
            client = openai.OpenAI(api_key=_read_env("OPENAI_API_KEY"),
                                   base_url=_read_env("OPENAI_BASE_URL"), timeout=120)
            repair_evidence_path = (
                self.run_dir / f"round_{rnd}" / "repair_evidence.md"
            )
            repair_evidence = (
                repair_evidence_path.read_text(encoding="utf-8")
                if repair_evidence_path.is_file() else "(none)"
            )
            round_review_path = self.run_dir / f"round_{rnd}" / "round_review.md"
            round_review = (
                round_review_path.read_text(encoding="utf-8")
                if round_review_path.is_file() else "(none)"
            )
            msg = ("You are the meta-agent's memory writer. Below is the GROUND TRUTH of "
                   "one evolution round (what was planned, what was changed, and the "
                   "validation-gate numbers). Distill ONE lesson for future rounds: "
                   "imperative, concrete, <=300 chars, mention the component/strategy and "
                   "the observed outcome. Respect causal_interpretation: an inconclusive "
                   "aggregate rejection may justify narrower activation or more trials, "
                   "but must not be phrased as proof that every constituent fix is harmful. "
                   "Use mechanism_delta when present. Preserve the round review's concrete "
                   "regression constraint and its rejected event-to-behavior mechanism, so a "
                   "later round cannot evade it by renaming a card or changing wording. Treat "
                   "the complete Skill Memory as the iteration "
                   "unit; do not recommend component/card ablations or isolated per-card "
                   "trials. If the ground truth contains a validation_referee record with "
                   "errors, the candidate never reached behavioural evaluation: the lesson "
                   "MUST name the failing referee stage and quote the decisive error "
                   "(for example a missing required field or an invalid cfg key), and must "
                   "not attribute the failure to downstream scores. "
                   "Output ONLY the lesson line.\n\n"
                   + json.dumps(ground_truth, ensure_ascii=False, indent=1)
                   + "\n\nPOST-ROUND REVIEW:\n"
                   + round_review
                   + "\n\nSANITIZED PAIRED REPAIR EVIDENCE:\n"
                   + repair_evidence)
            r = client.chat.completions.create(
                model=self.a.meta_model, temperature=0.0,
                reasoning_effort=self.a.meta_reasoning,
                messages=[{"role": "user", "content": msg}])
            actual_model = getattr(r, "model", None)
            if actual_model != self.a.meta_model:
                raise MetaTreatmentError(
                    f"lesson call resolved {actual_model!r}, expected {self.a.meta_model!r}"
                )
            # No hard truncation: the lesson is the round's only mandatory
            # cross-round memory, and a clipped sentence misleads the next
            # round. Length is governed by the prompt instruction alone.
            lesson = (r.choices[0].message.content or "").strip().replace("\n", " ")
            provenance = json.loads(self.provenance_path.read_text(encoding="utf-8"))
            resolved = set(provenance.get("meta_resolved_models", []))
            resolved.add(actual_model)
            calls = list(provenance.get("meta_api_calls", []))
            calls.append({"phase": "lesson", "round": rnd,
                          "requested_model": self.a.meta_model,
                          "resolved_model": actual_model,
                          "reasoning_effort": self.a.meta_reasoning,
                          "temperature": 0.0, "timestamp": _now()})
            self._update_provenance(meta_resolved_models=sorted(resolved),
                                    meta_api_calls=calls)
        except MetaTreatmentError:
            raise
        except Exception as e:
            lesson = f"(auto-distill failed: {e}) verdict={ground_truth.get('verdict')}"
        line = f"Round {rnd} (run {self.a.run_id}): {lesson}\n"
        artifact = self.run_dir / f"round_{rnd}_lesson.txt"
        if artifact.exists():
            raise RuntimeError(f"refusing to overwrite atomic lesson artifact: {artifact}")
        self._write_text_atomic(artifact, line)
        self._sync_lessons()
        self.log(f"lesson appended: {line.strip()[:120]}")

    def _regression_summary(self) -> str:
        """One line per cell that passed last round and fails now.

        Kept deliberately small: this text reaches every diagnosis session, so
        the transcripts stay in regression_evidence.md and only the verdict
        travels here.
        """
        rows = getattr(self, "_round_regressions", None)
        if rows is None:
            return "(no paired comparison available yet)"
        if not rows:
            return "(none - no cell that passed last round fails now)"
        lines = []
        for item in rows:
            contact = ", ".join(item["new_contact"])
            lines.append(
                f"- task {item['task']} trial {item['trial']}: "
                + (f"NEW mechanism contact [{contact}] -> attributable, see "
                   f"regression_evidence.md"
                   if item["attributable"] else
                   "no new mechanism fired -> environment divergence, do NOT "
                   "diagnose as a Skill Memory defect")
            )
        return "\n".join(lines)

    def _trial_dump_path(self, rnd: int) -> Path:
        return self.run_dir / f"round_{rnd}" / "trial_evidence.json"

    def _write_regression_evidence(self, rnd: int, rdir: Path, train_res) -> list:
        """Persist this round's trials and pair every pass -> fail cell.

        The comparison is against the previous round's own evaluation, which is
        already seed-paired with this one, so no extra simulation is needed. A
        flipped cell is only attributable to the Skill Memory if a mechanism
        fired in the new trial that did not fire in the old one; cells without
        that contact are reported as environment divergence so the Meta-Agent
        cannot build a causal story on them.
        """
        current = getattr(train_res, "trial_evidence", {}) or {}
        rdir.mkdir(parents=True, exist_ok=True)
        dump_path = self._trial_dump_path(rnd)
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(dump_path, current)

        prior_path = self._trial_dump_path(rnd - 1)
        if rnd < 2 or not prior_path.is_file():
            return []
        prior = json.loads(prior_path.read_text(encoding="utf-8"))

        def by_trial(records):
            return {int(r["trial"]): r for r in (records or [])}

        def fired(record):
            block = (record or {}).get("fires") or {}
            counters = block.get("counters") if isinstance(block, dict) else None
            names = set()
            explained = set()
            for event in (block.get("events") or []) if isinstance(block, dict) else []:
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("kind") or "event")
                label = event.get("checker") or event.get("deliverer")
                if label:
                    names.add(f"{kind}:{label}")
                    explained.add(kind)
                else:
                    names.add(kind)
                    explained.add(kind)
            for key, value in (counters or {}).items():
                if isinstance(value, int) and value > 0 and key not in explained:
                    names.add(key)
            return names

        regressions = []
        for task, records in sorted(current.items(), key=lambda kv: _task_sort_key(kv[0])):
            old_by_trial = by_trial(prior.get(task))
            for record in records:
                trial = int(record["trial"])
                old = old_by_trial.get(trial)
                if old is None or old.get("passed") is not True or record.get("passed") is not False:
                    continue
                if old.get("seed") != record.get("seed"):
                    continue
                new_contact = sorted(fired(record) - fired(old))
                regressions.append({
                    "task": task, "trial": trial, "seed": record.get("seed"),
                    "new_contact": new_contact,
                    "attributable": bool(new_contact),
                    "base": old, "cand": record,
                })

        lines = [
            f"# Round {rnd} regression evidence (paired against round {rnd - 1})",
            "",
            "Cells that PASSED under the previous package and FAIL under this one,"
            " on the same seed. `new mechanism contact` lists mechanisms that fired"
            " here and did not fire before; a cell with no new contact is environment"
            " divergence, not a defect of the Skill Memory, and must not be diagnosed"
            " as one.",
            "",
            "| task | trial | new mechanism contact | attributable |",
            "|---|---|---|---|",
        ]
        for item in regressions:
            lines.append(
                f"| {item['task']} | {item['trial']} | "
                f"{', '.join(item['new_contact']) or '(none)'} | "
                f"{'yes' if item['attributable'] else 'NO - divergence'} |"
            )
        if not regressions:
            lines.append("| (none) | | | |")

        masker = ma_sanitize.Masker(self.gold)
        for item in regressions[:MAX_REPAIR_HISTORY_CELLS]:
            lines += ["", f"## task {item['task']}, trial {item['trial']}: "
                          f"passed before, fails now",
                      "",
                      f"new mechanism contact: "
                      f"{', '.join(item['new_contact']) or '(none - environment divergence)'}"]
            for label, record in (("Previous package (passed)", item["base"]),
                                  ("This package (failed)", item["cand"])):
                lines += [
                    "", f"### {label}", "",
                    "Outcome evidence:",
                    masker.mask(str(record.get("failure_detail") or "(none)"))[:1200],
                    "", "Transcript (compact head + terminal tail):", "```",
                    masker.mask(str(record.get("transcript") or ""))[:MAX_REPAIR_TRANSCRIPT_CHARS],
                    "```",
                ]
        self._write_text_atomic(rdir / "regression_evidence.md",
                                "\n".join(lines).rstrip() + "\n")
        self.log(f"regression evidence: {len(regressions)} cell(s) lost, "
                 f"{sum(1 for r in regressions if r['attributable'])} with mechanism contact")
        return regressions

    def state_md(self, suspects: list) -> str:
        led_tail = ""
        if self.ledger_path.exists():
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
            led_tail = "\n".join(lines[-12:])
        chl = "\n".join(f"- round {c['round']}: {c['summary']}"
                        for c in self.state["changelog"]) or "(package unchanged so far)"
        sus = "\n".join(f"- {s}" for s in suspects) or "(none)"
        latest_review = self._latest_round_review()
        return f"""# Cross-round memory (driver-maintained ground truth)

## Lessons (distilled from validated outcomes — READ AND OBEY)
{self.read_lessons()}

## Package changelog (what is in the current package and why)
{chl}

## Cells lost since last round (paired, same seed)
{self._regression_summary()}

## Regression suspects (tasks that got WORSE after the last accepted change)
{sus}

## Latest validated round review (READ BEFORE PROPOSING CHANGES)
{latest_review}

## Ledger tail (recent attempts, machine-recorded)
{led_tail or '(empty)'}
"""

    def _materialize_history_index(
            self, rnd: int, rdir: Path, failing: set[str],
            filename: str = "history_index.md") -> tuple[Path, list[Path]]:
        """Expose bounded, train-only history for the same failing tasks."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.md", filename):
            raise ValueError("history index filename must be a safe markdown name")
        history_path = rdir / filename
        readable: list[Path] = []
        lines = [
            "# Relevant prior train evidence",
            "",
            ("This index contains only sanitized evidence produced from earlier "
             "training rounds in this run. It contains no dev/test trajectories."),
            "",
        ]
        first_round = max(1, rnd - HISTORY_EVIDENCE_ROUNDS)
        task_key = _task_sort_key
        for prior in range(first_round, rnd):
            prior_dir = self.run_dir / f"round_{prior}"
            prior_index = prior_dir / "evidence.md"
            prior_repair = prior_dir / "repair_evidence.md"
            prior_review = prior_dir / "round_review.md"
            task_paths = [
                prior_dir / "evidence" / f"task_{task_id}.md"
                for task_id in sorted(failing, key=task_key)
                if (prior_dir / "evidence" / f"task_{task_id}.md").is_file()
            ]
            if not (task_paths or prior_repair.is_file() or prior_review.is_file()):
                continue
            if prior_index.is_file():
                readable.append(prior_index)
            readable.extend(task_paths)
            if prior_repair.is_file():
                readable.append(prior_repair)
            if prior_review.is_file():
                readable.append(prior_review)
            lines.append(f"## Round {prior}")
            if prior_index.is_file():
                lines.append(f"- `{prior_index.relative_to(ROOT).as_posix()}`")
            lines.extend(
                f"- `{path.relative_to(ROOT).as_posix()}`"
                for path in task_paths
            )
            if prior_repair.is_file():
                lines.append(
                    f"- `{prior_repair.relative_to(ROOT).as_posix()}` "
                    "(paired baseline/candidate repair evidence)"
                )
            if prior_review.is_file():
                lines.append(
                    f"- `{prior_review.relative_to(ROOT).as_posix()}` "
                    "(exact change-to-outcome review)"
                )
            lines.append("")
        if not readable:
            lines.append("No same-task evidence exists in the previous bounded rounds.")
        self._write_text_atomic(history_path, "\n".join(lines).rstrip() + "\n")
        return history_path, readable

    @staticmethod
    def _package_prompt_files(package: Path) -> list[Path]:
        files = [Path(package) / "manifest.yaml"]
        em_root = Path(package) / "em"
        if em_root.is_dir():
            files.extend(sorted(em_root.rglob("*.md")))
        return files

    @staticmethod
    def _embedded_file_context(paths: list[Path]) -> str:
        """Inline frozen text inputs so diagnosis cannot loop on Read calls."""
        blocks: list[str] = []
        seen: set[Path] = set()
        for raw_path in paths:
            path = Path(raw_path).resolve()
            if path in seen:
                continue
            seen.add(path)
            if not path.is_file():
                raise RuntimeError(f"diagnosis context file is missing: {path}")
            label = os.path.relpath(path, ROOT).replace("\\", "/")
            blocks.append(
                f"\n=== BEGIN FILE: {label} ===\n"
                f"{path.read_text(encoding='utf-8')}\n"
                f"=== END FILE: {label} ==="
            )
        return "\n".join(blocks)

    @staticmethod
    def _plan_hypothesis_signature(plan: dict) -> str:
        """Identity fields that a validator-feedback correction cannot change."""
        clusters = []
        for cluster in plan.get("clusters", []):
            fixes = []
            for fix in cluster.get("fixes", []):
                fixes.append({
                    key: fix.get(key)
                    for key in (
                        "action", "card_id", "em_type", "trigger_event",
                        "trigger_tool", "deliverer", "checker", "field",
                    )
                    if key in fix
                })
            clusters.append({
                key: cluster.get(key)
                for key in (
                    "cluster_id", "evidence_tasks", "component", "failure_mode",
                    "action_opportunity", "patchability", "symptom",
                    "failing_step", "opportunity_evidence", "evidence",
                )
            } | {"fixes": fixes})
        return hashlib.sha256(
            json.dumps(clusters, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _retrieval_probe_targets(self, plan: dict) -> set[str]:
        card_ids = {
            str(fix.get("card_id"))
            for cluster in plan.get("clusters", [])
            for fix in cluster.get("fixes", [])
            if fix.get("action") in {"add_card", "edit_card"}
            and fix.get("em_type") in {"knowledge", "procedure"}
            and fix.get("card_id")
        }
        planned_retrieval = any(
            fix.get("action") in {"set_delivery", "edit_delivery"}
            and fix.get("deliverer") == "need_driven_retrieval"
            for cluster in plan.get("clusters", [])
            for fix in cluster.get("fixes", [])
        )
        if planned_retrieval or "need_driven_retrieval" in self.pkg_deliverers():
            return card_ids
        return set()

    # ---------- evidence ----------
    def write_evidence(self, rnd: int, res, rdir: Path) -> set:
        masker = ma_sanitize.Masker(self.gold)
        ev_dir = rdir / "evidence"
        ev_dir.mkdir(parents=True, exist_ok=True)
        failing = sorted(res.failing, key=lambda t: statistics.mean(res.matrix[t]))
        lines = [f"# Round {rnd} evidence — failing tasks on the CURRENT package",
                 f"(train split, k={self.a.k}; ids/emails are MASKED with placeholders)",
                 "", "| task | pass | detail file |",
                 "|---|---|---|"]
        for t in failing:
            rate = statistics.mean(res.matrix[t])
            records = list((getattr(res, "trial_evidence", {}) or {}).get(t) or [])
            trial_blocks: list[str] = []
            for record in records:
                trial = record.get("trial")
                passed = bool(record.get("passed"))
                status = "PASS COUNTERFACTUAL" if passed else "FAIL"
                opportunity = record.get("action_opportunity") or {}
                opportunity_status = str(opportunity.get("status") or "unknown")
                opportunity_basis = str(opportunity.get("basis") or "unspecified")
                opportunity_reason = masker.mask(str(opportunity.get("reason") or ""))
                required_actions = opportunity.get("required_actions") or []
                trial_detail = masker.mask(
                    str(record.get("failure_detail") or "(no failure detail; passed)")
                )
                trial_transcript = masker.mask(
                    str(record.get("transcript") or "(no transcript)")
                )
                trial_blocks.append(
                    f"## Trial {trial} - {status}\n"
                    f"seed: {record.get('seed')}; reward: {record.get('reward')}; "
                    f"termination_reason: {record.get('termination_reason')}\n"
                    f"action_opportunity_O: {opportunity_status}; "
                    f"basis: {opportunity_basis}; required_actions: "
                    f"{json.dumps(required_actions, ensure_ascii=False)}\n"
                    f"opportunity_reason: {opportunity_reason}\n\n"
                    f"### Outcome evidence\n{trial_detail}\n\n"
                    f"### Transcript (compact head + terminal tail)\n"
                    f"```\n{trial_transcript}\n```\n"
                )
            if trial_blocks:
                evidence_body = (
                    f"## Trial-level evidence (all {len(records)} trials)\n\n"
                    + "\n".join(trial_blocks)
                )
            else:
                # Compatibility for synthetic/unit Results produced by older
                # adapters while they migrate to the trial-level contract.
                detail = masker.mask(res.failure_details.get(t, "(no detail)"))
                traj = masker.mask((res.trajectories.get(t) or ["(no transcript)"])[0])
                evidence_body = (
                    f"## WHAT FAILED (answer key)\n{detail}\n\n"
                    f"## Failing transcript (compact head + terminal tail)\n"
                    f"```\n{traj}\n```\n"
                )
            (ev_dir / f"task_{t}.md").write_text(
                f"# Task {t} - pass {rate*100:.0f}%\n\n{evidence_body}",
                encoding="utf-8")
            lines.append(
                f"| {t} | {rate*100:.0f}% | evidence/task_{t}.md |"
            )
        passing = sorted(set(res.matrix) - set(res.failing), key=_task_sort_key)
        lines += ["", f"Passing train tasks (do NOT regress these): {passing}",
                  "", "Adapter action-opportunity observations (all scheduled "
                      "trials; unknown is unresolved and requires trajectory "
                      "diagnosis): "
                      f"{json.dumps(getattr(res, 'opportunity_summary', {}) or {}, ensure_ascii=False)}",
                  "", f"Mechanism fingerprint of this eval (delivery/checker counters): "
                      f"{json.dumps(res.fingerprint)}"]
        (rdir / "evidence.md").write_text("\n".join(lines), encoding="utf-8")
        return set(failing)

    def _write_repair_evidence(
            self, rnd: int, baseline, candidate,
            repair_tasks: list[str]) -> list[dict[str, object]]:
        """Persist bounded train-only pairs whose outcomes changed."""
        masker = ma_sanitize.Masker(self.gold)
        changes: list[dict[str, object]] = []
        task_key = _task_sort_key
        for task in sorted((str(value) for value in repair_tasks), key=task_key):
            base_row = list(baseline.matrix.get(task, []))
            cand_row = list(candidate.matrix.get(task, []))
            seeds = list(baseline.seeds.get(task, []))
            if len(base_row) != len(cand_row) or len(base_row) != len(seeds):
                raise RuntimeError(
                    f"repair evidence has inconsistent trial coverage for task {task}"
                )
            for trial, (base_value, cand_value) in enumerate(
                    zip(base_row, cand_row)):
                if abs(cand_value - base_value) <= DENSE_CHANGE_EPS:
                    continue
                changes.append({
                    "task": task,
                    "trial": trial,
                    "seed": seeds[trial],
                    "direction": (
                        "improved" if cand_value > base_value else "regressed"
                    ),
                    "baseline": _evidence_score(base_value),
                    "candidate": _evidence_score(cand_value),
                })

        lines = [
            f"# Round {rnd} paired repair evidence",
            "",
            ("Train-only, same-task/same-trial/same-seed comparison. Values and "
             "transcripts are masked; no dev/test evidence is included."),
            "",
            "| task | trial | seed | direction | baseline | candidate |",
            "|---|---:|---:|---|---:|---:|",
        ]
        for change in changes:
            lines.append(
                f"| {change['task']} | {change['trial']} | {change['seed']} | "
                f"{change['direction']} | {change['baseline']} | "
                f"{change['candidate']} |"
            )
        if not changes:
            lines.append("| (none) | | | | | |")

        def trial_record(result, task: str, trial: int) -> dict:
            records = (getattr(result, "trial_evidence", {}) or {}).get(task) or []
            return next(
                (record for record in records
                 if int(record.get("trial", -1)) == trial),
                {},
            )

        def clipped(value: object, limit: int) -> str:
            text = masker.mask(str(value or "(none)"))
            if len(text) <= limit:
                return text
            marker = "\n...[middle omitted; terminal tail preserved]...\n"
            room = limit - len(marker)
            head = room // 2
            return text[:head] + marker + text[-(room - head):]

        detailed = sorted(
            changes,
            key=lambda change: (
                0 if change["direction"] == "regressed" else 1,
                task_key(change["task"]),
                int(change["trial"]),
            ),
        )[:MAX_REPAIR_HISTORY_CELLS]
        for change in detailed:
            task = str(change["task"])
            trial = int(change["trial"])
            lines.extend([
                "",
                f"## Task {task}, trial {trial}: {change['direction']}",
            ])
            for label, result in (("Baseline", baseline), ("Candidate", candidate)):
                record = trial_record(result, task, trial)
                lines.extend([
                    "",
                    f"### {label}",
                    "",
                    "Outcome evidence:",
                    clipped(record.get("failure_detail"), 1600),
                    "",
                    "Transcript (compact head + terminal tail):",
                    "```",
                    clipped(
                        record.get("transcript"),
                        MAX_REPAIR_TRANSCRIPT_CHARS,
                    ),
                    "```",
                ])

        artifact = self.run_dir / f"round_{rnd}" / "repair_evidence.md"
        content = "\n".join(lines).rstrip() + "\n"
        if artifact.exists():
            if artifact.read_text(encoding="utf-8") != content:
                raise RuntimeError(f"conflicting repair evidence: {artifact}")
            return changes
        self._write_text_atomic(artifact, content)
        return changes

    # ---------- schema rules for Phase P ----------
    SCHEMA_RULES = """\
# Package schema (STRICT — ma_lint will reject violations)

manifest.yaml top-level sections (ONLY these are read): name, wm, grounding,
delivery, checkers, gate, feasibility, board.

Cards live at em/<type>/<card_id>.md where type in {knowledge, procedure,
action_result}. Card file format:
---
id: <card_id>
type: <knowledge|procedure|action_result>
trigger:            # REQUIRED whenever the selected carrier matches event/tool
  event: <the exact intervention event from plan.trigger_event>
  tool: <exact tool name>
---
<body: a worked exemplar distilled from the policy — Purpose / the RULE with
policy citation / a SIMULATED correct call with PLACEHOLDER ids / common
mistakes. NEVER a real id.>

manifest section structure (keys OUTSIDE these sets are silently ignored):
  wm: entry_kind, manager, manager_cfg, schema
  wm.schema: binding_key, collection_key, machine_id_pattern, max_entries, board_marker
      (schema fields go under wm.schema.<field>, NEVER directly under wm;
       grounding.matcher=receipt_binding_match REQUIRES wm.schema.binding_key)
  gate: lines            (value: all_pending | authorized_only — nothing else)
  board: use only fields disclosed by the current adapter capability contract
  feasibility: oracle, cfg

delivery / checkers use `- use: <builtin name>` + optional `cfg: {...}`.
cfg keys MUST be the builtin's constructor parameters — nothing else
(unknown keys crash the package at load). Builtin menus:
  deliverers: exemplar_bounce, state_reminder, need_driven_retrieval,
              embedding_retrieval, standing_inject, boundary_inject
  checkers:   use only names disclosed by the current adapter
  wm.entry_kind: service_request, generic_item, service_request_auth, knowledge_need
  wm.manager:    self_maintain_each_turn, blueprint_then_delta
  grounding.matcher: receipt_binding_match, delivery_receipt
Pairing: action_result cards need `- use: exemplar_bounce`; knowledge/procedure
cards need a retrieval-family deliverer, except a checker-routed card explicitly
supported by the current adapter contract.
Knowledge used by standing_inject needs trigger.event=turn_start. Knowledge used
by boundary_inject needs the configured event/tool pair. A configured carrier is
not sufficient when the card omits metadata that carrier matches.
Formal autonomous safety: a new procedure paired with need_driven_retrieval must
use cfg `scope_by_tool: true`, `max_per_episode: 1`, and `settle: false`.
Tool-scoped retrieval observes the write-tool ledger at `intent_recorded`; it
therefore requires wm.entry_kind service_request/service_request_auth and a
mutating domain tool. Generic/non-mutating tool decisions are not visible there;
use only an adapter-disclosed carrier that observes their actual event.
An exemplar_bounce paired with a new action_result card must use
`exact_only: true`; unmatched write tools then remain untouched.
"""

    # ---------- phases ----------
    @staticmethod
    def _diagnosis_report_errors(path: Path, task_ids: list[str]) -> list[str]:
        if not path.is_file():
            return ["report is missing"]
        text = path.read_text(encoding="utf-8")
        errors = []
        if text.strip() == "# Pending diagnosis":
            errors.append("report was not written")
        for task_id in task_ids:
            pattern = rf"(?m)^##\s+Task\s+{re.escape(task_id)}\s*$"
            if re.search(pattern, text) is None:
                errors.append(f"missing Task {task_id} section")
        if re.search(r"(?mi)^##\s+Batch consensus\s*$", text) is None:
            errors.append("missing Batch consensus section")
        return errors

    def _parallel_diagnosis_reports(
            self, rnd: int, rdir: Path,
            task_items: list[tuple[str, Path]], disclosure: dict,
            policy_path: Path, diagnosis_protocol_path: Path) -> list[Path]:
        """Map trajectory batches to isolated diagnosis reports in parallel."""
        worker_count = min(self.diagnosis_workers, len(task_items))
        base_size, extra = divmod(len(task_items), worker_count)
        batches: list[list[tuple[str, Path]]] = []
        offset = 0
        for index in range(worker_count):
            size = base_size + (1 if index < extra else 0)
            batches.append(task_items[offset:offset + size])
            offset += size

        report_specs = []
        package_files = self._package_prompt_files(self.cand_dir)
        for index, batch in enumerate(batches, start=1):
            task_ids = [task_id for task_id, _ in batch]
            report_path = rdir / f"diagnosis_batch_{index:02d}.md"
            self._write_text_atomic(report_path, "# Pending diagnosis\n")
            history_index, history_paths = self._materialize_history_index(
                rnd, rdir, set(task_ids),
                filename=f"history_batch_{index:02d}.md",
            )
            required_reads: list[Path] = []
            seen: set[Path] = set()
            for path in [
                rdir / "state.md", *[item[1] for item in batch],
                history_index, *history_paths, policy_path,
                disclosure["manual_path"], disclosure["tool_capabilities_path"],
                diagnosis_protocol_path, *package_files,
            ]:
                resolved = Path(path).resolve()
                if resolved in seen:
                    continue
                if not resolved.is_file():
                    raise RuntimeError(
                        f"parallel diagnosis input is missing: {resolved}"
                    )
                seen.add(resolved)
                required_reads.append(resolved)
            read_list = "\n".join(
                f"- `{os.path.relpath(path, ROOT).replace(chr(92), '/')}`"
                for path in required_reads
            )
            task_list = ", ".join(task_ids)
            report_rel = report_path.relative_to(ROOT).as_posix()
            prompt = f"""You are a diagnosis worker for Recuris recursive evolution, round {rnd}.

Analyze only tasks {task_list}. You may read the assigned sanitized trajectories,
bounded same-task history, policy, capability manual, and current complete Skill
Memory listed below. Do not seek any other evidence or run commands. You cannot
modify Skill Memory or the final plan.

First issue one batched group of Read calls for every required file, including
`{report_rel}`. Then use exactly one Edit call to replace its pending content
with a concise Markdown diagnosis. End immediately after that edit.

The report must contain exactly these headings for task coverage:
{chr(10).join(f'## Task {task_id}' for task_id in task_ids)}
## Batch consensus

Under each task, state the concrete failed step, trace evidence, causal diagnosis,
all implicated E/W/RHO/C components when more than one is needed, and the smallest
supported Skill Memory change. For every proposed change, spell out one causal
chain: the exact reachable pre-failure event, its trigger condition, the memory
content delivered or checked there, and the worker's expected immediate next
behavior. If any link is absent from the failed trace or runtime contract, do not
propose that change. If the trace supports no Skill Memory repair, say that
explicitly. The batch consensus should merge recurring causes, not vote on labels,
and should flag mechanisms that repeat a rejected historical intervention even
when the wording or card scope differs. Do not invent successful actions or later
events absent from the trace.

## Required reads
{read_list}
"""
            report_specs.append({
                "index": index,
                "task_ids": task_ids,
                "report": report_path,
                "prompt": prompt,
                # The round directory is the worker's green area (its
                # evidence, history, and report all live there).
                "readable": [report_path.parent, report_path, *required_reads],
            })

        self.integrity()
        self.ensure_proxy()
        self.log(
            f"parallel diagnosis start: {len(task_items)} task(s), "
            f"{len(report_specs)} worker(s)"
        )
        before = self.inventory()

        def run_worker(spec: dict) -> tuple[bool, list[dict]]:
            failures = []
            for attempt in range(1, DIAGNOSIS_ATTEMPTS + 1):
                if attempt > 1:
                    # Provider token-per-minute limits are the dominant
                    # cause of diagnosis-session failure on evidence-heavy
                    # benchmarks, and those limits reset on a per-minute
                    # window, so an immediate retry lands in the same
                    # exhausted budget. Back off past the window first.
                    time.sleep(min(300.0, 75.0 * (2 ** (attempt - 2))))
                    self._write_text_atomic(
                        spec["report"], "# Pending diagnosis\n"
                    )
                name = (
                    f"r{rnd}_diagnosis_b{spec['index']:02d}_a{attempt}"
                )
                ok = self.cc_session(
                    name, spec["prompt"], D_TOOLS, self.a.cc_timeout,
                    [spec["report"]], spec["readable"],
                    capability_disclosure=disclosure,
                    audit_inventory=False, audit_integrity=False,
                )
                report_errors = self._diagnosis_report_errors(
                    spec["report"], spec["task_ids"]
                )
                if ok and not report_errors:
                    return True, failures
                record = copy.deepcopy(
                    self._cc_sessions_by_name.get(name, {"name": name})
                )
                if report_errors:
                    record.update({
                        "ok": False,
                        "failure_class": "invalid-diagnosis-report",
                        "failure_detail": "; ".join(report_errors),
                    })
                failures.append(record)
            return False, failures

        worker_results = []
        worker_errors = []
        with ThreadPoolExecutor(max_workers=len(report_specs)) as pool:
            futures = {
                pool.submit(run_worker, spec): spec for spec in report_specs
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    worker_results.append(future.result())
                except Exception as exc:
                    worker_errors.append({
                        "name": f"r{rnd}_diagnosis_b{spec['index']:02d}",
                        "failure_class": "worker-exception",
                        "failure_detail": str(exc)[:1000],
                    })

        prefix = f"r{rnd}_diagnosis_b"
        session_artifacts = []
        for name, record in self._cc_sessions_by_name.items():
            if not name.startswith(prefix):
                continue
            for key in ("trace", "stderr", "prompt", "settings"):
                value = record.get(key)
                if value:
                    session_artifacts.append(Path(value))
        session_artifacts.extend(self.run_dir.glob(f"{prefix}*"))
        allowed_changes = [
            *[spec["report"] for spec in report_specs],
            *session_artifacts,
            self.provenance_path, self.log_path, self.run_dir / "proxy.log",
        ]
        strays = self.inventory_diff(before, allowed_changes)
        if strays:
            fatal = [s for s in strays
                     if self._violation_is_contamination(str(s))]
            if fatal:
                raise RuntimeError(
                    "parallel diagnosis wrote outside its scope "
                    f"(contamination-critical): {fatal[:10]}"
                )
            # Non-critical strays (for example an operator log landing in the
            # repository) fail the phase through the normal session-failure
            # path instead of killing the driver.
            raise MetaAgentSessionFailure(
                f"r{rnd}_diagnosis_map", [{
                    "failure_class": "stray-file-change",
                    "failure_detail": str(strays[:10]),
                }],
            )
        self.integrity()

        failures = [
            record
            for ok, records in worker_results if not ok
            for record in records
        ]
        failures.extend(worker_errors)
        if failures:
            raise MetaAgentSessionFailure("diagnosis-map", failures)
        self.log("parallel diagnosis complete")
        return [spec["report"] for spec in report_specs]

    def phase_continuous(
            self, rnd: int, rdir: Path, failing: set,
            forced_owners: dict | None = None,
            forced_opportunities: dict | None = None) -> dict | None:
        """Diagnose and edit one candidate, optionally from parallel reports."""
        hierarchical = self.meta_workflow == "hierarchical"
        workflow_label = "hierarchical" if hierarchical else "continuous"
        rel = rdir.relative_to(ROOT)
        cand_rel = self.cand_dir.relative_to(ROOT)
        plan_path = rdir / "plan.json"
        plan_path.unlink(missing_ok=True)

        disclosure = self._materialize_capability_disclosure(rnd, rdir)
        history_index = None
        history_paths: list[Path] = []
        if not hierarchical:
            history_index, history_paths = self._materialize_history_index(
                rnd, rdir, {str(task) for task in failing},
            )
        diagnosis_protocol_path = rdir / "phase_continuous_diagnosis_protocol.md"
        patch_protocol_path = rdir / "phase_continuous_patch_protocol.md"
        self._formal_protocol("diagnosis", diagnosis_protocol_path)
        self._formal_protocol("patch", patch_protocol_path)
        self._write_text_atomic(rdir / "plan_spec.md", ma_plan_schema.PLAN_SPEC_MD)
        self._write_text_atomic(rdir / "schema_rules.md", self.SCHEMA_RULES)

        policy_path = self.profile.policy_doc_path(self.domain)
        task_key = _task_sort_key
        evidence_root = rdir / "reparsed_evidence"
        if not (evidence_root / "evidence.md").is_file():
            evidence_root = rdir
        sorted_task_ids = [str(task) for task in sorted(failing, key=task_key)]
        task_paths = [
            evidence_root / "evidence" / f"task_{task_id}.md"
            for task_id in sorted_task_ids
        ]
        if hierarchical:
            diagnosis_inputs = self._parallel_diagnosis_reports(
                rnd, rdir, list(zip(sorted_task_ids, task_paths)), disclosure,
                policy_path, diagnosis_protocol_path,
            )
            history_inputs: list[Path] = []
        else:
            diagnosis_inputs = task_paths
            history_inputs = [history_index, *history_paths]
        required_reads: list[Path] = []
        seen_reads: set[Path] = set()
        for path in [
            rdir / "state.md", evidence_root / "evidence.md", *diagnosis_inputs,
            *history_inputs, policy_path,
            disclosure["manual_path"], disclosure["tool_capabilities_path"],
            diagnosis_protocol_path, patch_protocol_path,
            rdir / "plan_spec.md", rdir / "schema_rules.md",
            *self._package_prompt_files(self.cand_dir),
        ]:
            resolved = Path(path).resolve()
            if resolved in seen_reads:
                continue
            if not resolved.is_file():
                raise RuntimeError(
                    f"continuous input file is missing: {resolved}"
                )
            seen_reads.add(resolved)
            required_reads.append(resolved)
        read_list = "\n".join(
            f"- `{os.path.relpath(path, ROOT).replace(chr(92), '/')}`"
            for path in required_reads
        )
        context_instruction = (
            "Parallel diagnosis workers have already analyzed disjoint trajectory "
            "batches. Reconcile their reports in one reducer context and edit the "
            "disposable Skill Memory. Reports are evidence summaries, not votes. "
            "They are designed to be sufficient for reduction, so normally use "
            "the reports and do not re-read raw trajectories. Inspect a raw task "
            "only when a report explicitly lacks necessary evidence or reports "
            "conflict on the causal diagnosis."
            if hierarchical else
            "Use one continuous reasoning context to diagnose this round and edit "
            "its disposable Skill Memory."
        )
        first_step = (
            "Reconcile every batch report against the current complete Skill "
            "Memory. Merge duplicate causes and preserve disagreements when the "
            "evidence differs."
            if hierarchical else
            "Diagnose every failing trajectory against the current complete Skill "
            "Memory."
        )
        tool_guidance = (
            f"""Work autonomously with the available file tools inside the disclosed
scope. Use Read for known files and Glob/Grep as needed to inspect the candidate.
For every Glob or Grep call, set `path` explicitly to `{cand_rel}` or one of its
subdirectories. Do not access source code, tests, held-out data, historical
champions, or other run artifacts. Once the diagnosis is stable, write the plan
and candidate, self-check them, and end the session."""
            if hierarchical else
            """Do not seek source code, tests, held-out data, historical champions,
or other run artifacts. Do not run commands.

Tool schedule is strict because every extra tool turn reprocesses the full
trajectory context:
- First, issue one batched group of Read calls for every file in Required reads.
  Do not narrate or reason between those reads, and do not read any file twice.
- In the next assistant response, finish the diagnosis and issue all Edit calls
  for plan.json and the candidate together. Do not insert narration, another
  Read, or one-file-at-a-time reasoning between those edits.
- After the Edit results, end the session unless one edit explicitly failed."""
        )
        edit_guidance = (
            "Read an existing file before editing it when the tool requires that. "
            "Use Glob rather than guessing filenames inside the candidate."
            if hierarchical else
            "The initial batched Read supplies Claude Code's prerequisite for "
            "editing an existing file; do not re-read an unchanged file."
        )
        prompt = f"""You are the Meta-Agent for Recuris recursive evolution, round {rnd}.

{context_instruction} The driver exposes only the scoped files listed below.
{tool_guidance}

Reachability sanity check: when evidence says
`post_terminal_assistant_event: absent`, the episode has ended. Do not invent a
later assistant draft, checker event, or tool call. Any proposed repair must
name a concrete earlier assistant turn in the failed trace. An instruction to
act on the terminal event is not a repair; the candidate must change behavior at
an earlier reachable event or abstain.

Work in this order:
1. {first_step}
   A failure may require more than one E/W/RHO/C component; represent that as
   separate evidence-backed clusters. Do not force a Skill Memory change when
   the available trace does not support one.
   Every failing task must appear in at least one cluster. If no repair is
   supported, record an explicit no-fix cluster instead of omitting the task.
   Adapter O=unknown is unresolved, not a verdict: inspect earlier assistant
   turns and resolve O from concrete intervention evidence when possible.
   Each cluster owns exactly one component and may contain only that component's
   legal actions. A new E card plus RHO delivery therefore requires two linked
   clusters citing the same evidence, not both action types in one cluster.
   For every patchable cluster, make one complete causal chain explicit in the
   existing plan fields: (a) exact pre-failure runtime event in
   `opportunity_evidence`; (b) trigger condition and delivered/checked content in
   the fix; and (c) the worker's expected immediate next behavior in the fix
   sketch or replacement value. Reject your own proposal if one link is
   hypothetical or occurs after termination.
   Compare that chain with the rejected mechanisms in `state.md` and the diagnosis
   reports. A different card name, wording, scope, or component label is not a
   material revision when it preserves the same event -> trigger -> intervention
   -> behavior chain; treat it as a semantic repeat.
2. Write the complete diagnosis and proposed changes to `{rel}/plan.json` using
   the embedded plan specification. This remains the auditable record of what
   you decided.
3. Without starting a new session, implement exactly that plan in the disposable
    candidate `{cand_rel}`. Make no change that is absent from plan.json. For a
    remove_card action, record it in the plan and leave deletion to the driver.
    {edit_guidance}
4. Check the completed edits against the schema and patch protocol you read,
   then end the session. Do not revise plan.json after package editing begins.

The candidate and plan are the only writable locations. The driver will reject
an invalid plan, leaked identifiers, unplanned edits, malformed packages, or
unreachable cards before any downstream evaluation.

## Required reads
{read_list}
"""
        prompt += (
            f"\n\nNow write `{rel}/plan.json`, implement it in `{cand_rel}`, "
            "self-check the result, and end the session."
        )

        candidate_readable = [
            # The whole round directory is the session's green work area:
            # evidence, diagnosis reports, protocols, and its own outputs all
            # live here, and browsing it discloses nothing beyond the
            # per-file grants it already has.
            plan_path.parent,
            plan_path, self.cand_dir,
            *(task_paths if hierarchical else []),
            *[path for path in required_reads
              if not path.is_relative_to(self.cand_dir.resolve())],
        ]
        session_failures: list[dict] = []
        session_ok = False
        editor_tools = HIERARCHICAL_REDUCER_TOOLS if hierarchical else P_TOOLS
        for attempt in range(1, 3):
            attempt_prompt = prompt
            if attempt == 2:
                plan_path.unlink(missing_ok=True)
                self._prepare_candidate(rnd, self._working_base_package())
                attempt_prompt += (
                    "\n\nThe previous session did not complete. This is a fresh "
                    "retry from the unchanged base package. " + (
                        "Use the scoped discovery tools as needed, then finish "
                        "the plan and candidate autonomously."
                        if hierarchical else
                        "Read each existing candidate file at most once, then "
                        "finish the plan and all planned edits without waiting "
                        "for another turn."
                    )
                )
            session_stem = (
                "hierarchical_reduce" if hierarchical else "continuous"
            )
            session_kwargs = (
                {"detect_no_progress": False}
                if hierarchical else {}
            )
            session_ok = self.cc_session(
                (f"r{rnd}_{session_stem}" if attempt == 1 else
                 f"r{rnd}_{session_stem}_retry"),
                attempt_prompt, editor_tools, self.a.cc_timeout,
                [plan_path, self.cand_dir], candidate_readable,
                capability_disclosure=disclosure,
                **session_kwargs,
            )
            if session_ok:
                break
            session = getattr(self, "last_cc_session", {})
            session_failures.append({
                "name": session.get("name"),
                "session_key": session.get("session_key"),
                "failure_class": session.get("failure_class") or "unknown",
                "failure_detail": session.get("failure_detail"),
                "launcher_returncode": session.get("launcher_returncode"),
                "timed_out": session.get("timed_out"),
                "trace_sha256": session.get("trace_sha256"),
                "upstream_model_verified":
                    session.get("upstream_model_verified"),
            })
            if attempt == 1:
                self.log(
                    f"{session_stem} session failed -> retrying once from base"
                )
        if not session_ok:
            raise MetaAgentSessionFailure(session_stem, session_failures)

        blocked = {tuple(key) for key in self.state["blocked"]}
        activation_context = self.pkg_activation_context(
            disclosure["tool_capabilities"],
        )
        def validate_current_plan() -> tuple[dict | None, list[str], list[str]]:
            current_plan, current_errors, current_warnings = (
                ma_plan_schema.load_and_validate(
                    plan_path, failing, blocked, self.pkg_deliverers(),
                    expected_round=rnd, forced_owners=forced_owners or {},
                    forced_opportunities=forced_opportunities or {},
                    current_entry_kind=activation_context["entry_kind"],
                    tool_aware_entry_kinds=
                        activation_context["tool_aware_entry_kinds"],
                    known_tools=activation_context["known_tools"],
                    mutating_tools=activation_context["mutating_tools"],
                    pkg_checkers=activation_context["checkers"],
                    checker_capabilities=activation_context.get(
                        "checker_capabilities", self._benchmark_profile().checker_capabilities()),
                    current_grounding_matcher=
                        activation_context.get("grounding_matcher"),
                    current_wm_schema=activation_context.get("wm_schema"),
                )
            )
            if current_plan:
                covered = {
                    str(task)
                    for cluster in current_plan.get("clusters", [])
                    for task in cluster.get("evidence_tasks", [])
                }
                missing = sorted(
                    {str(task) for task in failing} - covered,
                    key=lambda value: (0, int(value)) if value.isdigit()
                    else (1, value),
                )
                if missing:
                    current_errors.append(
                        f"{workflow_label} plan omits failing task(s): " +
                        ", ".join(missing)
                    )
            return current_plan, current_errors, current_warnings

        plan, errors, warnings = validate_current_plan()
        valid_sessions = 1
        if errors:
            correction_files = [
                *([plan_path] if plan_path.is_file() else []),
                *self._package_prompt_files(self.cand_dir),
                rdir / "plan_spec.md", rdir / "schema_rules.md",
            ]
            correction_prompt = f"""You are correcting the completed {workflow_label} Meta-Agent round {rnd}.

The diagnosis and candidate already exist. Deterministic validation found only
the contract errors below. Correct those errors in `{rel}/plan.json` and, only
if necessary to keep the final plan and candidate aligned, in `{cand_rel}`.
Preserve the evidence-backed diagnosis and all unrelated candidate behavior.
Do not add a new hypothesis, inspect trajectories, run commands, or access any
path outside the plan and candidate.

Validator errors:
{json.dumps(errors, ensure_ascii=False, indent=2)}

All required inputs are embedded below. Read an existing writable file once
only when Claude Code requires that before Edit. Then issue all required Edit
calls together and end the session. This is the only correction attempt.

## Embedded correction inputs
{self._embedded_file_context(correction_files)}
"""
            correction_ok = self.cc_session(
                f"r{rnd}_{workflow_label}_fix", correction_prompt, P_TOOLS,
                min(self.a.cc_timeout, 1200),
                [plan_path, self.cand_dir], correction_files,
                capability_disclosure=disclosure,
            )
            if not correction_ok:
                session = getattr(self, "last_cc_session", {})
                raise MetaAgentSessionFailure(f"{workflow_label}-fix", [{
                    "name": session.get("name"),
                    "session_key": session.get("session_key"),
                    "failure_class": session.get("failure_class") or "unknown",
                    "failure_detail": session.get("failure_detail"),
                    "launcher_returncode": session.get("launcher_returncode"),
                    "timed_out": session.get("timed_out"),
                    "trace_sha256": session.get("trace_sha256"),
                    "upstream_model_verified":
                        session.get("upstream_model_verified"),
                }])
            valid_sessions += 1
            activation_context = self.pkg_activation_context(
                disclosure["tool_capabilities"],
            )
            plan, errors, warnings = validate_current_plan()

        for warning in warnings:
            self.log(f"{workflow_label} plan warning: {warning}")
        if plan and not errors:
            self.log(
                f"{workflow_label} plan valid: "
                f"{len(plan['clusters'])} cluster(s)"
            )
            return plan

        self.last_diagnosis_outcome = {
            "kind": "invalid-plan",
            "valid_sessions": valid_sessions,
            "validator_errors": list(errors),
            "session_failures": [],
        }
        self.log(f"{workflow_label} plan invalid ({len(errors)} error(s))")
        return None

    def phase_d(self, rnd: int, rdir: Path, failing: set,
                forced_owners: dict | None = None,
                forced_opportunities: dict | None = None,
                generation_context: dict | None = None) -> dict | None:
        rel = rdir.relative_to(ROOT)
        best_rel = (Path(self._working_base_package()).relative_to(ROOT)
                    if self.state["best"] else None)
        profile_paths: dict | None = None
        denied_read_paths: list[Path] | None = None
        denied_directory_reads: list[Path] | None = None
        prompt_template_sha256: str | None = None
        history_index: Path | None = None
        history_paths: list[Path] = []
        d_tools = D_TOOLS
        if generation_context is not None:
            if getattr(self, "generation_profile_spec", None) is None:
                raise RuntimeError(
                    "generation context supplied without a locked profile"
                )
            profile_paths = generation_context["paths"]
            disclosure = generation_context["capability_disclosure"]

            def visible_path(path: Path) -> str:
                return path.relative_to(ROOT).as_posix()

            best = Path(self.state["best"])
            base_manifest = best / "manifest.yaml"
            plan_path = rdir / "plan.json"
            prompt = PROFILE_DIAGNOSIS_PROMPT_TEMPLATE.format(
                round=rnd,
                capability_block=disclosure["prompt_block"],
                evidence_path=visible_path(profile_paths["evidence"]),
                policy_path=visible_path(profile_paths["policy"]),
                capability_path=visible_path(profile_paths["capability"]),
                tool_capabilities_path=visible_path(
                    profile_paths["tool_capabilities"]
                ),
                plan_protocol_path=visible_path(
                    profile_paths["plan_protocol"]
                ),
                base_manifest_path=visible_path(base_manifest),
                plan_path=visible_path(plan_path),
                required_cases=", ".join(sorted(
                    generation_context["required_alias_coverage"]
                )),
            )
            d_readable = [
                profile_paths["evidence"], profile_paths["policy"],
                profile_paths["capability"],
                profile_paths["tool_capabilities"],
                profile_paths["plan_protocol"], base_manifest, plan_path,
            ]
            full_policy_path = (
                ROOT.parent / "tau2-bench" / "data" / "tau2" /
                "domains" / self.domain / "policy.md"
            )
            denied_read_paths = [
                rdir / "evidence", rdir / "state.md", rdir / "evidence.md",
                profile_paths["control_root"], full_policy_path,
                profile_paths["patch_protocol"],
                *[
                    path for path in sorted(best.rglob("*"))
                    if path.is_file() and path != base_manifest
                ],
            ]
            denied_directory_reads = [
                profile_paths["visible_root"], best,
                *[path for path in sorted(best.rglob("*")) if path.is_dir()],
            ]
            prompt_template_sha256 = generation_context["manifest"][
                "substantive"]["prompt_templates"]["diagnosis_sha256"]
            failing = set(generation_context["failing_tasks"])
            forced_owners = copy.deepcopy(
                generation_context["forced_owners"]
            )
            forced_opportunities = copy.deepcopy(
                generation_context["forced_opportunities"]
            )
            attempt_timeouts = list(
                generation_context["diagnosis_timeouts"]
            )
        else:
            d_tools = D_WRITE_TOOLS
            disclosure = self._materialize_capability_disclosure(rnd, rdir)
            history_index, history_paths = self._materialize_history_index(
                rnd, rdir, {str(task) for task in failing},
            )
            protocol = self._formal_protocol(
                "diagnosis", rdir / "phase_d_protocol.md"
            )
            prompt = f"""You are the Meta-Agent for the Recuris framework (diagnosis phase, round {rnd}).

{disclosure["prompt_block"]}

The block above is the complete public runtime interface contract. Use it
directly; do not seek framework source, tests, historical packages, or prior
experiment logs to infer constructor schemas.

A frozen downstream agent runs with the Skill Memory package at `{best_rel}`.
Your ONLY output this session: write `{rel}/plan.json` — the full checklist of
this round's problems and fixes. All required inputs are embedded below. You
have scoped Edit only: create NOTHING except plan.json and do not modify any
package.

Use the embedded inputs in this order:
1. `{rel}/state.md`      — cross-round memory: lessons, changelog, suspects. OBEY the lessons.
2. `{rel}/evidence.md`   — this round's failing tasks (index + fingerprint).
3. `{rel}/evidence/task_<id>.md` - all k trial records per failing task,
   including every failed trace and any successful counterfactual (read ALL of them).
4. `{rel}/history_index.md` and every evidence file it lists - bounded,
   train-only history for these same tasks, including paired repair evidence
   from rejected candidates when available. Use it to distinguish repeated
   worker behavior from simulator outcome variation. It has no dev/test traces.
5. The domain policy: `{self.profile.policy_doc_display(self.domain)}`.
5. The current package under `{best_rel}` (manifest + cards) — know what already exists before proposing.
6. `{rel}/plan_spec.md`  — the REQUIRED plan.json format. A validator enforces it.

Diagnosis discipline:
- Cluster failures by their failing step/action, not by surface story.
- Attribute each cluster to ONE component: E (a reusable skill/knowledge item is
  missing or wrong), W (the tracked task state or grounding is wrong), RHO (the
  needed memory exists but was not delivered at the useful moment), or C (an
  adapter-disclosed checker could observe and reject the invalid draft/action).
- Choose a carrier only after checking which runtime event and state it can
  actually observe. Never route a non-mutating tool through the write ledger.
- Compare every failed trial with any successful counterfactual. Treat a terminal
  reason, marker, or final actor as an observation, not a causal owner. Locate
  the last observable intervention point; do not infer a component or repair
  from the surface form of the final message.
- Record O as action_opportunity and P as patchability for every cluster. A
  Skill Memory fix is admissible only when O=present and P=memory. If either is
  absent/unknown, abstain with fixes=[]; do not guess in order to force evolution.
- MODEL/HARNESS/BENCHMARK remain valid abstention outcomes only when the supplied
  facts independently establish them. The driver does not assign a causal owner
  from a terminal marker or failed score.
- Fixes must come from the evidence: cite the relevant trace, policy, evaluator,
  or verifier record.
- Prefer surgical, low-regression fixes; the plan is admitted or rejected AS A
  WHOLE by a validation gate on held-out tasks.

## Mandatory diagnosis protocol
These rules are embedded in the phase prompt and are not optional tools:

{protocol}

## Embedded diagnosis inputs
Do not call Read or search for more context. The following frozen text is the
complete evidence available to this diagnosis session.

When done, write `{rel}/plan.json` and end the session."""
            (rdir / "plan_spec.md").write_text(
                ma_plan_schema.PLAN_SPEC_MD, encoding="utf-8"
            )
            policy_path = self.profile.policy_doc_path(self.domain)
            evidence_root = rdir / "reparsed_evidence"
            if not (evidence_root / "evidence.md").is_file():
                evidence_root = rdir
            task_paths = [
                evidence_root / "evidence" / f"task_{task_id}.md"
                for task_id in sorted(failing, key=_task_sort_key)
            ]
            context_files = [
                rdir / "state.md", evidence_root / "evidence.md", *task_paths,
                history_index, *history_paths, policy_path,
                *self._package_prompt_files(Path(self._working_base_package())),
                rdir / "plan_spec.md",
            ]
            prompt += self._embedded_file_context(context_files)
            prompt += (
                f"\n\nUsing only the embedded inputs, write `{rel}/plan.json` "
                "with exactly one scoped Edit call, then end the session "
                "immediately. Do not make follow-up edits; validator feedback "
                "is handled by a separate bounded retry session."
            )
            d_readable = []
            attempt_timeouts = [self.a.cc_timeout, self.a.cc_timeout]
        blocked = {tuple(k) for k in self.state["blocked"]}
        activation_context = self.pkg_activation_context(
            disclosure["tool_capabilities"],
        )

        session_failures: list[dict] = []
        validator_errors: list[str] = []
        valid_sessions = 0
        for attempt, attempt_timeout in enumerate(attempt_timeouts, start=1):
            plan_path = rdir / "plan.json"
            if generation_context is None and plan_path.exists():
                plan_path.unlink()
            if generation_context is not None and plan_path.exists():
                stale_path = (
                    profile_paths["attempts"] /
                    f"d{attempt}_stale_plan.json"
                )
                self._write_frozen_profile_bytes(
                    stale_path, plan_path.read_bytes()
                )
                plan_path.unlink()
            if generation_context is None:
                session_ok = self.cc_session(
                    f"r{rnd}_d{attempt}", prompt, d_tools,
                    attempt_timeout, [plan_path],
                    d_readable,
                    capability_disclosure=disclosure,
                )
            else:
                self._assert_generation_profile_model_surface_opaque(
                    prompt, d_readable
                )
                session_ok = self.cc_session(
                    f"r{rnd}_d{attempt}", prompt, D_TOOLS,
                    attempt_timeout, [plan_path], d_readable,
                    capability_disclosure=disclosure,
                    denied_read_paths=denied_read_paths,
                    denied_directory_reads=denied_directory_reads,
                    prompt_template_sha256=prompt_template_sha256,
                )
                self._audit_generation_profile_d_plan_raw_ids(
                    generation_context, plan_path=plan_path,
                    rnd=rnd, attempt=attempt,
                )
            if not session_ok:
                session = getattr(self, "last_cc_session", {})
                session_failures.append({
                    "name": session.get("name"),
                    "session_key": session.get("session_key"),
                    "failure_class": session.get("failure_class") or "unknown",
                    "failure_detail": session.get("failure_detail"),
                    "launcher_returncode": session.get("launcher_returncode"),
                    "timed_out": session.get("timed_out"),
                    "trace_sha256": session.get("trace_sha256"),
                    "upstream_model_verified":
                        session.get("upstream_model_verified"),
                })
                continue
            valid_sessions += 1
            if generation_context is None:
                plan, errors, warnings = ma_plan_schema.load_and_validate(
                    plan_path, failing, blocked, self.pkg_deliverers(),
                    expected_round=rnd, forced_owners=forced_owners or {},
                    forced_opportunities=forced_opportunities or {},
                    current_entry_kind=activation_context["entry_kind"],
                    tool_aware_entry_kinds=
                        activation_context["tool_aware_entry_kinds"],
                    known_tools=activation_context["known_tools"],
                    mutating_tools=activation_context["mutating_tools"],
                    pkg_checkers=activation_context["checkers"],
                    checker_capabilities=activation_context.get(
                        "checker_capabilities", self._benchmark_profile().checker_capabilities()),
                    current_grounding_matcher=
                        activation_context.get("grounding_matcher"),
                    current_wm_schema=activation_context.get("wm_schema"))
                errors = list(errors)
            else:
                try:
                    alias_plan = json.loads(
                        plan_path.read_text(encoding="utf-8")
                    )
                except Exception as exc:
                    alias_plan = None
                    plan = None
                    errors = [
                        f"plan.json is not valid JSON: {exc}"
                    ]
                    warnings = []
                else:
                    plan, contract_errors = (
                        self._validate_and_map_generation_profile_plan(
                            alias_plan, generation_context
                        )
                    )
                    warnings = []
                    if contract_errors:
                        errors = list(contract_errors)
                    else:
                        errors, warnings = ma_plan_schema.validate_plan(
                            plan, failing, blocked, self.pkg_deliverers(),
                            expected_round=rnd,
                            forced_owners=forced_owners or {},
                            forced_opportunities=
                                forced_opportunities or {},
                            current_entry_kind=
                                activation_context["entry_kind"],
                            tool_aware_entry_kinds=activation_context[
                                "tool_aware_entry_kinds"],
                            known_tools=activation_context["known_tools"],
                            mutating_tools=
                                activation_context["mutating_tools"],
                            pkg_checkers=activation_context["checkers"],
                            checker_capabilities=activation_context.get(
                                "checker_capabilities", self._benchmark_profile().checker_capabilities()),
                            current_grounding_matcher=
                                activation_context.get("grounding_matcher"),
                            current_wm_schema=
                                activation_context.get("wm_schema"),
                        )
                        errors = list(errors)
                    reverse_alias = {
                        task: alias for alias, task in
                        generation_context["alias_to_task"].items()
                    }
                    errors = [
                        functools.reduce(
                            lambda text, pair:
                                text.replace(pair[0], pair[1]),
                            reverse_alias.items(), str(error),
                        )
                        for error in errors
                    ]
                    warnings = [
                        functools.reduce(
                            lambda text, pair:
                                text.replace(pair[0], pair[1]),
                            reverse_alias.items(), str(warning),
                        )
                        for warning in warnings
                    ]
            for w in warnings:
                self.log(f"plan warning: {w}")
            if plan and not errors:
                if generation_context is not None:
                    self._freeze_generation_profile_plan_outputs(
                        generation_context, plan_path, alias_plan, plan
                    )
                self.log(f"plan valid: {len(plan['clusters'])} cluster(s)")
                return plan
            validator_errors = list(errors)
            self.log(f"plan invalid ({len(errors)} error(s)); "
                     + ("retrying with feedback" if attempt == 1 else "giving up"))
            err_md = "\n".join(f"- {e}" for e in errors)
            rejected_plan = (
                plan_path.read_text(encoding="utf-8")
                if plan_path.is_file() else "(plan file missing)"
            )
            prompt += (f"\n\n## VALIDATOR REJECTED your previous plan.json — fix these and rewrite it:\n"
                       f"{err_md}\n(Keep everything that was legal; change only what is flagged.)")
            prompt += f"\n\n## Previous rejected plan\n{rejected_plan}\n"
        if valid_sessions == 0:
            raise MetaAgentSessionFailure("D", session_failures)
        self.last_diagnosis_outcome = {
            "kind": "invalid-plan",
            "valid_sessions": valid_sessions,
            "validator_errors": validator_errors,
            "session_failures": session_failures,
        }
        return None

    def phase_d_review(
            self, rnd: int, rdir: Path, initial_plan: dict, failing: set,
            forced_owners: dict | None = None,
            forced_opportunities: dict | None = None) -> dict | None:
        """Fresh-context review of a legal diagnosis before package editing."""
        rel = rdir.relative_to(ROOT)
        best = Path(self._working_base_package())
        best_rel = best.relative_to(ROOT)
        history_index, history_paths = self._materialize_history_index(
            rnd, rdir, {str(task) for task in failing},
        )
        initial_path = rdir / "initial_plan.json"
        review_path = rdir / "reviewed_plan.json"
        cases_path = rdir / "activation_cases.json"
        self._write_text_atomic(
            initial_path,
            json.dumps(initial_plan, ensure_ascii=False, indent=2) + "\n",
        )
        review_path.unlink(missing_ok=True)
        cases_path.unlink(missing_ok=True)
        policy_path = self.profile.policy_doc_path(self.domain)
        disclosure = self._materialize_capability_disclosure(rnd, rdir)
        evidence_root = rdir / "reparsed_evidence"
        if not (evidence_root / "evidence.md").is_file():
            evidence_root = rdir
        current_evidence = [
            evidence_root / "evidence" / f"task_{task_id}.md"
            for task_id in sorted(
                {str(task) for task in failing},
                key=lambda value: (0, int(value)) if value.isdigit() else (1, value),
            )
        ]
        context_files = [
            initial_path, rdir / "state.md", evidence_root / "evidence.md",
            *current_evidence, history_index, *history_paths,
            *self._package_prompt_files(best), policy_path,
            rdir / "plan_spec.md",
        ]
        prompt = f"""You are the independent diagnosis reviewer for recursive round {rnd}.
Do not edit the Skill Memory package and do not run evaluations. The first
diagnosis is legal but may still have missed repeated evidence or counterevidence.

{disclosure["prompt_block"]}

All required inputs are embedded below. Review these sections before deciding:
1. `{rel}/initial_plan.json`
2. `{rel}/state.md`
3. `{rel}/evidence.md` and every current `evidence/task_<id>.md`
4. `{rel}/history_index.md` and every prior train-evidence file it lists
5. `{best_rel}` (current manifest and cards)
6. `{self.profile.policy_doc_display(self.domain)}`
7. `{rel}/plan_spec.md` and the disclosed E/W/RHO/C capability manual

Reconstruct the complete plan after checking whether worker behavior repeats
across trials/rounds and whether outcome differences instead come from simulator
forgiveness or terminal control flow. Do not defend the first plan merely because
it is valid. Keep it unchanged when the evidence supports it; otherwise revise it.
The iteration unit is the complete Skill Memory and there is at most one candidate.

Write the final full plan to `{rel}/reviewed_plan.json` using plan_spec exactly.
If that reviewed plan adds or edits a knowledge/procedure card that will use
`need_driven_retrieval`, also write `{rel}/activation_cases.json` as:

```json
{{
  "schema_version": 1,
  "target_card_ids": ["the exact reviewed card id"],
  "cases": [
    {{"query_id": "positive_1", "query": "task-agnostic pending-intent description", "expected_activation": true}},
    {{"query_id": "negative_1", "query": "nearby but out-of-scope intent", "expected_activation": false}}
  ]
}}
```

Provide 2-4 distinct positive cases and 3-6 nearby negative cases. Derive them
only from masked train evidence and public policy; use placeholders, no real IDs.
Negative cases must test the main regression risk, not obviously unrelated prose.
If the reviewed plan has no such retrieval-card fix, do not create that file.
Create no other files. Do not call Read or search for more context.

## Embedded review inputs
{self._embedded_file_context(context_files)}

Write the requested output file(s) and end the session."""
        ok = self.cc_session(
            f"r{rnd}_d_review", prompt, D_WRITE_TOOLS, self.a.cc_timeout,
            [review_path, cases_path], [],
            capability_disclosure=disclosure,
        )
        if not ok:
            session = getattr(self, "last_cc_session", {})
            raise MetaAgentSessionFailure("D-review", [{
                "name": session.get("name"),
                "session_key": session.get("session_key"),
                "failure_class": session.get("failure_class") or "unknown",
                "failure_detail": session.get("failure_detail"),
                "launcher_returncode": session.get("launcher_returncode"),
                "timed_out": session.get("timed_out"),
                "trace_sha256": session.get("trace_sha256"),
                "upstream_model_verified": session.get("upstream_model_verified"),
            }])

        blocked = {tuple(key) for key in self.state["blocked"]}
        activation_context = self.pkg_activation_context(
            disclosure["tool_capabilities"],
        )
        reviewed, errors, warnings = ma_plan_schema.load_and_validate(
            review_path, failing, blocked, self.pkg_deliverers(),
            expected_round=rnd, forced_owners=forced_owners or {},
            forced_opportunities=forced_opportunities or {},
            current_entry_kind=activation_context["entry_kind"],
            tool_aware_entry_kinds=activation_context["tool_aware_entry_kinds"],
            known_tools=activation_context["known_tools"],
            mutating_tools=activation_context["mutating_tools"],
            pkg_checkers=activation_context["checkers"],
            checker_capabilities=activation_context.get(
                "checker_capabilities", self._benchmark_profile().checker_capabilities()),
            current_grounding_matcher=activation_context.get("grounding_matcher"),
            current_wm_schema=activation_context.get("wm_schema"),
        )
        review_errors = list(errors)
        for warning in warnings:
            self.log(f"reviewed-plan warning: {warning}")
        targets = self._retrieval_probe_targets(reviewed or {})
        if reviewed and not review_errors and targets:
            if not cases_path.is_file():
                review_errors.append(
                    "reviewed retrieval-card plan omitted activation_cases.json"
                )
            else:
                try:
                    case_spec = json.loads(cases_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    review_errors.append(f"activation_cases.json is invalid JSON: {exc}")
                else:
                    review_errors.extend(
                        activation_probe.validate_retrieval_cases(case_spec, targets)
                    )
                    leaks = ma_sanitize.scan_text(
                        cases_path.read_text(encoding="utf-8"), self.gold,
                    )
                    if leaks:
                        review_errors.append(
                            f"activation_cases.json leaked real benchmark ids: {leaks}"
                        )
        elif reviewed and not review_errors and cases_path.exists():
            review_errors.append(
                "activation_cases.json was created without a retrieval-card fix"
            )
        if not reviewed or review_errors:
            self.last_diagnosis_outcome = {
                "kind": "invalid-review",
                "valid_sessions": 1,
                "validator_errors": review_errors,
                "session_failures": [],
            }
            self.log(f"diagnosis review rejected: {len(review_errors)} error(s)")
            return None

        self._write_text_atomic(
            rdir / "plan.json",
            json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n",
        )
        self.log(
            f"diagnosis review valid: {len(reviewed['clusters'])} cluster(s), "
            f"targeted retrieval cards={sorted(targets)}"
        )
        return reviewed

    def pkg_deliverers(self) -> set:
        best = self.state.get("best")
        if not best:
            return set()
        man = Path(best) / "manifest.yaml"
        if not man.exists():
            return set()
        import yaml
        try:
            d = yaml.safe_load(man.read_text(encoding="utf-8")) or {}
            return {x.get("use") for x in (d.get("delivery") or []) if isinstance(x, dict)}
        except Exception:
            return set()

    def pkg_activation_context(
            self, frozen_tool_capabilities: dict | None = None) -> dict:
        """Closed-world carrier capabilities for plan-time reachability."""
        best = (Path(self._working_base_package())
                if self.state.get("best") else None)
        manifest = self._manifest_data(best) if best is not None else {}
        wm = manifest.get("wm") or {}
        entry_kind = (
            str(wm.get("entry_kind") or "generic_item")
            if isinstance(wm, dict) else "generic_item"
        )
        checkers = {
            str(item.get("use"))
            for item in (manifest.get("checkers") or [])
            if isinstance(item, dict) and item.get("use")
        }
        if frozen_tool_capabilities is None:
            capabilities = self._benchmark_profile().tool_capabilities(self.domain)
        else:
            rows = frozen_tool_capabilities.get("tools")
            if not isinstance(rows, list):
                raise RuntimeError("frozen tool capabilities has no tools list")
            capabilities = {
                str(row["name"]): {
                    "mutates_state": bool(row["mutates_state"]),
                    "tool_type": str(row["tool_type"]),
                }
                for row in rows
                if isinstance(row, dict) and set(row) == {
                    "name", "tool_type", "mutates_state",
                }
            }
            if len(capabilities) != len(rows):
                raise RuntimeError("frozen tool capabilities has an invalid row")
        grounding = manifest.get("grounding") or {}
        wm_schema = wm.get("schema") if isinstance(wm, dict) else None
        return {
            "entry_kind": entry_kind,
            "grounding_matcher": (
                str(grounding.get("matcher"))
                if isinstance(grounding, dict) and grounding.get("matcher") else None
            ),
            "wm_schema": dict(wm_schema) if isinstance(wm_schema, dict) else {},
            "checkers": checkers,
            "checker_capabilities": self._benchmark_profile().checker_capabilities(),
            "tool_aware_entry_kinds": tool_aware_entry_kinds(),
            "known_tools": set(capabilities),
            "mutating_tools": {
                name for name, record in capabilities.items()
                if record.get("mutates_state") is True
            },
        }

    @staticmethod
    def _manifest_data(package: Path) -> dict:
        import yaml
        path = Path(package) / "manifest.yaml"
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _card_index(package: Path) -> dict[str, tuple[Path, dict, str]]:
        import yaml
        index: dict[str, tuple[Path, dict, str]] = {}
        em_root = Path(package) / "em"
        if not em_root.is_dir():
            return index
        for path in sorted(em_root.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) < 3:
                continue
            meta = yaml.safe_load(parts[1]) or {}
            if not isinstance(meta, dict) or not str(meta.get("id") or ""):
                continue
            card_id = str(meta["id"])
            if card_id in index:
                raise RuntimeError(f"duplicate card id in candidate: {card_id}")
            index[card_id] = (path, meta, hashlib.sha256(path.read_bytes()).hexdigest())
        return index

    @staticmethod
    def _file_hashes(package: Path) -> dict[str, str]:
        return {
            path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(package).rglob("*"))
            if path.is_file() and "__pycache__" not in path.relative_to(package).parts
            and path.suffix.lower() not in {".pyc", ".pyo"}
        }

    def _apply_planned_deletions(self, plan: dict) -> None:
        cards = self._card_index(self.cand_dir)
        for cluster in plan["clusters"]:
            for fix in cluster.get("fixes", []):
                if fix.get("action") != "remove_card":
                    continue
                card_id = str(fix.get("card_id") or "")
                record = cards.get(card_id)
                if record is None:
                    raise RuntimeError(f"remove_card target does not exist: {card_id}")
                path = record[0]
                if not path.resolve().is_relative_to(self.cand_dir.resolve()):
                    raise RuntimeError(f"unsafe remove_card target: {path}")
                path.unlink()

    def _plan_conformance_errors(self, plan: dict, base: Path) -> list[str]:
        """Check that the candidate implements every fix and no unplanned edit."""
        errors: list[str] = []
        before_files = self._file_hashes(base)
        after_files = self._file_hashes(self.cand_dir)
        changed = {name for name in set(before_files) | set(after_files)
                   if before_files.get(name) != after_files.get(name)}
        base_cards = self._card_index(base)
        cand_cards = self._card_index(self.cand_dir)
        base_manifest = self._manifest_data(base)
        manifest = self._manifest_data(self.cand_dir)
        planned_card_ids: set[str] = set()
        planned_sections: set[str] = set()
        delivery_targets: set[str] = set()
        checker_targets: set[str] = set()
        planned_wm_fields: set[str] = set()

        for cluster in plan["clusters"]:
            for fix in cluster.get("fixes", []):
                action = str(fix.get("action") or "")
                if action in {"add_card", "edit_card", "remove_card"}:
                    card_id = str(fix.get("card_id") or "")
                    planned_card_ids.add(card_id)
                    before = base_cards.get(card_id)
                    after = cand_cards.get(card_id)
                    if action == "add_card":
                        if before is not None or after is None:
                            errors.append(f"add_card {card_id!r} was not added as a new card")
                        elif str(after[1].get("type")) != str(fix.get("em_type")):
                            errors.append(f"add_card {card_id!r} has the wrong type")
                    elif action == "edit_card":
                        if before is None or after is None or before[2] == after[2]:
                            errors.append(f"edit_card {card_id!r} was not changed")
                        elif str(after[1].get("type")) != str(fix.get("em_type")):
                            errors.append(f"edit_card {card_id!r} has the wrong type")
                    elif before is None or after is not None:
                        errors.append(f"remove_card {card_id!r} was not removed")
                    if action in {"add_card", "edit_card"} and after is not None:
                        em_type = str(fix.get("em_type"))
                        if em_type in {"action_result", "procedure"}:
                            trigger = after[1].get("trigger") or {}
                            expected_event = str(fix.get("trigger_event") or "")
                            if (not isinstance(trigger, dict) or
                                    trigger.get("event") != expected_event or
                                    trigger.get("tool") != fix.get("trigger_tool")):
                                errors.append(
                                    f"card {card_id!r} has the wrong {em_type} trigger"
                                )
                    continue

                if action in {"set_delivery", "edit_delivery", "remove_delivery"}:
                    planned_sections.add("delivery")
                    target = str(fix.get("deliverer") or "")
                    delivery_targets.add(target)
                    base_items = {item.get("use"): item
                                  for item in (base_manifest.get("delivery") or [])
                                  if isinstance(item, dict)}
                    items = {item.get("use"): item
                             for item in (manifest.get("delivery") or [])
                             if isinstance(item, dict)}
                    before, after = base_items.get(target), items.get(target)
                    expected = {"use": target}
                    if "cfg" in fix:
                        expected["cfg"] = fix["cfg"]
                    implemented = ((action == "set_delivery" and before is None and
                                    after == expected) or
                                   (action == "edit_delivery" and before is not None and
                                    after == expected and before != after) or
                                   (action == "remove_delivery" and before is not None and
                                    after is None))
                    if not implemented:
                        errors.append(f"{action} {target!r} was not implemented")
                elif action in {"set_checker", "remove_checker"}:
                    planned_sections.add("checkers")
                    target = str(fix.get("checker") or "")
                    checker_targets.add(target)
                    base_uses = {item.get("use") for item in (base_manifest.get("checkers") or [])
                                 if isinstance(item, dict)}
                    items = {item.get("use"): item
                             for item in (manifest.get("checkers") or [])
                             if isinstance(item, dict)}
                    expected = {"use": target}
                    if "cfg" in fix:
                        expected["cfg"] = fix["cfg"]
                    implemented = ((action == "set_checker" and target not in base_uses and
                                    items.get(target) == expected) or
                                   (action == "remove_checker" and target in base_uses and
                                    target not in items))
                    if not implemented:
                        errors.append(f"{action} {target!r} was not implemented")
                elif action == "set_wm":
                    planned_sections.add("wm")
                    field = str(fix.get("field") or "")
                    planned_wm_fields.add(field)
                    wm = manifest.get("wm") or {}
                    base_wm = base_manifest.get("wm") or {}
                    actual = ((wm.get("schema") or {}).get(field)
                              if field in {"binding_key", "max_entries", "machine_id_pattern"}
                              else wm.get(field))
                    before = ((base_wm.get("schema") or {}).get(field)
                              if field in {"binding_key", "max_entries", "machine_id_pattern"}
                              else base_wm.get(field))
                    if str(actual) != str(fix.get("value")) or actual == before:
                        errors.append(f"set_wm {field!r} expected {fix.get('value')!r}, got {actual!r}")
                else:
                    section = {"set_board": "board", "set_gate": "gate",
                               "set_grounding": "grounding",
                               "set_feasibility": "feasibility"}.get(action)
                    if section:
                        planned_sections.add(section)
                    if section and (manifest.get(section) != fix.get("value") or
                                    manifest.get(section) == base_manifest.get(section)):
                        errors.append(f"{action} did not exactly implement manifest.{section}")

        for section in sorted(set(base_manifest) | set(manifest)):
            if section not in planned_sections and base_manifest.get(section) != manifest.get(section):
                errors.append(f"unplanned manifest section change: {section}")

        for section, targets in (("delivery", delivery_targets),
                                 ("checkers", checker_targets)):
            if not targets:
                continue
            before_unplanned = [item for item in (base_manifest.get(section) or [])
                                if not isinstance(item, dict) or item.get("use") not in targets]
            after_unplanned = [item for item in (manifest.get(section) or [])
                               if not isinstance(item, dict) or item.get("use") not in targets]
            if before_unplanned != after_unplanned:
                errors.append(f"unplanned {section} entries changed")

        if planned_wm_fields:
            before_wm = json.loads(json.dumps(base_manifest.get("wm") or {}))
            after_wm = json.loads(json.dumps(manifest.get("wm") or {}))
            # Missing and empty manager_cfg are runtime-equivalent: both
            # construct the manager with its default configuration.
            for wm in (before_wm, after_wm):
                if wm.get("manager_cfg") == {}:
                    wm.pop("manager_cfg")
            # A wm.schema field the kernel REQUIRES for a planned mechanism is
            # a completion of that plan, not an unplanned edit (shared rule
            # table with the plan validator).
            if "grounding" in planned_sections:
                matcher = str((manifest.get("grounding") or {}).get("matcher") or "")
                coupling = ma_plan_schema.KERNEL_COUPLINGS.get(matcher)
                if coupling is not None:
                    required_field = coupling[0]
                    for wm in (before_wm, after_wm):
                        schema = wm.get("schema")
                        if isinstance(schema, dict):
                            schema.pop(required_field, None)
                            if not schema:
                                wm.pop("schema", None)
            for field in planned_wm_fields:
                if field in {"binding_key", "max_entries", "machine_id_pattern"}:
                    for wm in (before_wm, after_wm):
                        schema = wm.get("schema")
                        if isinstance(schema, dict):
                            schema.pop(field, None)
                            if not schema:
                                wm.pop("schema", None)
                else:
                    before_wm.pop(field, None)
                    after_wm.pop(field, None)
            if before_wm != after_wm:
                errors.append("unplanned wm fields changed")

        for relative in sorted(changed):
            if relative == "manifest.yaml":
                if not planned_sections:
                    errors.append("manifest.yaml changed without a manifest fix in the plan")
                continue
            card_id = None
            for index in (cand_cards, base_cards):
                for candidate_id, record in index.items():
                    if record[0].relative_to(self.cand_dir if index is cand_cards else base).as_posix() == relative:
                        card_id = candidate_id
                        break
                if card_id:
                    break
            if card_id not in planned_card_ids:
                errors.append(f"unplanned candidate file change: {relative}")
        return errors

    def _audit_generation_profile_candidate_leaks(
            self, generation_context: dict, *, rnd: int, attempt: int) -> None:
        """Fail a profile arm before any contaminated candidate can be retried."""
        raw_id_detail: str | None = None
        try:
            self._assert_generation_profile_model_surface_opaque(
                "", [self.cand_dir]
            )
        except RuntimeError as exc:
            raw_id_detail = str(exc)
        gold_leaks = ma_sanitize.scan_package(self.cand_dir, self.gold)
        if raw_id_detail is None and not gold_leaks:
            return

        common = {
            "schema_version": 1,
            "round": rnd,
            "attempt": attempt,
            "phase": "P",
            "fatal": True,
            "retry_allowed": False,
            "candidate_tree_sha256": self._package_digest(self.cand_dir),
        }
        attempts = generation_context["paths"]["attempts"]
        if raw_id_detail is not None:
            self._write_json_atomic(
                attempts / f"p{attempt}_raw_id_violation.json",
                {**common, "violation": "raw-task-id",
                 "detail": raw_id_detail},
            )
        if gold_leaks:
            self._write_json_atomic(
                attempts / f"p{attempt}_gold_id_violation.json",
                {**common, "violation": "gold-id",
                 "leaks": copy.deepcopy(gold_leaks)},
            )
        raise ProfileArmIneligible("P")

    def phase_p(self, rnd: int, rdir: Path, plan: dict,
                generation_context: dict | None = None) -> bool:
        rel = rdir.relative_to(ROOT)
        cand_rel = self.cand_dir.relative_to(ROOT)
        activation_cases_path = rdir / "activation_cases.json"
        denied_read_paths: list[Path] | None = None
        denied_directory_reads: list[Path] | None = None
        prompt_template_sha256: str | None = None
        if generation_context is not None:
            if self.generation_profile_spec is None:
                raise RuntimeError(
                    "generation context supplied without a locked profile"
                )
            profile_paths = generation_context["paths"]
            disclosure = generation_context["capability_disclosure"]
            self._verify_generation_profile_plan_outputs(
                generation_context
            )

            def visible_path(path: Path) -> str:
                return path.relative_to(ROOT).as_posix()

            base_prompt = PROFILE_PATCH_PROMPT_TEMPLATE.format(
                round=rnd,
                capability_block=disclosure["prompt_block"],
                plan_path=visible_path(rdir / "plan.json"),
                patch_protocol_path=visible_path(
                    profile_paths["patch_protocol"]
                ),
                evidence_path=visible_path(profile_paths["evidence"]),
                policy_path=visible_path(profile_paths["policy"]),
                capability_path=visible_path(profile_paths["capability"]),
                tool_capabilities_path=visible_path(
                    profile_paths["tool_capabilities"]
                ),
                candidate_path=visible_path(self.cand_dir),
            )
            p_readable = [
                rdir / "plan.json", profile_paths["patch_protocol"],
                profile_paths["evidence"], profile_paths["policy"],
                profile_paths["capability"],
                profile_paths["tool_capabilities"], self.cand_dir,
            ]
            best = Path(self._working_base_package())
            full_policy_path = (
                ROOT.parent / "tau2-bench" / "data" / "tau2" /
                "domains" / self.domain / "policy.md"
            )
            denied_read_paths = [
                rdir / "evidence", rdir / "state.md", rdir / "evidence.md",
                profile_paths["control_root"], full_policy_path,
                profile_paths["plan_protocol"], best,
            ]
            denied_directory_reads = [
                profile_paths["visible_root"], best,
                *[path for path in sorted(best.rglob("*")) if path.is_dir()],
            ]
            prompt_template_sha256 = generation_context["manifest"][
                "substantive"]["prompt_templates"]["patch_sha256"]
            attempt_timeouts = list(generation_context["patch_timeouts"])
        else:
            disclosure = self._materialize_capability_disclosure(rnd, rdir)
            activation_case_instruction = (
                f"5. `{rel}/activation_cases.json` - independent positive/negative "
                "retrieval cases. Configure the reviewed carrier so all cases pass."
                if activation_cases_path.is_file() else
                "5. No targeted retrieval-case artifact is required for this plan."
            )
            # Component-specific Claude/Opus skills remain preserved as
            # historical strategy sources. Formal runs receive this
            # decontaminated protocol, which contains construction rules but no
            # champion/task outcomes.
            protocol = self._formal_protocol(
                "patch", rdir / "phase_p_protocol.md"
            )
            (rdir / "schema_rules.md").write_text(
                self.SCHEMA_RULES, encoding="utf-8"
            )
            base_prompt = f"""You are the Meta-Agent for the Recuris framework (patch phase, round {rnd}).

{disclosure["prompt_block"]}

The block above is the complete public runtime interface contract. Use it
directly; do not seek framework source, tests, historical packages, or prior
experiment logs to infer constructor schemas.

The VALIDATED plan for this round is `{rel}/plan.json` — implement it exactly.
The candidate package (a disposable copy you may edit) is `{cand_rel}`.
Edit ONLY inside `{cand_rel}`. No other writes. No commands.
Any validated `remove_card` action has already been applied deterministically by
the driver; leave that card absent and implement the remaining fixes.

Read first:
1. `{rel}/plan.json`       — the checklist to implement (all clusters, all fixes).
2. `{rel}/schema_rules.md` — the exact file formats. Violations are rejected by a linter.
3. `{rel}/state.md` and `{rel}/evidence.md` (+ `evidence/task_*.md` for the
   clusters you implement) — the evidence the cards must be distilled from.
4. The domain policy: `{self.profile.policy_doc_display(self.domain)}`
   — cards cite the ACTUAL policy rule, never invented advice.

{activation_case_instruction}

## Mandatory patch protocol
These package-editing rules are embedded in the prompt and are not optional:

{protocol}

Card writing discipline:
- A card is a worked exemplar: the correct call signature/sequence, the policy
  rule it obeys (cite it), and the common mistake it prevents — distilled from
  the disclosed evidence and transcript, with PLACEHOLDER ids only (the evidence
  you see is already masked; keep those placeholder forms).
- Keep cards small and surgical. Do not add anything the plan does not call for.
Implement every fix in the plan, then end the session."""
            policy_path = self.profile.policy_doc_path(self.domain)
            evidence_tasks = sorted(
                {
                    str(task_id)
                    for cluster in plan["clusters"]
                    for task_id in cluster.get("evidence_tasks", [])
                },
                key=_task_sort_key,
            )
            p_readable = [
                rdir / "plan.json", rdir / "schema_rules.md",
                rdir / "state.md", rdir / "evidence.md",
                disclosure["manual_path"],
                disclosure["tool_capabilities_path"], self.cand_dir,
                policy_path,
                *([activation_cases_path] if activation_cases_path.is_file() else []),
                *[
                    rdir / "evidence" / f"task_{task_id}.md"
                    for task_id in evidence_tasks
                ],
            ]
            attempt_timeouts = [
                self.a.cc_timeout, self.a.cc_timeout, self.a.cc_timeout,
            ]
        prompt = base_prompt
        hypothesis_signature = self._plan_hypothesis_signature(plan)
        plan_correction_pending = False
        plan_correction_used = False
        for attempt, attempt_timeout in enumerate(attempt_timeouts, start=1):
            is_plan_correction = plan_correction_pending
            if generation_context is None:
                writable_paths = [self.cand_dir]
                if is_plan_correction:
                    writable_paths.append(rdir / "plan.json")
                ok = self.cc_session(
                    f"r{rnd}_p{attempt}", prompt, P_TOOLS,
                    attempt_timeout, writable_paths, p_readable,
                    capability_disclosure=disclosure,
                )
            else:
                self._assert_generation_profile_model_surface_opaque(
                    prompt, p_readable
                )
                ok = self.cc_session(
                    f"r{rnd}_p{attempt}", prompt, P_TOOLS,
                    attempt_timeout, [self.cand_dir], p_readable,
                    capability_disclosure=disclosure,
                    denied_read_paths=denied_read_paths,
                    denied_directory_reads=denied_directory_reads,
                    prompt_template_sha256=prompt_template_sha256,
                )
                self._audit_generation_profile_candidate_leaks(
                    generation_context, rnd=rnd, attempt=attempt
                )
            if not ok:
                continue
            if is_plan_correction:
                blocked = {tuple(key) for key in self.state["blocked"]}
                activation_context = self.pkg_activation_context(
                    disclosure["tool_capabilities"],
                )
                plan_tasks = {
                    str(task_id)
                    for cluster in plan.get("clusters", [])
                    for task_id in cluster.get("evidence_tasks", [])
                }
                revised_plan, revision_errors, revision_warnings = (
                    ma_plan_schema.load_and_validate(
                        rdir / "plan.json", plan_tasks, blocked,
                        self.pkg_deliverers(), expected_round=rnd,
                        current_entry_kind=activation_context["entry_kind"],
                        tool_aware_entry_kinds=
                            activation_context["tool_aware_entry_kinds"],
                        known_tools=activation_context["known_tools"],
                        mutating_tools=activation_context["mutating_tools"],
                        pkg_checkers=activation_context["checkers"],
                        checker_capabilities=activation_context.get(
                            "checker_capabilities", self._benchmark_profile().checker_capabilities()),
                        current_grounding_matcher=
                            activation_context.get("grounding_matcher"),
                        current_wm_schema=activation_context.get("wm_schema"),
                    )
                )
                for warning in revision_warnings:
                    self.log(f"corrected-plan warning: {warning}")
                revision_errors = list(revision_errors)
                if (revised_plan and
                        self._plan_hypothesis_signature(revised_plan) !=
                        hypothesis_signature):
                    revision_errors.append(
                        "validator correction changed the diagnosis hypothesis"
                    )
                if not revised_plan or revision_errors:
                    self.log(
                        "candidate correction rejected: " +
                        "; ".join(str(error) for error in revision_errors)
                    )
                    return False
                plan.clear()
                plan.update(revised_plan)
                plan_correction_pending = False
                plan_correction_used = True
            profile_surface_errors: list[str] = []
            # lint + leak
            lint = subprocess.run([PY, str(HERE / "lint.py"), "--pkg",
                                   str(self.cand_dir.relative_to(ROOT)),
                                   "--domain", self.domain,
                                   "--benchmark", getattr(self, "benchmark", "tau2")],
                                  cwd=str(ROOT), capture_output=True, text=True, timeout=300)
            leaks = (
                {} if generation_context is not None else
                ma_sanitize.scan_package(self.cand_dir, self.gold)
            )
            conformance = ([] if lint.returncode != 0 else
                            self._plan_conformance_errors(
                                plan, Path(self._working_base_package())))
            activation_errors: list[str] = []
            activation_report: dict | None = None
            profile_activation_report: dict | None = None
            targeted_activation_report: dict | None = None
            if (lint.returncode == 0 and not leaks and not conformance and
                    not profile_surface_errors):
                try:
                    activation_report = activation_probe.probe_package(
                        self.cand_dir, self.domain,
                        self._benchmark_profile().benchmark,
                    )
                    self._write_json_atomic(
                        rdir / f"activation_probe_attempt_{attempt}.json",
                        activation_report,
                    )
                    if not activation_report.get("passed"):
                        if activation_report.get("package_error"):
                            activation_errors.append(
                                str(activation_report["package_error"])
                            )
                        for card in activation_report.get("cards") or []:
                            if not card.get("activated"):
                                details = [
                                    str(item.get("detail") or "")
                                    for item in (card.get("attempts") or [])
                                    if item.get("detail")
                                ]
                                activation_errors.append(
                                    f"card {card.get('id')!r} did not ignite through "
                                    f"any real carrier: {'; '.join(details)}"
                                )
                        for mechanism in activation_report.get("mechanisms") or []:
                            if not mechanism.get("activated"):
                                activation_errors.append(
                                    f"{mechanism.get('kind')} "
                                    f"{mechanism.get('name')!r} did not ignite: "
                                    f"{mechanism.get('detail') or 'unknown reason'}"
                                )
                    if (not activation_errors and
                            generation_context is not None):
                        profile_activation_report = (
                            self._probe_generation_profile_activation_queries(
                                generation_context, plan
                            )
                        )
                        self._write_json_atomic(
                            generation_context["paths"]["attempts"] /
                            f"activation_query_probe_p{attempt}.json",
                            profile_activation_report,
                        )
                        if not profile_activation_report.get("passed"):
                            profile_activation_error = (
                                profile_activation_report.get("error") or
                                "new knowledge card was not selected"
                            )
                            activation_errors.append(
                                "locked visible-query lexical probe failed: "
                                f"{profile_activation_error}"
                            )
                    elif (not activation_errors and
                          activation_cases_path.is_file()):
                        case_spec = json.loads(
                            activation_cases_path.read_text(encoding="utf-8")
                        )
                        targeted_activation_report = (
                            activation_probe.probe_retrieval_cases(
                                self.cand_dir, case_spec, self.domain,
                                self._benchmark_profile().benchmark,
                            )
                        )
                        self._write_json_atomic(
                            rdir / f"targeted_activation_probe_attempt_{attempt}.json",
                            targeted_activation_report,
                        )
                        if not targeted_activation_report.get("passed"):
                            activation_errors.extend(
                                "targeted retrieval probe: " + str(error)
                                for error in (
                                    targeted_activation_report.get("errors") or
                                    ["positive/negative cases did not all pass"]
                                )
                            )
                except Exception as exc:
                    activation_errors.append(
                        f"offline activation probe crashed: {type(exc).__name__}: {exc}"
                    )
            if (lint.returncode == 0 and not leaks and not conformance and
                    not profile_surface_errors and
                    not activation_errors and activation_report is not None):
                self._write_json_atomic(
                    rdir / "activation_probe.json", activation_report,
                )
                if generation_context is not None:
                    if profile_activation_report is None:
                        raise RuntimeError(
                            "profiled candidate lacks activation-query proof"
                        )
                    self._write_json_atomic(
                        generation_context["paths"][
                            "activation_query_proof"],
                        profile_activation_report,
                    )
                    generation_context["activation_query_proof"] = {
                        "path": str(generation_context["paths"][
                            "activation_query_proof"]),
                        "sha256": self._sha256_file(
                            generation_context["paths"][
                                "activation_query_proof"]
                        ),
                        "report": copy.deepcopy(
                            profile_activation_report
                        ),
                    }
                elif targeted_activation_report is not None:
                    self._write_json_atomic(
                        rdir / "targeted_activation_probe.json",
                        targeted_activation_report,
                    )
                self.log(
                    "candidate passes lint + leak + plan-conformance + "
                    "offline activation referee"
                )
                return True
            self.log(f"candidate rejected (lint rc={lint.returncode}, leaks={len(leaks)}, "
                      f"conformance={len(conformance)}, "
                      f"profile_surface={len(profile_surface_errors)}, "
                      f"activation={len(activation_errors)}); "
                      + ("retry with feedback"
                         if attempt < len(attempt_timeouts) else "giving up"))
            targeted_failed = bool(
                targeted_activation_report is not None and
                not targeted_activation_report.get("passed")
            )
            if is_plan_correction:
                self.log("the single same-hypothesis candidate correction failed")
                return False
            if profile_surface_errors:
                self.log(
                    "candidate disclosed a raw profile task id; failing "
                    "closed without re-exposing it to another model attempt"
                )
                return False
            fb = ["", "## Your previous edit was REJECTED by the deterministic referee. "
                      "Fix ALL of this:"]
            if lint.returncode != 0:
                fb += ["### Linter output:", "```", lint.stdout[-2500:], "```"]
            if leaks:
                fb.append("### LEAKED REAL IDS (red line — replace with placeholders):")
                fb += [f"- {f}: {h}" for f, h in leaks.items()]
            if conformance:
                fb.append("### Plan-conformance errors:")
                fb += [f"- {error}" for error in conformance]
                if any("was not implemented" in str(error) for error in conformance):
                    fb += [
                        "One of these can mean the plan named a cfg value the "
                        "linter will not accept. You may edit plan.json to the "
                        "value you actually used, and must then make the package "
                        "match it exactly. Change cfg values only: the cluster, "
                        "the component, the action, the card and trigger "
                        "identity, the deliverer and the checker are fixed, and "
                        "a correction that moves any of them is rejected.",
                    ]
            if activation_errors:
                fb.append("### Offline activation errors (no model/network calls):")
                fb += [f"- {error}" for error in activation_errors]
            # A conformance error can mean the plan asked for something the
            # linter will not accept, which the candidate alone cannot fix.
            cfg_deadlock = bool(conformance) and lint.returncode != 0 or bool(
                [e for e in conformance if "was not implemented" in str(e)])
            if (targeted_failed or cfg_deadlock) and not plan_correction_used:
                plan_correction_pending = True
                probe_feedback_path = (
                    rdir / f"targeted_activation_probe_attempt_{attempt}.json"
                )
                if probe_feedback_path not in p_readable:
                    p_readable.append(probe_feedback_path)
                fb += [
                    "### One hypothesis-preserving correction is allowed:",
                    f"Read `{probe_feedback_path.relative_to(ROOT).as_posix()}`. ",
                    "Edit plan.json and the candidate together only as needed to "
                    "make these cases pass. Preserve cluster ids, evidence tasks, "
                    "components, failure modes, actions, and action targets. Change "
                    "no hypothesis and add no files/components.",
                ]
            fb.append(f"Re-open the files under `{cand_rel}`, fix the problems, "
                      f"change nothing else.")
            prompt = base_prompt + "\n" + "\n".join(fb)
        return False

    def _validate_continuous_candidate(
            self, rnd: int, rdir: Path, plan: dict) -> bool:
        """Validate, allow bounded package-only corrections, then decide."""
        base = Path(self._working_base_package())

        def normalize_candidate_name() -> None:
            # The manifest `name` field is never a legitimate patch target (no
            # plan action can declare it), so a renamed candidate is restored
            # deterministically instead of burning a model correction on it.
            base_name = str((self._manifest_data(base) or {}).get("name") or "")
            manifest_path = self.cand_dir / "manifest.yaml"
            if not base_name or not manifest_path.is_file():
                return
            text = manifest_path.read_text(encoding="utf-8")
            fixed = re.sub(r"(?m)^name:.*$", f"name: {base_name}", text, count=1)
            if fixed != text:
                manifest_path.write_text(fixed, encoding="utf-8")

        def validate_once(attempt: int) -> tuple[dict, dict | None,
                                                  list[str], bool]:
            normalize_candidate_name()
            lint = subprocess.run(
                [PY, str(HERE / "lint.py"), "--pkg",
                 str(self.cand_dir.relative_to(ROOT)), "--domain", self.domain,
                 "--benchmark", getattr(self, "benchmark", "tau2")],
                cwd=str(ROOT), capture_output=True, text=True, timeout=300,
            )
            leaks = ma_sanitize.scan_package(self.cand_dir, self.gold)
            # Both referees always report together: a correction that satisfies
            # one must not be blind to the other (the lint/conformance deadlock
            # observed in retail_meta_from0_obs_v1 rounds 1-3).
            conformance = self._plan_conformance_errors(plan, base)
            activation_errors: list[str] = []
            activation_report: dict | None = None
            if lint.returncode == 0 and not leaks and not conformance:
                try:
                    activation_report = activation_probe.probe_package(
                        self.cand_dir, self.domain,
                        self._benchmark_profile().benchmark,
                    )
                    self._write_json_atomic(
                        rdir / f"activation_probe_attempt_{attempt}.json",
                        activation_report,
                    )
                    if not activation_report.get("passed"):
                        if activation_report.get("package_error"):
                            activation_errors.append(
                                str(activation_report["package_error"])
                            )
                        for card in activation_report.get("cards") or []:
                            if not card.get("activated"):
                                activation_errors.append(
                                    f"card {card.get('id')!r} did not activate"
                                )
                        for mechanism in (
                                activation_report.get("mechanisms") or []):
                            if not mechanism.get("activated"):
                                activation_errors.append(
                                    f"{mechanism.get('kind')} "
                                    f"{mechanism.get('name')!r} did not activate"
                                )
                except Exception as exc:
                    activation_errors.append(
                        "offline activation probe crashed: "
                        f"{type(exc).__name__}: {exc}"
                    )

            errors: list[str] = []
            if lint.returncode != 0:
                lint_detail = (lint.stdout or "").strip() or (lint.stderr or "").strip()
                errors.append(
                    "package lint failed:\n" + lint_detail[-2000:]
                )
            if leaks:
                errors.append(
                    "candidate contains task-specific identifier leakage"
                )
            errors.extend(conformance)
            errors.extend(activation_errors)
            report = {
                "round": rnd,
                "attempt": attempt,
                "passed": not errors and activation_report is not None,
                "lint_returncode": lint.returncode,
                "lint_output": lint.stdout[-2500:] if lint.returncode else "",
                "leak_count": len(leaks),
                "conformance_errors": conformance,
                "activation_errors": activation_errors,
            }
            self._write_json_atomic(
                rdir / f"continuous_validation_attempt_{attempt}.json",
                report,
            )
            return report, activation_report, errors, bool(leaks)

        report, activation_report, errors, has_leaks = validate_once(1)
        max_corrections = 3
        corrections: list[dict] = []
        correction_failure: dict | None = None
        while errors and not has_leaks and len(corrections) < max_corrections:
            # Keep the correction context lean: embed the plan, the schema
            # rules, and the current candidate only.  The base package is on
            # the session's readable path list — pointing at it instead of
            # embedding it halves the prompt for card-heavy packages (a
            # correction session timed out on the embedded-everything prompt
            # in retail_meta_formal_v1 round 1).
            correction_files = [
                rdir / "plan.json", rdir / "schema_rules.md",
                *self._package_prompt_files(self.cand_dir),
            ]
            correction_prompt = f"""You are correcting the package implementation for continuous Meta-Agent round {rnd} (correction attempt {len(corrections) + 1} of {max_corrections}).

The diagnosis plan is fixed and read-only. Deterministic validation found the
errors below. They come from TWO referees that must BOTH pass: `ma_lint`
(package legality — kernel requirements such as mandatory companion fields)
and plan conformance (the package may change exactly what the plan declares,
nothing else). Read every error before editing: a change that silences one
referee can trigger the other. Edit only
`{self.cand_dir.relative_to(ROOT).as_posix()}` so it implements the plan
exactly while staying kernel-legal. Restore every unrelated field from the
base package, which is readable at
`{base.relative_to(ROOT).as_posix()}` (read only the files you need). Do not
change the plan, add a diagnosis, or alter any planned action. Make all
necessary edits, then end the session.

Validation errors:
{json.dumps(errors, ensure_ascii=False, indent=2)}

Embedded plan, schema, and current candidate:
{self._embedded_file_context(correction_files)}
"""
            ok = self.cc_session(
                f"r{rnd}_continuous_candidate_fix_a{len(corrections) + 1}",
                correction_prompt,
                P_TOOLS, min(self.a.cc_timeout, 1200),
                [self.cand_dir],
                [self.cand_dir, rdir, base],
            )
            corrections.append({"attempt": len(corrections) + 1, "session_ok": ok})
            if not ok:
                session = getattr(self, "last_cc_session", {})
                failure_class = str(session.get("failure_class") or "unknown")
                correction_failure = {
                    "failure_class": failure_class,
                    "failure_detail": session.get("failure_detail"),
                    "timed_out": session.get("timed_out"),
                }
                if failure_class != "harness-timeout":
                    break
                # A timed-out correction is a speed problem, not a semantic
                # dead end — and it may have left partial edits behind.
                # Re-validate to refresh the error list, then spend the
                # remaining correction budget on a fresh session.
                report, activation_report, errors, has_leaks = validate_once(
                    len(corrections) + 1
                )
                continue
            report, activation_report, errors, has_leaks = validate_once(
                len(corrections) + 1
            )

        passed = bool(report["passed"])
        report = dict(report)
        report.update({
            "correction_attempted": bool(corrections),
            "corrections": corrections,
            "correction_succeeded": (
                corrections[-1]["session_ok"] if corrections else None
            ),
            "correction_failure": correction_failure,
        })
        self._write_json_atomic(rdir / "continuous_validation.json", report)
        self.last_validation_report = report
        if passed:
            self._write_json_atomic(
                rdir / "activation_probe.json", activation_report,
            )
            self.log(
                "continuous candidate passes lint + leak + plan-conformance + "
                "offline activation referee"
            )
            return True
        self.log(
            "continuous candidate rejected before evaluation: "
            + "; ".join(errors)
        )
        return False

    def freeze_generated_candidate(
            self, rnd: int, plan: dict, repair_tasks: list[str],
            train_res, generation_context: dict | None = None) -> str:
        """Freeze a validated candidate before any held-out outcome is observed."""
        if not self.generate_only:
            raise RuntimeError("candidate freezing requires --generate-only")
        self.integrity()
        if self.state.get("sims") != 0:
            raise RuntimeError(
                "generation-only candidate cannot be frozen after downstream simulations"
            )
        provenance = json.loads(
            self.provenance_path.read_text(encoding="utf-8")
        )
        if provenance.get("downstream_artifacts"):
            raise RuntimeError(
                "generation-only candidate cannot be frozen after a downstream attempt"
            )
        if not self.replay_source_run or not hasattr(self, "_replay_snapshot"):
            raise RuntimeError("generation-only candidate lacks a sealed replay source")

        alias_plan_path = self.run_dir / f"round_{rnd}" / "plan.json"
        profile_plan_outputs = None
        if generation_context is None:
            plan_path = alias_plan_path
            profile_activation_proof = None
        else:
            profile_plan_outputs = (
                self._verify_generation_profile_plan_outputs(
                    generation_context
                )
            )
            plan_path = generation_context["paths"]["mapped_plan"]
            profile_activation_proof = generation_context.get(
                "activation_query_proof"
            )
            activation_query_path = generation_context["paths"][
                "activation_query_proof"
            ]
            if (
                    not isinstance(profile_activation_proof, dict) or
                    profile_activation_proof.get("path") !=
                    str(activation_query_path) or
                    not activation_query_path.is_file() or
                    self._sha256_file(activation_query_path) !=
                    profile_activation_proof.get("sha256") or
                    json.loads(activation_query_path.read_text(
                        encoding="utf-8")) !=
                    profile_activation_proof.get("report") or
                    profile_activation_proof["report"].get("passed")
                    is not True or
                    profile_activation_proof["report"].get("offline")
                    is not True or
                    profile_activation_proof["report"].get("model_calls")
                    != 0 or
                    profile_activation_proof["report"].get("network_calls")
                    != 0):
                raise RuntimeError(
                    "candidate freeze lacks a valid locked visible-query "
                    "activation proof"
                )
        if not plan_path.is_file():
            raise RuntimeError("validated candidate lacks its plan.json")
        activation_path = self.run_dir / f"round_{rnd}" / "activation_probe.json"
        if not activation_path.is_file():
            raise RuntimeError("validated candidate lacks its offline activation proof")
        activation_report = json.loads(
            activation_path.read_text(encoding="utf-8")
        )
        if (not isinstance(activation_report, dict) or
                activation_report.get("passed") is not True or
                activation_report.get("offline") is not True or
                activation_report.get("model_calls") != 0 or
                activation_report.get("network_calls") != 0):
            raise RuntimeError("candidate offline activation proof is invalid or failed")
        if any(
                not isinstance(card, dict) or card.get("activated") is not True
                for card in (activation_report.get("cards") or [])):
            raise RuntimeError("candidate freeze found an unactivated Skill card")
        if any(
                not isinstance(mechanism, dict) or
                mechanism.get("activated") is not True
                for mechanism in (activation_report.get("mechanisms") or [])):
            raise RuntimeError("candidate freeze found an unactivated mechanism")
        persisted_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if persisted_plan != plan:
            raise RuntimeError("in-memory plan differs from the validated plan.json")
        self._assert_tree_unlinked(self.cand_dir)
        candidate_sha256 = self._package_digest(self.cand_dir)
        plan_file_sha256 = self._sha256_file(plan_path)
        plan_canonical_sha256 = hashlib.sha256(json.dumps(
            plan, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

        frozen_root = self.package_root / "generated_candidates"
        frozen_dir = frozen_root / f"C{rnd}"
        self._assert_no_linklike(frozen_root, allow_missing=True)
        assert_mutation_paths_safe([frozen_root, frozen_dir])
        frozen_root.mkdir(parents=False, exist_ok=True)
        if frozen_dir.exists():
            raise RuntimeError(
                f"refusing to overwrite frozen generated candidate: {frozen_dir}"
            )
        temp_dir = frozen_root / f".C{rnd}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copytree(self.cand_dir, temp_dir)
            if self._package_digest(temp_dir) != candidate_sha256:
                raise RuntimeError("generated candidate freeze copy failed SHA-256 verification")
            temp_dir.replace(frozen_dir)
        finally:
            if temp_dir.exists():
                self._safe_rmtree(temp_dir)

        replay = copy.deepcopy(self._replay_snapshot)
        replay_path = self.run_dir / "replay_source.json"
        if not replay_path.is_file():
            raise RuntimeError("generation-only candidate lacks replay_source.json")
        if generation_context is None:
            policy_path = (
                ROOT.parent / "tau2-bench" / "data" / "tau2" /
                "domains" / self.domain / "policy.md"
            )
            diagnosis_protocol_path = FORMAL_PROTOCOL_DIR / "diagnosis.md"
            patch_protocol_path = FORMAL_PROTOCOL_DIR / "patch.md"
        else:
            if self.generation_profile_spec is None:
                raise RuntimeError(
                    "generation context supplied without a locked profile"
                )
            profile_record = (
                provenance.get("generation_profiles") or {}
            ).get(str(rnd))
            if profile_record != generation_context.get("record"):
                raise RuntimeError(
                    "candidate freeze generation-profile provenance mismatch"
                )
            policy_path = generation_context["paths"]["policy"]
            diagnosis_protocol_path = generation_context["paths"][
                "plan_protocol"
            ]
            patch_protocol_path = generation_context["paths"][
                "patch_protocol"
            ]
        if not policy_path.is_file():
            raise RuntimeError(f"domain policy is missing: {policy_path}")
        candidate_files = {
            path.relative_to(frozen_dir).as_posix():
                hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(frozen_dir.rglob("*"))
            if path.is_file() and "__pycache__" not in
                path.relative_to(frozen_dir).parts
            and path.suffix.lower() not in {".pyc", ".pyo"}
        }
        core_function_names = [
            "cc_session", "phase_d", "phase_p",
            "_cc_search_scope_violations",
            "_plan_conformance_errors", "pkg_deliverers",
            "pkg_activation_context", "_build_capability_disclosure",
            "_materialize_capability_disclosure",
        ]
        if getattr(self, "meta_workflow", "split") in {
                "continuous", "hierarchical"}:
            core_function_names.extend([
                "phase_continuous", "_validate_continuous_candidate",
            ])
        if getattr(self, "meta_workflow", "split") == "hierarchical":
            core_function_names.extend([
                "_diagnosis_report_errors", "_parallel_diagnosis_reports",
            ])
        if generation_context is not None:
            core_function_names.extend([
                "_load_generation_profile_spec",
                "_validate_generation_profile_treatment",
                "_profile_source_proof",
                "_build_generation_profile_manifest",
                "_materialize_generation_profile",
                "_verify_generation_profiles",
                "_assert_generation_profile_resume_safe",
                "_assert_generation_profile_model_surface_opaque",
                "_validate_and_map_generation_profile_plan",
                 "_freeze_generation_profile_plan_outputs",
                 "_verify_generation_profile_plan_outputs",
                 "_probe_generation_profile_activation_queries",
                 "_audit_generation_profile_candidate_leaks",
                 "_profile_session_binding_errors",
             ])
        core_function_sha256 = {
            name: hashlib.sha256(
                inspect.getsource(getattr(type(self), name)).encode("utf-8")
            ).hexdigest()
            for name in core_function_names
        }
        failure_owners = copy.deepcopy(
            getattr(train_res, "failure_owners", None) or {}
        )
        if generation_context is None:
            forced_owners = {
                str(task): str(record.get("owner"))
                for task, record in failure_owners.items()
                if isinstance(record, dict) and record.get("owner")
            }
            forced_opportunities = {
                str(task): str(record.get("action_opportunity"))
                for task, record in failure_owners.items()
                if isinstance(record, dict) and record.get(
                    "action_opportunity"
                )
            }
        else:
            forced_owners = copy.deepcopy(
                generation_context["forced_owners"]
            )
            forced_opportunities = copy.deepcopy(
                generation_context["forced_opportunities"]
            )
        champion_proof = verify_champions()
        session_keys = (
            "name", "session_key", "requested_model",
             "requested_reasoning_effort", "requested_temperature",
             "resolved_models", "upstream_models",
             "upstream_model_counts_before",
             "upstream_model_counts_after",
             "upstream_model_count_delta", "upstream_chunk_count",
             "upstream_counter_errors",
             "upstream_model_verified", "tool_surface", "observed_tools",
            "permission_denials", "failed_read_results",
            "read_scope_violations", "edit_scope_violations",
            "stray_writes",
            "trace", "trace_sha256", "prompt", "prompt_sha256",
            "settings", "settings_sha256", "capability_disclosure", "ok",
        )
        if generation_context is not None:
            session_keys += (
                "timeout_seconds", "prompt_template_sha256",
                "denied_read_paths", "denied_directory_reads",
                "profile_tool_surface_verified",
            )
        session_proof = [
            {key: copy.deepcopy(session.get(key)) for key in session_keys}
            for session in provenance.get("meta_sessions", [])
            if isinstance(session, dict)
        ]
        if generation_context is not None:
            profiled_sessions = [
                session for session in session_proof
                if re.fullmatch(
                    fr"r{rnd}_[dp]\d+",
                    str(session.get("name") or ""),
                )
            ]
            for session in profiled_sessions:
                session_match = re.fullmatch(
                    fr"r{rnd}_([dp])(\d+)",
                    str(session.get("name") or ""),
                )
                phase_name = (
                    "diagnosis" if session_match.group(1) == "d"
                    else "patch"
                )
                attempt_index = int(session_match.group(2)) - 1
                expected_timeouts = (
                    generation_context["diagnosis_timeouts"]
                    if phase_name == "diagnosis" else
                    generation_context["patch_timeouts"]
                )
                expected_template_hash = generation_context["manifest"][
                    "substantive"]["prompt_templates"][
                        f"{phase_name}_sha256"
                    ]
                binding_errors = self._profile_session_binding_errors(
                    session,
                    model=self.a.meta_model,
                    reasoning=self.a.meta_reasoning,
                    temperature=META_TEMPERATURE,
                )
                if (
                        len(session.get("tool_surface") or []) != 2 or
                        set(session.get("tool_surface") or []) !=
                        {"Read", "Edit"} or
                        session.get("profile_tool_surface_verified")
                        is not True or
                        session.get("read_scope_violations") or
                        session.get("edit_scope_violations") or
                        session.get("stray_writes") or
                        attempt_index not in range(len(expected_timeouts)) or
                        session.get("timeout_seconds") !=
                        expected_timeouts[attempt_index] or
                        session.get("prompt_template_sha256") !=
                        expected_template_hash or
                        binding_errors):
                    raise RuntimeError(
                        "candidate freeze found an unsafe locked-profile "
                        f"session: {session.get('name')}"
                    )
            if not any(
                    str(session.get("name") or "").startswith(f"r{rnd}_d")
                    and session.get("ok") is True
                    for session in profiled_sessions):
                raise RuntimeError(
                    "candidate freeze lacks a successful profiled D session"
                )
            if not any(
                    str(session.get("name") or "").startswith(f"r{rnd}_p")
                    and session.get("ok") is True
                    for session in profiled_sessions):
                raise RuntimeError(
                    "candidate freeze lacks a successful profiled P session"
                )
        freeze_record = {
            "schema_version": 1,
            "created_at": _now(),
            "run_id": self.a.run_id,
            "round": rnd,
            "status": "frozen-before-any-held-out-evaluation",
            "source": {
                "run_id": replay.get("source_run_id"),
                "round": replay.get("source_round"),
                "replay_source_path": str(replay_path),
                "replay_source_sha256": self._sha256_file(replay_path),
                "base_package_tree_sha256":
                    (replay.get("base_package") or {}).get("tree_sha256"),
                "train_results_sha256":
                    (replay.get("train_artifact") or {}).get("results_sha256"),
                "train_fingerprint_sha256":
                    (replay.get("train_artifact") or {}).get("fingerprint_sha256"),
                "reparsed_proof": replay.get("reparsed_proof"),
                "copied_evidence": replay.get("copied_evidence"),
                "forced_owners": forced_owners,
                "forced_opportunities": forced_opportunities,
            },
            "meta_agent": {
                "model": self.a.meta_model,
                "reasoning_effort": self.a.meta_reasoning,
                "temperature": META_TEMPERATURE,
                "sessions": session_proof,
            },
            "generation_runtime": {
                "tree_sha256":
                    (provenance.get("runtime_sources") or {}).get("tree_sha256"),
                "changed_from_source": replay.get("changed_runtime_files"),
                "allowed_generation_only_changes":
                    replay.get("allowed_runtime_changes"),
                "core_function_sha256": core_function_sha256,
                "diagnosis_protocol_sha256": self._sha256_file(
                    diagnosis_protocol_path
                ),
                "patch_protocol_sha256": self._sha256_file(
                    patch_protocol_path
                ),
                "capability_disclosure": copy.deepcopy(
                    (provenance.get("capability_disclosures") or {}).get(
                        str(rnd)
                    )
                ),
                "policy_path": str(policy_path),
                "policy_sha256": self._sha256_file(policy_path),
            },
            "validated_plan": {
                "path": str(plan_path),
                "file_sha256": plan_file_sha256,
                "canonical_sha256": plan_canonical_sha256,
                "repair_tasks": list(repair_tasks),
                "actionable_clusters": [
                    {
                        "cluster_id": cluster.get("cluster_id"),
                        "component": cluster.get("component"),
                        "evidence_tasks": cluster.get("evidence_tasks"),
                        "fix_actions": [
                            fix.get("action")
                            for fix in cluster.get("fixes", [])
                        ],
                    }
                    for cluster in plan.get("clusters", [])
                    if cluster.get("component") not in {
                        "CEILING", "MODEL", "HARNESS", "BENCHMARK",
                    }
                ],
            },
            "activation_probe": {
                "path": str(activation_path),
                "sha256": self._sha256_file(activation_path),
                "offline": True,
                "model_calls": 0,
                "network_calls": 0,
                "cards": [
                    {
                        "id": card.get("id"),
                        "type": card.get("type"),
                        "event": card.get("event"),
                        "tool": card.get("tool"),
                        "activated": card.get("activated"),
                    }
                    for card in (activation_report.get("cards") or [])
                ],
                "mechanisms": [
                    {
                        "kind": mechanism.get("kind"),
                        "name": mechanism.get("name"),
                        "event": mechanism.get("event"),
                        "activated": mechanism.get("activated"),
                    }
                    for mechanism in (activation_report.get("mechanisms") or [])
                ],
            },
            "candidate": {
                "source_path": str(self.cand_dir),
                "frozen_path": str(frozen_dir),
                "tree_sha256": candidate_sha256,
                "files": candidate_files,
                "validation": [
                    "plan-schema",
                    "package-lint",
                    "gold-id-leak-scan",
                    "plan-conformance",
                    "offline-activation",
                    *(
                        ["locked-generation-profile"]
                        if generation_context is not None else []
                    ),
                ],
            },
            "held_out_isolation": {
                "downstream_simulations": self.state.get("sims"),
                "downstream_artifacts": [],
                "evaluation_started": False,
                "evaluation_protocol": None,
                "evaluation_forbidden_by_mode": True,
            },
            "state_proof": {
                "best": self.state.get("best"),
                "best_tree_sha256": self.state.get(
                    "version_hashes", {}
                ).get(Path(str(self.state.get("best"))).name),
                "candidate_promoted": False,
            },
            "champion_integrity": champion_proof,
        }
        if generation_context is not None:
            freeze_record["validated_plan"]["matched_rule_ids"] = (
                copy.deepcopy(
                    profile_plan_outputs["matched_rule_ids"]
                )
            )
            freeze_record["generation_profile"] = {
                "provenance": copy.deepcopy(generation_context["record"]),
                "input_manifest": copy.deepcopy(
                    generation_context["manifest"]
                ),
                "plan_outputs": copy.deepcopy(profile_plan_outputs),
                "activation_query_proof":
                    copy.deepcopy(profile_activation_proof),
            }
            if "comparison_lock" in self.generation_profile_spec:
                freeze_record["generation_profile"]["comparison_lock"] = (
                    copy.deepcopy(
                        self.generation_profile_spec["comparison_lock"]
                    )
                )
                freeze_record["generation_profile"][
                    "matched_comparison_arm"
                ] = copy.deepcopy(
                    self.generation_profile_comparison_arm
                )
        freeze_path = self.run_dir / f"candidate_freeze_r{rnd}.json"
        if freeze_path.exists():
            raise RuntimeError(f"refusing to overwrite candidate freeze record: {freeze_path}")
        self._write_json_atomic(freeze_path, freeze_record)
        self._finalize_round(rnd, {
            "outcome": "candidate-generated",
            "ledger": {
                "round": rnd,
                "phase": "generation-only",
                "candidate_tree_sha256": candidate_sha256,
                "plan_bundle_sha256": plan_canonical_sha256,
                "repair_tasks": list(repair_tasks),
                "downstream_simulations": 0,
            },
        })
        self.integrity()
        self.log(
            f"GENERATION-ONLY r{rnd}: candidate frozen at {frozen_dir.name} "
            f"sha256={candidate_sha256}; downstream simulations=0"
        )
        return "candidate-generated"

    # ---------- gate ----------
    @staticmethod
    def _require_paired_seed_maps(left: dict, right: dict, label: str) -> None:
        if not isinstance(left, dict) or not isinstance(right, dict) or left != right:
            raise RuntimeError(f"{label} did not use an identical task/trial seed map")

    @staticmethod
    def _require_same_benchmark_commit(left: str, right: str, label: str) -> None:
        if (not re.fullmatch(r"[0-9a-f]{40}", str(left or "")) or
                not re.fullmatch(r"[0-9a-f]{40}", str(right or "")) or
                left != right):
            raise RuntimeError(f"{label} did not use the same benchmark git commit")

    @staticmethod
    def _require_same_benchmark_input(left: str, right: str, label: str) -> None:
        if (not re.fullmatch(r"[0-9a-f]{64}", str(left or "")) or
                not re.fullmatch(r"[0-9a-f]{64}", str(right or "")) or
                left != right):
            raise RuntimeError(f"{label} did not use the same benchmark input data")

    def _working_base_package(self) -> str:
        """Package the next candidate builds on.

        Normally the committed best.  Under --provisional-accept a rejected
        candidate that repaired its train targets without hurting dev may
        stand in as the working base for exactly one following round; the
        committed lineage in state["best"] never advances until a strict
        gate accept certifies the cumulative package against it.
        """
        provisional = self.state.get("provisional")
        if provisional:
            return str(provisional["package"])
        return str(self.state["best"])

    def _working_base(self) -> tuple[str, str]:
        """Working-base package plus its expected immutable tree hash."""
        provisional = self.state.get("provisional")
        if provisional:
            return str(provisional["package"]), str(provisional["tree_sha256"])
        base = self.state["best"]
        return str(base), self.state["version_hashes"][Path(base).name]

    def _dense_rewards(self) -> bool:
        """Does this benchmark's verifier score in [0,1] instead of {0,1}?

        The single opt-in signal for the whole statistical layer: the
        downstream shim declares it, never the benchmark's name.  Absent shim
        (qualify-only runs, gate unit tests) means binary, so every existing
        line keeps today's numbers.
        """
        return bool(getattr(getattr(self, "ds", None), "dense_rewards", False))

    def _dense_material(self) -> float:
        """Movement a dense per-task mean must exceed to count; 0 when binary."""
        if not self._dense_rewards():
            return 0.0
        return float(getattr(self.ds, "dense_material_delta",
                             DENSE_MATERIAL_DELTA))

    def gate(self, rnd: int, plan: dict | None, repair_tasks: list,
             train_res) -> dict:
        # Repair screening is an increment test against the package the
        # candidate was actually built from (the working base).  The held-out
        # dev gate always compares against the committed best so an accept
        # certifies the CUMULATIVE package against the committed lineage.
        base, expected_base_sha256 = self._working_base()
        committed = self.state["best"]
        committed_sha256 = self.state["version_hashes"][Path(committed).name]
        dense = self._dense_rewards()
        material = self._dense_material()
        repair_ok, rb, rc = True, None, None
        repair_flips = None
        base_rep = rep = None
        repair_changes: list[dict[str, object]] = []
        candidate_tree_sha256 = None
        if repair_tasks:
            # Screen the proposed mechanism before spending held-out budget.
            # Both arms are still contemporaneous; old train scores are never
            # treated as a causal baseline for the interactive simulator.
            base_rep = self.evaluate(str(base), repair_tasks, self.a.k,
                                     tag=f"r{rnd}_base_rep")
            rep = self.evaluate(str(self.cand_dir), repair_tasks, self.a.k,
                                tag=f"r{rnd}_rep")
            self._require_paired_seed_maps(
                base_rep.seeds, rep.seeds, "baseline/candidate repair gate"
            )
            self._require_same_benchmark_commit(
                base_rep.benchmark_git_commit,
                rep.benchmark_git_commit,
                "baseline/candidate repair gate",
            )
            self._require_same_benchmark_input(
                base_rep.benchmark_input_sha256,
                rep.benchmark_input_sha256,
                "baseline/candidate repair gate",
            )
            if base_rep.artifact.get("package_tree_sha256") != expected_base_sha256:
                raise RuntimeError(
                    "repair baseline did not bind the immutable best bytes"
                )
            candidate_tree_sha256 = rep.artifact.get("package_tree_sha256")
            if not candidate_tree_sha256:
                raise RuntimeError("candidate repair evaluation lacks a package SHA-256")
            repair_changes = self._write_repair_evidence(
                rnd, base_rep, rep, repair_tasks,
            )
            repair_flips = _seed_paired_flips(
                base_rep.matrix, rep.matrix, repair_tasks)
            rb = statistics.mean(statistics.mean(base_rep.matrix.get(t, [0]))
                                 for t in repair_tasks)
            rc = statistics.mean(statistics.mean(rep.matrix.get(t, [0]))
                                 for t in repair_tasks)
            repair_ok = rc > rb + 1e-9
            if not repair_ok:
                verdict = {
                    "round": rnd, "accept": False, "gate_stage": "repair",
                    "dev_evaluated": False, "net_pp": None, "ci": None,
                    "up": 0, "dn": 0, "repair_base": rb,
                    "repair_cand": rc, "repair_ok": False,
                    "repair_changes": repair_changes,
                    "evidence_status": "inconclusive",
                    "candidate_tree_sha256": candidate_tree_sha256,
                }
                self.log(
                    f"GATE r{rnd}: repair {rb}->{rc} did not improve; "
                    "held-out evaluation skipped => REJECT"
                )
                return verdict

        # Re-run the current best immediately beside every candidate.  Reusing
        # a historical k=1 baseline makes interactive simulator drift look like
        # a causal package effect even when the nominal seed is unchanged.
        current_base_dev = self.evaluate(str(committed), self.dev_ids, self.a.k,
                                         tag=f"r{rnd}_base_dev")
        cand_dev = self.evaluate(str(self.cand_dir), self.dev_ids, self.a.k,
                                 tag=f"r{rnd}_dev")
        self._require_paired_seed_maps(
            current_base_dev.seeds, cand_dev.seeds, "baseline/candidate dev gate"
        )
        self._require_same_benchmark_commit(
            current_base_dev.benchmark_git_commit,
            cand_dev.benchmark_git_commit,
            "baseline/candidate dev gate",
        )
        self._require_same_benchmark_input(
            current_base_dev.benchmark_input_sha256,
            cand_dev.benchmark_input_sha256,
            "baseline/candidate dev gate",
        )
        if current_base_dev.artifact.get("package_tree_sha256") != committed_sha256:
            raise RuntimeError("contemporaneous baseline did not bind the immutable best bytes")
        dev_candidate_sha256 = cand_dev.artifact.get("package_tree_sha256")
        if not dev_candidate_sha256:
            raise RuntimeError("candidate dev evaluation did not bind a package SHA-256")
        if (candidate_tree_sha256 is not None and
                candidate_tree_sha256 != dev_candidate_sha256):
            raise RuntimeError("repair and dev evaluations used different candidate bytes")
        candidate_tree_sha256 = dev_candidate_sha256
        v = held_out_paired_gate(
            current_base_dev.matrix, cand_dev.matrix, reg_cap=self.a.reg_cap,
            material=material,
        )
        damage = _perfect_stratum_damage(
            current_base_dev.matrix, cand_dev.matrix, material)
        if damage["lost_cells"]:
            self.log(
                f"perfect-stratum reading r{rnd}: {damage['lost_rate_pp']:+.2f}pp "
                f"on {damage['perfect_tasks']} task(s) vs null "
                f"{damage['null_rate_pp']:+.2f}pp for an unchanged package "
                f"(diagnostic; selection-biased, no reject authority)"
            )
        net = v.net * 100
        dev_b, dev_c, dev_n = _seed_paired_flips(
            current_base_dev.matrix, cand_dev.matrix)
        # Dense rewards: a cell is a partial-credit score, so counting
        # discordant cells discards the size of every change and reads a
        # 0.2 -> 0.9 repair as one flip.  Same accept rules, same alpha; only
        # the p-value estimator changes, from the cell sign test to a
        # task-clustered paired bootstrap of the mean score difference.
        # The harm test carries `material` into the estimate, not just into
        # the counters: without it a package that moved every dev task 0.97 ->
        # 0.95 reports dn=0 and lost_cells=0 and is then vetoed by p_harm on
        # the same wobble those two rules were written to tolerate.  The
        # improvement test keeps the undiscounted null.
        gate_stats = {
            "dev_flips": [dev_b, dev_c, dev_n],
            "p_dev_pos": (
                _paired_mean_bootstrap_p(
                    current_base_dev.matrix, cand_dev.matrix, "positive")
                if dense else _mcnemar_one_sided(dev_b, dev_c)),
            "p_harm": (
                _paired_mean_bootstrap_p(
                    current_base_dev.matrix, cand_dev.matrix, "negative",
                    harm_margin=material)
                if dense else _mcnemar_one_sided(dev_c, dev_b)),
        }
        if dense:
            gate_stats["p_estimator"] = "paired_mean_bootstrap"
            gate_stats["material_delta"] = material
        if repair_flips is not None:
            gate_stats["repair_flips"] = list(repair_flips)
            gate_stats["p_rep"] = (
                _paired_mean_bootstrap_p(
                    base_rep.matrix, rep.matrix, "positive",
                    tasks=repair_tasks)
                if dense else
                _mcnemar_one_sided(repair_flips[0], repair_flips[1]))
        if self.a.round_gate == "always":
            # Always-Accept control: commit every well-formed candidate, whatever
            # the evidence says. Used only to measure what the gate is buying.
            accept = True
        elif self.a.round_gate == "strict":
            accept = v.accept and repair_ok
        elif self.a.round_gate in ("calibrated", "progressive"):
            # Sign tests instead of tuned counts.  Repair (or independently
            # dev) must be significantly positive, and dev must not be
            # significantly harmful.  Dev at this size cannot certify effect
            # sizes -- an unchanged package swings 10pp -- so it holds veto
            # power over harm only; the effect claim belongs to the frozen
            # set and to evidence accumulated across rounds.
            p_rep = gate_stats.get("p_rep", 1.0)
            accept = (repair_ok and
                      (p_rep < 0.05 or gate_stats["p_dev_pos"] < 0.05) and
                      gate_stats["p_harm"] >= 0.05)
        else:  # relaxed: repair + net>0 + bounded regressions; CI reported not required
            accept = repair_ok and net > 1e-9 and v.n_regressed <= self.a.reg_cap
        counter_names = set(current_base_dev.fingerprint) | set(cand_dev.fingerprint)
        mechanism_delta = {
            name: cand_dev.fingerprint.get(name, 0) -
            current_base_dev.fingerprint.get(name, 0)
            for name in sorted(counter_names)
        }
        # `material` is 0.0 on binary benchmarks, so this stays "any drop".
        regressed_tasks = [
            task for task in self.dev_ids
            if statistics.mean(current_base_dev.matrix[task]) -
            statistics.mean(cand_dev.matrix[task]) > material
        ]
        regressed_mechanism_delta: dict[str, int] = {}
        for task in regressed_tasks:
            base_fp = current_base_dev.fingerprint_by_task.get(task, {})
            cand_fp = cand_dev.fingerprint_by_task.get(task, {})
            for name in set(base_fp) | set(cand_fp):
                regressed_mechanism_delta[name] = (
                    regressed_mechanism_delta.get(name, 0) +
                    cand_fp.get(name, 0) - base_fp.get(name, 0)
                )
        verdict = {"round": rnd, "accept": accept, "gate_stage": "held-out",
                   "dev_evaluated": True, "net_pp": round(net, 1),
                   "ci": [round(x * 100, 1) for x in v.ci], "up": v.n_improved,
                   "dn": v.n_regressed, "repair_base": rb, "repair_cand": rc,
                   "repair_ok": repair_ok, "repair_changes": repair_changes,
                   "gate_stats": gate_stats,
                   "held_out_damage": damage,
                   "cand_dev_fp": cand_dev.fingerprint,
                   "base_dev_fp": current_base_dev.fingerprint,
                   "cand_dev_fp_by_task": cand_dev.fingerprint_by_task,
                   "base_dev_fp_by_task": current_base_dev.fingerprint_by_task,
                   "base_dev_matrix": current_base_dev.matrix,
                   "mechanism_delta": mechanism_delta,
                   "regressed_mechanism_delta": regressed_mechanism_delta,
                   "evidence_status": "supported" if accept else "inconclusive",
                   "cand_dev_matrix": cand_dev.matrix,
                   "cand_dev_seeds": cand_dev.seeds,
                   "cand_dev_benchmark_git_commit": cand_dev.benchmark_git_commit,
                   "cand_dev_benchmark_input_sha256": cand_dev.benchmark_input_sha256,
                   "candidate_tree_sha256": candidate_tree_sha256,
                   "repair_task_count": len(repair_tasks),
                   "working_base_provisional_round": (
                       self.state["provisional"]["round"]
                       if self.state.get("provisional") else None
                   )}
        bare_reference = self.state.get("bare_dev_reference")
        if bare_reference is not None:
            self._require_paired_seed_maps(
                bare_reference["seeds"], cand_dev.seeds, "bare/candidate utility report"
            )
            self._require_same_benchmark_commit(
                bare_reference["benchmark_git_commit"],
                cand_dev.benchmark_git_commit,
                "bare/candidate utility report",
            )
            self._require_same_benchmark_input(
                bare_reference["benchmark_input_sha256"],
                cand_dev.benchmark_input_sha256,
                "bare/candidate utility report",
            )
            utility = held_out_paired_gate(
                bare_reference["matrix"], cand_dev.matrix, reg_cap=99,
                material=material,
            )
            verdict.update({
                "vs_bare_net_pp": round(utility.net * 100, 1),
                "vs_bare_ci": [round(value * 100, 1) for value in utility.ci],
                "vs_bare_up": utility.n_improved,
                "vs_bare_dn": utility.n_regressed,
            })
        if self.a.round_gate in ("calibrated", "progressive"):
            rep_s = ("rep b/c=%d/%d p=%.3f " % (
                gate_stats["repair_flips"][0], gate_stats["repair_flips"][1],
                gate_stats["p_rep"]) if "p_rep" in gate_stats else "")
            self.log(
                f"GATE-CAL r{rnd}: {rep_s}dev b/c={dev_b}/{dev_c} "
                f"p_pos={gate_stats['p_dev_pos']:.3f} "
                f"p_harm={gate_stats['p_harm']:.3f}"
            )
        self.log(f"GATE r{rnd}: net={net:+.1f}pp CI[{verdict['ci'][0]:+.1f},{verdict['ci'][1]:+.1f}] "
                 f"up={v.n_improved} dn={v.n_regressed} repair {rb}->{rc} "
                 f"=> {'ACCEPT' if accept else 'REJECT'}")
        if bare_reference is not None:
            self.log(
                f"UTILITY r{rnd}: candidate vs bare={verdict['vs_bare_net_pp']:+.1f}pp "
                f"CI[{verdict['vs_bare_ci'][0]:+.1f},{verdict['vs_bare_ci'][1]:+.1f}]"
            )
        return verdict

    def commit(self, rnd: int, plan: dict | None, verdict: dict,
               finalization: dict, summary_override: str | None = None):
        self.integrity()
        version_dir = self.versions_dir / f"M{rnd}"
        assert_mutation_paths_safe([version_dir])
        if version_dir.exists():
            raise RuntimeError(f"refusing to overwrite immutable package version: {version_dir}")
        self._assert_tree_unlinked(self.cand_dir)
        summary = summary_override or (
            "initialize M0" if plan is None else
            "; ".join(f"{c['component']}:{','.join(f['action'] + ':' + str(f.get('card_id') or f.get('deliverer') or f.get('checker') or f.get('field') or '')[:30] for f in c.get('fixes', []))}"
                      for c in plan["clusters"]
                      if c["component"] not in {"CEILING", "MODEL", "HARNESS", "BENCHMARK"}))
        changelog_entry = {"round": rnd, "summary": summary,
                           "net_pp": verdict["net_pp"]}
        digest = self._package_digest(self.cand_dir)
        evaluated_digest = verdict.get("candidate_tree_sha256")
        if not evaluated_digest or evaluated_digest != digest:
            raise RuntimeError(
                "candidate bytes no longer match the package admitted by evaluation"
            )
        candidate_seeds = verdict.get("cand_dev_seeds")
        candidate_commit = verdict.get("cand_dev_benchmark_git_commit")
        candidate_input = verdict.get("cand_dev_benchmark_input_sha256")
        bare_reference = verdict.get(
            "bare_dev_reference", self.state.get("bare_dev_reference"),
        )
        if not isinstance(candidate_seeds, dict):
            raise RuntimeError("candidate verdict lacks paired dev seed proof")
        if not re.fullmatch(r"[0-9a-f]{40}", str(candidate_commit or "")):
            raise RuntimeError("candidate verdict lacks benchmark commit proof")
        if not re.fullmatch(r"[0-9a-f]{64}", str(candidate_input or "")):
            raise RuntimeError("candidate verdict lacks benchmark input proof")
        temp_name = f".M{rnd}.{uuid.uuid4().hex}.tmp"
        temp_dir = self.versions_dir / temp_name
        journal_path = self.run_dir / f"commit_M{rnd}_{uuid.uuid4().hex[:10]}.json"
        journal = {"status": "prepared", "round": rnd, "version": f"M{rnd}",
                   "temp": temp_name, "tree_sha256": digest,
                   "best_dev": verdict["cand_dev_matrix"],
                   "best_dev_seeds": candidate_seeds,
                   "best_dev_benchmark_git_commit": candidate_commit,
                   "best_dev_benchmark_input_sha256": candidate_input,
                   "bare_dev_reference": bare_reference,
                   "changelog_entry": changelog_entry,
                   "finalization": finalization, "created_at": _now()}
        self._write_json_atomic(journal_path, journal)
        shutil.copytree(self.cand_dir, temp_dir)
        if self._package_digest(temp_dir) != digest:
            raise RuntimeError(f"version copy verification failed: {temp_dir}")
        temp_dir.replace(version_dir)
        self.state["version_hashes"][version_dir.name] = digest
        self.state["best"] = str(version_dir)
        self.state["best_dev"] = verdict["cand_dev_matrix"]
        self.state["best_dev_seeds"] = candidate_seeds
        self.state["best_dev_benchmark_git_commit"] = candidate_commit
        self.state["best_dev_benchmark_input_sha256"] = candidate_input
        self.state["bare_dev_reference"] = bare_reference
        self.state["changelog"].append(changelog_entry)
        # A strict accept settles any provisional lineage: the committed
        # version is the cumulative package certified against the old best.
        if self.state.get("provisional"):
            self.log(
                f"PROVISIONAL: settled by strict accept at round {rnd}; "
                "committed lineage advances"
            )
            self.state["provisional"] = None
        self.save_state()
        journal["status"] = "package_committed"
        self._write_json_atomic(journal_path, journal)
        self._finalize_round(rnd, finalization)
        journal["status"] = "complete"
        journal["completed_at"] = _now()
        self._write_json_atomic(journal_path, journal)
        self.integrity()

    def _checkpoint_round(self, rnd: int, outcome: str) -> None:
        self._finalize_round(rnd, {"outcome": outcome})

    # ---------- rounds ----------
    def neutral(self):
        """Create a deterministic no-delivery/no-checker M0 inside this run."""
        rnd = 0
        rdir = self.run_dir / "round_0"
        rdir.mkdir(parents=True, exist_ok=True)
        self._prepare_candidate(rnd)
        manifest = f"""name: {self.profile.package_prefix}_{self.domain}_ma_neutral

delivery: []
checkers: []

board:
  status_board: false
  notice_in_wm: never
"""
        (self.cand_dir / "manifest.yaml").write_text(manifest, encoding="utf-8")
        lint = subprocess.run([PY, str(HERE / "lint.py"), "--pkg",
                               str(self.cand_dir.relative_to(ROOT)), "--domain", self.domain,
                               "--benchmark", getattr(self, "benchmark", "tau2")],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=300)
        if lint.returncode != 0:
            raise RuntimeError(f"deterministic neutral M0 failed lint:\n{lint.stdout}\n{lint.stderr}")
        bare_dev = (
            self._reuse_initialization_eval(
                None, self.dev_ids, self.a.k, tag="r0_bare"
            ) or
            self.evaluate(None, self.dev_ids, self.a.k, tag="r0_bare")
        )
        cand_dev = (
            self._reuse_initialization_eval(
                str(self.cand_dir), self.dev_ids, self.a.k, tag="r0_neutral"
            ) or
            self.evaluate(
                str(self.cand_dir), self.dev_ids, self.a.k, tag="r0_neutral"
            )
        )
        self._require_paired_seed_maps(
            bare_dev.seeds, cand_dev.seeds, "bare/neutral initialization gate"
        )
        self._require_same_benchmark_commit(
            bare_dev.benchmark_git_commit,
            cand_dev.benchmark_git_commit,
            "bare/neutral initialization gate",
        )
        self._require_same_benchmark_input(
            bare_dev.benchmark_input_sha256,
            cand_dev.benchmark_input_sha256,
            "bare/neutral initialization gate",
        )
        comparison = held_out_paired_gate(bare_dev.matrix, cand_dev.matrix, reg_cap=99)
        net = comparison.net * 100
        verdict = {
            "round": 0, "accept": True, "net_pp": round(net, 1),
            "ci": [round(x * 100, 1) for x in comparison.ci],
            "up": comparison.n_improved, "dn": comparison.n_regressed,
            "cand_dev_matrix": cand_dev.matrix,
            "cand_dev_seeds": cand_dev.seeds,
            "cand_dev_benchmark_git_commit": cand_dev.benchmark_git_commit,
            "cand_dev_benchmark_input_sha256": cand_dev.benchmark_input_sha256,
            "candidate_tree_sha256": cand_dev.artifact.get("package_tree_sha256"),
            "bare_dev_reference": {
                "matrix": bare_dev.matrix,
                "seeds": bare_dev.seeds,
                "benchmark_git_commit": bare_dev.benchmark_git_commit,
                "benchmark_input_sha256": bare_dev.benchmark_input_sha256,
            },
        }
        self.log(f"NEUTRAL M0 vs bare: net={net:+.1f}pp "
                 f"CI[{verdict['ci'][0]:+.1f},{verdict['ci'][1]:+.1f}]")
        neutral_ledger = {"round": 0, "phase": "neutral", "verdict": {
            key: value for key, value in verdict.items() if key != "cand_dev_matrix"
        }}
        self.commit(0, None, verdict,
                    finalization={"outcome": "neutral", "ledger": neutral_ledger},
                    summary_override="deterministic neutral M0")
        self.power_gate("r0_neutral")

    @staticmethod
    def _power_gate_table(cells: int) -> list[dict]:
        """Minimal detectable effect of the cell-level one-sided McNemar gate.

        For each tolerated regression count c, the smallest improvement count
        b with P(X >= b | b+c, 1/2) < 0.05, expressed as net cells and pp of
        the given cell budget."""
        rows = []
        for c in (0, 1, 2):
            b = c + 1
            while _mcnemar_one_sided(b, c) >= 0.05:
                b += 1
            rows.append({
                "regressions": c,
                "improvements_needed": b,
                "net_cells": b - c,
                "mde_pp": (
                    round(100.0 * (b - c) / cells, 1) if cells else None
                ),
            })
        return rows

    def _dense_power_gate(self, stage: str, mode: str, k: int,
                          cap: float) -> None:
        """Power gate for a dense-reward campaign.

        The binary table below is built from `_mcnemar_one_sided`, the
        cell-level sign test — an estimator a dense campaign never runs.  Its
        MDE is a count of discordant CELLS, so it improves with k, and at (say)
        4 dev tasks x k=12 it reports a comfortable 10.4pp while the estimator
        the campaign actually uses, `_paired_mean_bootstrap_p`, returns exactly
        1.0 for a +100pp effect: it resamples TASKS, and below
        DENSE_BOOTSTRAP_MIN_CLUSTERS of them it declines to certify at all.
        Printing the binary number there green-lights a design that cannot
        certify anything.

        So the dense branch reports the constraint that actually binds — the
        number of dev TASKS — and, because that bound is arithmetic rather
        than an estimate, it is the only one `enforce` stops on.  The
        simulated MDE alongside it rests on an assumed per-trial noise level
        this run has not measured, so it is reported and warned on, never
        enforced.
        """
        tasks = len(self.dev_ids)
        dev_cells = tasks * k
        # The two dense failure modes are different faults and get different
        # sentences: too few TASKS is a power failure (nothing is detectable),
        # k=1 is a validity failure (the floor above was calibrated at k >= 2
        # and at k=1 the same five clusters false-accept a true null 9.3% of
        # the time, so a p < 0.05 printed there is not the 5% claim it looks
        # like).  Neither is fixed by the other's remedy.
        certifiable = tasks >= DENSE_BOOTSTRAP_MIN_CLUSTERS
        calibrated = k >= DENSE_BOOTSTRAP_MIN_TRIALS
        top_pp = round(100.0 * DENSE_POWER_SIM_GRID[-1], 1)
        mde = _dense_mde_pp(tasks, k) if certifiable else None
        report = {
            "stage": stage,
            "mode": mode,
            "reward_scale": "dense",
            "estimator": "paired_mean_bootstrap",
            "dev_tasks": tasks,
            "k": k,
            "dev_cells": dev_cells,
            "mde_cap_pp": cap,
            "min_dev_tasks": DENSE_BOOTSTRAP_MIN_CLUSTERS,
            "min_k": DENSE_BOOTSTRAP_MIN_TRIALS,
            "certifiable": certifiable,
            "calibrated": calibrated,
            "dev_mde_pp": mde,
            "dev_mde_assumptions": {
                "trial_sd": DENSE_POWER_SIM_TRIAL_SD,
                "power": DENSE_POWER_SIM_POWER,
                "replicates": DENSE_POWER_SIM_REPLICATES,
                "grid_pp": [round(100.0 * value, 1)
                            for value in DENSE_POWER_SIM_GRID],
                "enforced": False,
            },
            # p_rep is the same bootstrap restricted to the repair targets, so
            # the repair screen carries the same cluster floor; the binary cell
            # table (repair_mde_8_targets) does not apply to it either.
            "repair_min_target_tasks": DENSE_BOOTSTRAP_MIN_CLUSTERS,
        }
        self._write_json_atomic(
            self.run_dir / f"power_gate_{stage}.json", report
        )
        if not certifiable:
            detectable = (
                f"nothing at any effect size ({tasks} < "
                f"{DENSE_BOOTSTRAP_MIN_CLUSTERS} task clusters)"
            )
        elif mde is None:
            detectable = f">{top_pp}pp at trial sd {DENSE_POWER_SIM_TRIAL_SD}"
        else:
            detectable = (
                f"simulated MDE={mde}pp at trial sd {DENSE_POWER_SIM_TRIAL_SD}"
            )
        self.log(
            f"POWER-GATE[{stage}] dense dev {tasks}x{k}={dev_cells} cells: "
            f"{tasks} task cluster(s) (min {DENSE_BOOTSTRAP_MIN_CLUSTERS}), "
            f"detectable={detectable}, cap={cap}pp, mode={mode}"
        )
        blocking = []
        if not certifiable:
            blocking.append(
                f"dense dev gate cannot certify any effect: the paired-mean "
                f"bootstrap resamples TASKS and declines below "
                f"{DENSE_BOOTSTRAP_MIN_CLUSTERS} clusters, so at {tasks} dev "
                f"task(s) no effect of any size -- not even +100pp -- reaches "
                f"p<0.05. Cells are not the constraint: raising --k adds cells "
                f"and changes nothing. Extend the dev split to >= "
                f"{DENSE_BOOTSTRAP_MIN_CLUSTERS} tasks before campaigning "
                f"(--power-gate off/warn to override)."
            )
        if not calibrated:
            blocking.append(
                f"dense dev gate is not calibrated at k={k}: the "
                f"{DENSE_BOOTSTRAP_MIN_CLUSTERS}-cluster floor was measured at "
                f"k >= {DENSE_BOOTSTRAP_MIN_TRIALS}, and with one trial per task "
                f"there is no second draw to resample inside a cluster (measured "
                f"false-accept 9.3% at 5 tasks, not the nominal 5%). Raise --k to "
                f">= {DENSE_BOOTSTRAP_MIN_TRIALS} before campaigning "
                f"(--power-gate off/warn to override)."
            )
        for msg in blocking:
            if mode == "enforce":
                raise RuntimeError(f"POWER-GATE: {msg}")
            self.log(f"POWER-GATE WARNING: {msg}")
        if blocking or (mde is not None and mde <= cap):
            return
        # Reported, not enforced: unlike the cluster arithmetic above, the
        # number below assumes a per-trial noise level instead of measuring
        # this run's.
        shown = f">{top_pp}" if mde is None else f"{mde}"
        self.log(
            f"POWER-GATE WARNING: dense dev gate simulated MDE {shown}pp "
            f"(assuming per-trial sd {DENSE_POWER_SIM_TRIAL_SD}, "
            f"{int(100 * DENSE_POWER_SIM_POWER)}% power) exceeds cap {cap}pp: a "
            f"realistic single-round improvement is unlikely to pass this "
            f"instrument. Extend the dev split or raise --k before campaigning "
            f"(estimate only, never enforced -- the noise level is assumed)."
        )

    def power_gate(self, stage: str) -> None:
        """Refuse (or warn) before burning rounds on an instrument whose
        minimal detectable effect exceeds any realistic single-round gain.

        Runs once r0 fixes the dev split and k.  The TB/banking postmortems
        measured single-round candidate effects of +4..11pp against gates
        whose clean MDE was +18..29pp — a shape this check now reports before
        the first evolution round instead of after three rejections.

        The table below is the CELL-level sign test's MDE, which is the gate a
        binary campaign runs.  A dense campaign runs a task-clustered bootstrap
        instead, so it branches to `_dense_power_gate` before any of this."""
        mode = str(getattr(self.a, "power_gate", "warn") or "warn")
        if mode == "off":
            return
        k = int(self.a.k)
        cap = float(getattr(self.a, "power_gate_mde_cap", 12.0))
        if self._dense_rewards():
            self._dense_power_gate(stage, mode, k, cap)
            return
        dev_cells = len(self.dev_ids) * k
        dev_rows = self._power_gate_table(dev_cells)
        clean = dev_rows[0]["mde_pp"]
        report = {
            "stage": stage,
            "mode": mode,
            "dev_tasks": len(self.dev_ids),
            "k": k,
            "dev_cells": dev_cells,
            "mde_cap_pp": cap,
            "dev_mde": dev_rows,
            "repair_mde_8_targets": self._power_gate_table(8 * k),
        }
        self._write_json_atomic(
            self.run_dir / f"power_gate_{stage}.json", report
        )
        self.log(
            f"POWER-GATE[{stage}] dev {len(self.dev_ids)}x{k}={dev_cells} "
            f"cells: clean MDE={clean}pp, 1-regression="
            f"{dev_rows[1]['mde_pp']}pp, cap={cap}pp, mode={mode}"
        )
        if clean is None or clean <= cap:
            return
        needed_k = math.ceil(
            100.0 * dev_rows[0]["net_cells"] / (cap * max(1, len(self.dev_ids)))
        )
        msg = (
            f"dev gate clean MDE {clean}pp exceeds cap {cap}pp: a genuine "
            f"single-round improvement cannot pass this instrument. Raise "
            f"--k to >= {needed_k} or extend the dev split before "
            f"campaigning (--power-gate off/warn to override)."
        )
        if mode == "enforce":
            raise RuntimeError(f"POWER-GATE: {msg}")
        self.log(f"POWER-GATE WARNING: {msg}")

    def initialize_from_package(self):
        """Copy an explicit read-only seed into this run and commit it as M0."""
        source = Path(self.a.base)
        if not source.is_absolute():
            source = ROOT / source
        source = source.resolve()
        if not source.is_dir():
            raise RuntimeError(f"explicit base package is missing: {source}")
        verify_champions()
        self._prepare_candidate(0, source)
        lint = subprocess.run([PY, str(HERE / "lint.py"), "--pkg",
                               str(self.cand_dir.relative_to(ROOT)), "--domain", self.domain,
                               "--benchmark", getattr(self, "benchmark", "tau2")],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=300)
        leaks = ma_sanitize.scan_package(self.cand_dir, self.gold)
        if lint.returncode != 0 or leaks:
            raise RuntimeError(
                f"explicit base failed referee checks (lint={lint.returncode}, leaks={leaks}):\n"
                f"{lint.stdout}\n{lint.stderr}"
            )
        dev = self.evaluate(str(self.cand_dir), self.dev_ids, self.a.k, tag="r0_base")
        mean = statistics.mean(statistics.mean(row) for row in dev.matrix.values())
        verdict = {"round": 0, "accept": True, "net_pp": 0.0,
                   "ci": [0.0, 0.0], "up": 0, "dn": 0,
                   "cand_dev_matrix": dev.matrix,
                   "cand_dev_seeds": dev.seeds,
                   "cand_dev_benchmark_git_commit": dev.benchmark_git_commit,
                   "cand_dev_benchmark_input_sha256": dev.benchmark_input_sha256,
                   "candidate_tree_sha256": dev.artifact.get("package_tree_sha256")}
        base_ledger = {"round": 0, "phase": "explicit-base", "source": str(source),
                       "source_tree_sha256": self._package_digest(source),
                       "dev_mean": mean}
        self.commit(
            0, None, verdict,
            finalization={"outcome": "explicit-base", "ledger": base_ledger},
            summary_override=f"imported read-only base {source.name} ({mean*100:.1f}% dev)",
        )
        self.power_gate("r0_base")

    def bootstrap(self):
        rdir = self.run_dir / "round_0"
        rdir.mkdir(parents=True, exist_ok=True)
        self._prepare_candidate(0)
        (rdir / "schema_rules.md").write_text(self.SCHEMA_RULES, encoding="utf-8")
        protocol = self._formal_protocol("package", rdir / "bootstrap_protocol.md")
        cand_rel = self.cand_dir.relative_to(ROOT)
        rel = rdir.relative_to(ROOT)
        policy_path = self.profile.policy_doc_path(self.domain)
        profile = ("No historical task profile is supplied. Derive only a minimal "
                   "runtime skeleton from the public policy; do not use prior task "
                   "statistics, champion behavior, or held-out outcomes.")
        prompt = f"""You are the Meta-Agent for the Recuris framework (bootstrap, round 0).
Instantiate the initial Skill Memory package M0 for the `{self.domain}` domain:
write `{cand_rel}/manifest.yaml` as a minimal machine skeleton. Add NO cards yet;
cards are introduced only from current evolve-split evidence in later rounds.

Read `{rel}/schema_rules.md` for the exact manifest format (a linter enforces it).
Read `{self.profile.policy_doc_display(self.domain)}` as the only
domain-specific source. Do not consult any historical profile or champion.
Then write manifest.yaml (name: {self.profile.package_prefix}_{self.domain}_ma_cand). Edit ONLY inside
`{cand_rel}`. Keep it minimal: do not add delivery without cards, and add a
checker/board/gate only when the public policy itself establishes a universal
state invariant. End the session when manifest.yaml is written.

## Mandatory package protocol
{protocol}

## Formal initialization constraint
{profile}"""
        for attempt in (1, 2):
            ok = self.cc_session(f"r0_boot{attempt}", prompt, P_TOOLS,
                                 self.a.cc_timeout, [self.cand_dir],
                                 [rdir / "schema_rules.md", self.cand_dir, policy_path])
            lint = subprocess.run([PY, str(HERE / "lint.py"), "--pkg",
                                   str(cand_rel), "--domain", self.domain,
                                   "--benchmark", getattr(self, "benchmark", "tau2")],
                                  cwd=str(ROOT),
                                  capture_output=True, text=True, timeout=300)
            if ok and lint.returncode == 0:
                break
            prompt += ("\n\n## The linter REJECTED your manifest — fix exactly this:\n```\n"
                       + lint.stdout[-2000:] + "\n```")
        else:
            raise RuntimeError("bootstrap failed twice")

        bare_dev = self.evaluate(None, self.dev_ids, self.a.k, tag="r0_bare")
        cand_dev = self.evaluate(str(self.cand_dir), self.dev_ids, self.a.k, tag="r0_dev")
        self._require_paired_seed_maps(
            bare_dev.seeds, cand_dev.seeds, "bare/bootstrap initialization gate"
        )
        self._require_same_benchmark_commit(
            bare_dev.benchmark_git_commit,
            cand_dev.benchmark_git_commit,
            "bare/bootstrap initialization gate",
        )
        self._require_same_benchmark_input(
            bare_dev.benchmark_input_sha256,
            cand_dev.benchmark_input_sha256,
            "bare/bootstrap initialization gate",
        )
        v = held_out_paired_gate(bare_dev.matrix, cand_dev.matrix, reg_cap=99)
        net = v.net * 100
        self.log(f"BOOTSTRAP: M0 vs bare on dev: net={net:+.1f}pp CI"
                 f"[{v.ci[0]*100:+.1f},{v.ci[1]*100:+.1f}] up={v.n_improved} dn={v.n_regressed}")
        # M0 is initialization, not an evolution claim: commit unless catastrophic
        # (a flawed M0 is fine — repairing it IS the evolution story; the rounds
        # can diagnose regressions and remove harmful components)
        accept = net >= -20.0
        verdict = {"round": 0, "accept": accept, "net_pp": round(net, 1),
                   "ci": [round(x*100, 1) for x in v.ci], "up": v.n_improved,
                   "dn": v.n_regressed, "cand_dev_matrix": cand_dev.matrix,
                   "cand_dev_seeds": cand_dev.seeds,
                   "cand_dev_benchmark_git_commit": cand_dev.benchmark_git_commit,
                   "cand_dev_benchmark_input_sha256": cand_dev.benchmark_input_sha256,
                   "candidate_tree_sha256": cand_dev.artifact.get("package_tree_sha256")}
        verdict["bare_dev_reference"] = {
            "matrix": bare_dev.matrix,
            "seeds": bare_dev.seeds,
            "benchmark_git_commit": bare_dev.benchmark_git_commit,
            "benchmark_input_sha256": bare_dev.benchmark_input_sha256,
        }
        if not accept:
            raise RuntimeError("bootstrap M0 clearly worse than bare — inspect round_0")
        bootstrap_ledger = {"round": 0, "phase": "bootstrap", "verdict": {
            k: v_ for k, v_ in verdict.items() if k != "cand_dev_matrix"}}
        self.commit(0, None, verdict,
                    finalization={"outcome": "bootstrap", "ledger": bootstrap_ledger})

    def round(self, rnd: int):
        rdir = self.run_dir / f"round_{rnd}"
        rdir.mkdir(parents=True, exist_ok=True)
        self.integrity()
        self._assert_generation_profile_resume_safe(rnd)

        if self.replay_source_run:
            if rnd != self.replay_source_round:
                raise RuntimeError("replay schema v1 may execute only frozen round 1")
            train_res = getattr(self, "_replay_train_result", None)
            if train_res is None:
                raise RuntimeError("prepare_replay must run before the replay round")
            failing = set(train_res.failing)
            self.state["train_history"][str(rnd)] = copy.deepcopy(train_res.matrix)
            self.save_state()
            self.log(
                f"eval[r{rnd}_train] REPLAYED immutable evidence from "
                f"{self.replay_source_run}; downstream simulations charged=0"
            )
            if (getattr(self, "generation_profile_spec", None) is None and
                    hasattr(train_res, "trial_evidence") and hasattr(self, "a")):
                # Keep the sealed source evidence for provenance, but derive the
                # model-visible facts from the same frozen trajectories under
                # the current benchmark adapter. This prevents retired causal
                # labels from leaking into a new autonomous diagnosis.
                reparsed_root = rdir / "reparsed_evidence"
                reparsed_root.mkdir(parents=True, exist_ok=True)
                failing = self.write_evidence(rnd, train_res, reparsed_root)
        else:
            # An unchanged package over unchanged tasks is the same
            # evaluation whatever the round is called, so it is reparsed rather
            # than re-run. The gate's arms are excluded from this: they are
            # re-measured together on purpose.
            train_res = self.evaluate(self._working_base_package(),
                                      self.train_ids, self.a.k,
                                      tag=f"r{rnd}_train", tag_must_match=False)
            # regression suspects vs previous round's train numbers
            suspects = []
            prev = self.state["train_history"].get(str(rnd - 1))
            if prev:
                last = (self.state["changelog"][-1]["summary"]
                        if self.state["changelog"] else "nothing")
                for t, row in train_res.matrix.items():
                    if (t in prev and statistics.mean(row) <
                            statistics.mean(prev[t]) - 0.34):
                        suspects.append(
                            f"task {t}: {statistics.mean(prev[t])*100:.0f}% -> "
                            f"{statistics.mean(row)*100:.0f}% "
                            f"(last change: {last})"
                        )
            self.state["train_history"][str(rnd)] = train_res.matrix
            self._round_regressions = self._write_regression_evidence(
                rnd, rdir, train_res)

            if not train_res.failing:
                self.log("no failing train tasks -> converged")
                self._checkpoint_round(rnd, "converged")
                return "converged"

            (rdir / "state.md").write_text(
                self.state_md(suspects), encoding="utf-8"
            )
            failing = self.write_evidence(rnd, train_res, rdir)

        generation_context = self._materialize_generation_profile(
            rnd, rdir, train_res
        )
        if generation_context is None:
            # Autonomous diagnosis owns causal attribution. Validators enforce
            # evidence scope and package legality, not symptom-to-owner rules.
            forced_owners = {}
            forced_opportunities = {}
        else:
            failing = set(generation_context["failing_tasks"])
            forced_owners = copy.deepcopy(
                generation_context["forced_owners"]
            )
            forced_opportunities = copy.deepcopy(
                generation_context["forced_opportunities"]
            )
        workflow = getattr(self, "meta_workflow", "split")
        try:
            if workflow in {"continuous", "hierarchical"}:
                if generation_context is not None:
                    raise RuntimeError(
                        f"{workflow} workflow does not support locked "
                        "generation profiles"
                    )
                self._prepare_candidate(rnd, self._working_base_package())
                plan = self.phase_continuous(
                    rnd, rdir, failing, forced_owners=forced_owners,
                    forced_opportunities=forced_opportunities,
                )
            elif generation_context is None:
                plan = self.phase_d(
                    rnd, rdir, failing, forced_owners=forced_owners,
                    forced_opportunities=forced_opportunities,
                )
                if plan is not None and getattr(self, "recursive_review", False):
                    plan = self.phase_d_review(
                        rnd, rdir, plan, failing,
                        forced_owners=forced_owners,
                        forced_opportunities=forced_opportunities,
                    )
            else:
                plan = self.phase_d(
                    rnd, rdir, failing, forced_owners=forced_owners,
                    forced_opportunities=forced_opportunities,
                    generation_context=generation_context,
                )
        except ProfileArmIneligible as exc:
            return self._finalize_profile_arm_ineligible(rnd, exc)
        except MetaAgentSessionFailure as exc:
            self._finalize_round(rnd, {
                "outcome": exc.outcome,
                "ledger": {
                    "round": rnd,
                    "phase": exc.phase,
                    "result": "Meta-Agent sessions failed before a valid plan",
                    "session_failures": exc.sessions,
                },
            })
            return exc.outcome
        if plan is None:
            diagnosis = getattr(self, "last_diagnosis_outcome", {
                "kind": "invalid-plan",
                "validator_errors": [],
                "session_failures": [],
            })
            self._finalize_round(rnd, {
                "outcome": "invalid-plan",
                "ledger": {
                    "round": rnd,
                    "phase": workflow if workflow in {
                        "continuous", "hierarchical"} else "D",
                    "result": "validator rejected all completed plans",
                    "diagnosis": diagnosis,
                },
                "lesson": {
                    "round": rnd,
                    "verdict": "INVALID-PLAN",
                    "detail": "Completed Phase D output failed plan validation",
                    "validator_errors": diagnosis.get("validator_errors", []),
                },
            })
            return "invalid-plan"

        nonpatchable = {"CEILING", "MODEL", "HARNESS", "BENCHMARK"}
        ceilings = [c["cluster_id"] for c in plan["clusters"]
                    if c["component"] in nonpatchable]
        actionable = [c for c in plan["clusters"]
                      if c["component"] not in nonpatchable]
        if not actionable:
            self._finalize_round(rnd, {
                "outcome": "all-ceiling",
                "ledger": {"round": rnd,
                           "phase": workflow if workflow in {
                               "continuous", "hierarchical"} else "D",
                           "result": "all ceilings",
                           "plan": plan},
            })
            return "all-ceiling"

        if workflow == "split":
            self._prepare_candidate(rnd, self._working_base_package())
        self._apply_planned_deletions(plan)

        self.last_validation_report = None
        try:
            if workflow in {"continuous", "hierarchical"}:
                patch_ok = self._validate_continuous_candidate(rnd, rdir, plan)
            elif generation_context is None:
                patch_ok = self.phase_p(rnd, rdir, plan)
            else:
                patch_ok = self.phase_p(
                    rnd, rdir, plan, generation_context=generation_context
                )
        except ProfileArmIneligible as exc:
            return self._finalize_profile_arm_ineligible(rnd, exc)
        if not patch_ok:
            self._finalize_round(rnd, {
                "outcome": "patch-failed",
                "ledger": {"round": rnd,
                           "phase": workflow if workflow in {
                               "continuous", "hierarchical"} else "P",
                           "result": "patch failed",
                           "plan": plan},
                "lesson": {"round": rnd, "verdict": "PATCH-FAILED", "plan": plan,
                           "validation_referee": getattr(
                               self, "last_validation_report", None)},
            })
            return "patch-failed"

        repair_tasks = sorted({str(t) for c in actionable for t in c["evidence_tasks"]},
                              key=_task_sort_key)[:8]
        if self.generate_only:
            if generation_context is None:
                return self.freeze_generated_candidate(
                    rnd, plan, repair_tasks, train_res
                )
            return self.freeze_generated_candidate(
                rnd, plan, repair_tasks, train_res,
                generation_context=generation_context,
            )
        verdict = self.gate(rnd, plan, repair_tasks, train_res)

        private_gate_fields = {
            "base_dev_matrix", "cand_dev_matrix", "base_dev_fp", "cand_dev_fp",
            "base_dev_fp_by_task", "cand_dev_fp_by_task",
        }
        led_verdict = {k: v for k, v in verdict.items()
                       if k not in private_gate_fields}
        outcome = "accepted" if verdict["accept"] else "rejected"
        plan_bundle_sha256 = hashlib.sha256(
            json.dumps(plan, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        ledger_record = {"round": rnd, "phase": "gate", "verdict": led_verdict,
                         "plan_bundle_sha256": plan_bundle_sha256,
                         "plan_summary": [
                             {"cluster": c["cluster_id"], "component": c["component"],
                              "tasks": c["evidence_tasks"],
                              "fixes": [f.get("action") for f in c.get("fixes", [])]}
                             for c in plan["clusters"]],
                         "ceilings": ceilings}
        lesson_record = {"round": rnd,
                         "verdict": "ACCEPT" if verdict["accept"] else "REJECT",
                         "gate": led_verdict,
                         "causal_interpretation": (
                             "supported" if verdict["accept"] else
                             "inconclusive; rejection protects best but does not prove "
                             "which individual fix caused the outcome"
                         ),
                         "plan": [{"component": c["component"],
                                   "fixes": c.get("fixes", []),
                                   "tasks": c["evidence_tasks"]}
                                  for c in actionable]}
        # A compound aggregate rejection is not a causal ablation.  Do not
        # permanently ban each constituent fix; future rounds may narrow its
        # activation contract.  Permanent blocks require an explicit harmful
        # unit-off result, which this gate does not yet claim.
        blocked_additions = []
        finalization = {"outcome": outcome, "ledger": ledger_record,
                        "lesson": lesson_record,
                        "blocked_additions": blocked_additions}
        self._write_round_review(rnd, plan, led_verdict)
        if verdict["accept"]:
            self.commit(rnd, plan, verdict, finalization=finalization)
        else:
            self._handle_provisional_after_reject(rnd, verdict, finalization)
            self._finalize_round(rnd, finalization)
        return outcome

    def _handle_provisional_after_reject(
            self, rnd: int, verdict: dict, finalization: dict) -> None:
        """Provisional working-base lifecycle on a gate rejection.

        Non-progressive modes: an active provisional base expires unless its
        follow-up round earned a strict accept (at most one consecutive
        provisional round).  Otherwise, under --provisional-accept, a rejected
        candidate becomes the next round's working base when its repair was
        decisively positive and dev did not get worse: repair net >= +4 cells,
        dev net >= 0 cells, and dev regressions within the cap.

        --round-gate progressive: the working base never expires on a
        rejection.  Any net-positive rejected candidate (dev or repair cells
        > 0) becomes — or supersedes — the working base, chaining without
        limit; the dev gate keeps veto power over significant harm only
        (p_harm).  Rationale: at this dev size the gate's MDE sits far above
        real effect sizes (both harness lines were round-rejected four-for-four
        and then cleared the preregistered frozen set), so certification of
        the cumulative package belongs to the frozen-set settlement, not to
        in-round significance.  The committed lineage is untouched either way.
        """
        progressive = self.a.round_gate == "progressive"
        if self.state.get("provisional") and not progressive:
            expired = self.state["provisional"]
            self.state["provisional"] = None
            self.save_state()
            finalization.setdefault("ledger", {})["provisional_expired"] = {
                "round_taken": expired["round"],
                "tree_sha256": expired["tree_sha256"],
            }
            self.log(
                f"PROVISIONAL r{rnd}: base from round {expired['round']} expired "
                "(no strict accept in its follow-up round); working base "
                "reverts to the committed best"
            )
            return
        if not progressive and not getattr(self.a, "provisional_accept", False):
            return
        if not verdict.get("dev_evaluated"):
            return
        if not progressive and not verdict.get("repair_ok"):
            return
        dense = self._dense_rewards()
        k = self.a.k
        repair_task_count = int(verdict.get("repair_task_count") or 0)
        if not progressive and repair_task_count <= 0:
            return
        # A dense benchmark moves fractions of a cell: rounding a real +0.4
        # score gain to zero cells reports "not improved" and stalls the
        # progressive chain on genuine progress.  Dense arms therefore keep the
        # score delta -- same unit (cells x mean score), just not integerised.
        def cells(value: float) -> int | float:
            return round(value, 4) if dense else round(value)

        def show(value: int | float) -> str:
            return f"{value:+.2f}" if dense else f"{value:+d}"

        repair_net_cells = (cells(
            (verdict["repair_cand"] - verdict["repair_base"]) *
            repair_task_count * k
        ) if repair_task_count > 0 else 0)
        dev_net_cells = cells(
            (verdict["net_pp"] / 100.0) * len(self.dev_ids) * k
        )
        stats = verdict.get("gate_stats") or {}
        if progressive:
            improved = dev_net_cells > 0 or repair_net_cells > 0
            harmful = stats.get("p_harm", 1.0) < 0.05
            if not improved or harmful:
                kept = self.state.get("provisional")
                self.log(
                    f"PROVISIONAL r{rnd}: candidate not adopted (dev "
                    f"{show(dev_net_cells)} cells, repair "
                    f"{show(repair_net_cells)} "
                    f"cells{', significant dev harm' if harmful else ''}); "
                    + (f"working base kept from round {kept['round']}"
                       if kept else "working base stays the committed best")
                )
                return
        elif self.a.round_gate == "calibrated" and stats:
            # The dn cap sits below its own null (a same-package pair regresses
            # 2.8 dev tasks on average); the calibrated mode replaces it with
            # the harm sign test at the same significance the gate uses.
            if (repair_net_cells < 4 or dev_net_cells < 0 or
                    stats.get("p_harm", 1.0) < 0.05):
                return
        elif (repair_net_cells < 4 or dev_net_cells < 0 or
                verdict["dn"] > self.a.reg_cap):
            return
        superseded = self.state.get("provisional")
        if superseded:
            finalization.setdefault("ledger", {})["provisional_superseded"] = {
                "round_taken": superseded["round"],
                "tree_sha256": superseded["tree_sha256"],
            }
            self.log(
                f"PROVISIONAL r{rnd}: net-positive candidate supersedes the "
                f"round-{superseded['round']} working base"
            )
            self.state["provisional"] = None
        candidate_sha256 = str(verdict.get("candidate_tree_sha256") or "")
        if self._package_digest(self.cand_dir) != candidate_sha256:
            raise RuntimeError(
                "provisional candidate bytes no longer match the evaluated package"
            )
        provisional_dir = self.package_root / f"provisional_r{rnd}"
        assert_mutation_paths_safe([provisional_dir])
        if provisional_dir.exists():
            raise RuntimeError(
                f"refusing to overwrite provisional snapshot: {provisional_dir}"
            )
        shutil.copytree(self.cand_dir, provisional_dir)
        if self._package_digest(provisional_dir) != candidate_sha256:
            raise RuntimeError(
                f"provisional snapshot verification failed: {provisional_dir}"
            )
        self.state["provisional"] = {
            "round": rnd,
            "package": str(provisional_dir),
            "tree_sha256": candidate_sha256,
            "repair_net_cells": repair_net_cells,
            "dev_net_cells": dev_net_cells,
        }
        self.save_state()
        finalization.setdefault("ledger", {})["provisional_taken"] = {
            "round": rnd,
            "tree_sha256": candidate_sha256,
            "repair_net_cells": repair_net_cells,
            "dev_net_cells": dev_net_cells,
        }
        self.log(
            f"PROVISIONAL r{rnd}: rejected candidate becomes next round's "
            f"working base (repair {show(repair_net_cells)} cells, dev "
            f"{show(dev_net_cells)} cells, dn={verdict['dn']}); "
            "committed best unchanged"
        )

    def qualify_harness(self):
        """Cheap Read/Edit qualification before any benchmark simulation."""
        qdir = self.run_dir / "qualification"
        self._safe_rmtree(qdir)
        candidate = qdir / "candidate"
        (candidate / "em" / "procedure").mkdir(parents=True, exist_ok=True)
        visible_dir = qdir / "visible"
        visible_dir.mkdir()
        nonce = f"{self.a.run_id}-{time.time_ns()}"
        probe_path = visible_dir / "evidence.md"
        probe_path.write_text(
            f"QUALIFICATION_NONCE={nonce}\n", encoding="utf-8",
        )
        initial_manifest = (
            "name: harness_probe\n\n"
            "delivery:\n"
            "  - use: need_driven_retrieval\n"
            "    cfg: {em_types: [procedure], top_k: 1, min_overlap: 1}\n"
            "checkers: []\n"
            "board: {status_board: false, notice_in_wm: never}\n")
        expected_manifest = initial_manifest.replace(
            "name: harness_probe", "name: harness_probe_edited", 1)
        expected_card = ("---\n"
                         "id: qualification_protocol\n"
                         "type: procedure\n"
                         "---\n"
                         "Read evidence before editing.\n")
        manifest_path = candidate / "manifest.yaml"
        card_path = (
            candidate / "em" / "procedure" /
            "qualification_protocol.md"
        )
        result_path = qdir / "probe_result.json"
        manifest_path.write_text(initial_manifest, encoding="utf-8")
        forbidden_probe = self.run_dir / "qualification_forbidden_probe.txt"
        if forbidden_probe.exists():
            raise RuntimeError(f"stale qualification canary exists: {forbidden_probe}")
        forbidden_read_probe = HERE / "driver.py"
        if not forbidden_read_probe.is_file():
            raise RuntimeError(
                f"qualification forbidden-Read canary is missing: {forbidden_read_probe}"
            )
        forbidden_existing_edit_sha256 = self._sha256_file(
            forbidden_read_probe
        )
        qrel = qdir.relative_to(ROOT)
        crel = candidate.relative_to(ROOT)
        probe_rel = probe_path.relative_to(ROOT)
        forbidden_rel = forbidden_probe.relative_to(ROOT)
        forbidden_read_rel = forbidden_read_probe.relative_to(ROOT)
        prompt = f"""You are qualifying as the Recuris Meta-Agent. Use only Read/Edit.

0. First, explicitly CALL Read on `{forbidden_read_rel}` with `limit` set to 1.
   This is a permission canary: the harness must deny it. Continue after the denial.
1. Explicitly CALL Edit on `{forbidden_read_rel}` with
   `old_string` set to `QUALIFICATION_STRING_THAT_DOES_NOT_EXIST` and
   `new_string` set to `MUST_BE_DENIED_EXISTING_FILE`.
   This existing hidden-file canary must be permission-denied before Edit can
   inspect whether old_string matches. Continue after the denial.
2. Explicitly CALL Edit on `{forbidden_rel}` with `old_string` empty and
   `new_string` set to `MUST_BE_DENIED`.
   This is a permission canary: the harness must deny it. Continue after the denial.
3. Read `{probe_rel}` and copy its nonce exactly.
4. Edit `{crel}/manifest.yaml`: change only `name` to `harness_probe_edited`.
5. Create `{crel}/em/procedure/qualification_protocol.md` with Edit and EXACTLY these bytes:
```
---
id: qualification_protocol
type: procedure
---
Read evidence before editing.
```
Do not include the nonce in that card.
6. Create `{qrel}/probe_result.json` with Edit as exactly one JSON object with keys
   `nonce` (the exact value read in step 3) and `status` (`ok`).

Edit nowhere else. Do not run commands. End after all seven steps."""
        ok = self.cc_session(
            "qualification", prompt, QUALIFICATION_TOOLS,
            self.a.cc_timeout,
            [manifest_path, card_path, result_path],
            [probe_path, manifest_path, card_path, result_path],
            denied_directory_reads=[
                visible_dir,
                candidate,
                candidate / "em",
                candidate / "em" / "procedure",
            ],
        )
        errors = []
        if not ok:
            errors.append("Claude Code session did not finish successfully")
        session = getattr(self, "last_cc_session", {})
        if set(session.get("tool_surface", [])) != {"Read", "Edit"}:
            errors.append(f"unexpected tool surface: {session.get('tool_surface')}")
        if session.get("upstream_models") != [self.a.meta_model]:
            errors.append(
                f"upstream model mismatch: {session.get('upstream_models')}, "
                f"expected {[self.a.meta_model]}"
            )
        observed = set(session.get("observed_tools", []))
        if not {"Read", "Edit"}.issubset(observed):
            errors.append(f"qualification did not exercise Read/Edit: {sorted(observed)}")
        calls = session.get("tool_calls", [])
        normalized_calls = [(call.get("name"), str((call.get("input") or {}).get("file_path", ""))
                             .replace("\\", "/")) for call in calls]
        if not any(
                name == "Read" and
                path.endswith("/qualification/visible/evidence.md")
                   for name, path in normalized_calls):
            errors.append(
                "Read tool was not used on nested visible evidence.md"
            )
        if not any(name == "Edit" and path.endswith("/candidate/manifest.yaml")
                   for name, path in normalized_calls):
            errors.append("Edit tool was not used on candidate/manifest.yaml")
        forbidden_read_suffix = "metaagent/driver.py"
        if not any(name == "Read" and path.endswith(forbidden_read_suffix)
                   for name, path in normalized_calls):
            errors.append("negative permission probe did not attempt the forbidden Read")
        if not any(
                name == "Edit" and path.endswith(forbidden_read_suffix)
                for name, path in normalized_calls):
            errors.append(
                "negative permission probe did not attempt the existing "
                "hidden-file Edit"
            )
        edit_paths = {path for name, path in normalized_calls if name == "Edit"}
        forbidden_suffix = "/qualification_forbidden_probe.txt"
        if not any(path.endswith(forbidden_suffix) for path in edit_paths):
            errors.append("negative permission probe did not attempt the forbidden Edit")
        if forbidden_probe.exists():
            errors.append("negative permission probe escaped its write scope")
        denial_text = json.dumps(session.get("permission_denials", []), ensure_ascii=False)
        denied_call_ids = {
            str(item.get("tool_use_id") or "")
            for item in session.get("permission_denials", [])
            if isinstance(item, dict)
        }
        existing_hidden_edit_ids = {
            str(call.get("id") or "")
            for call in calls
            if call.get("name") == "Edit" and
            str((call.get("input") or {}).get("file_path", ""))
            .replace("\\", "/").endswith(forbidden_read_suffix)
        }
        if ("metaagent" not in denial_text.replace("\\", "/") or
                "driver.py" not in denial_text):
            errors.append("forbidden Read was not recorded as a permission denial")
        if "qualification_forbidden_probe.txt" not in denial_text:
            errors.append("forbidden Edit was not recorded as a permission denial")
        if not (existing_hidden_edit_ids & denied_call_ids):
            errors.append(
                "existing hidden-file Edit was not recorded as a "
                "permission denial"
            )
        if self._sha256_file(
                forbidden_read_probe) != forbidden_existing_edit_sha256:
            errors.append(
                "existing hidden-file Edit canary changed its target"
            )
        if session.get("read_scope_violations"):
            errors.append(
                "qualification had a non-denied out-of-scope Read: "
                f"{session.get('read_scope_violations')}"
            )
        if session.get("edit_scope_violations"):
            errors.append(
                "qualification had a non-denied out-of-scope Edit: "
                f"{session.get('edit_scope_violations')}"
            )
        for suffix in ("/candidate/em/procedure/qualification_protocol.md",
                       "/qualification/probe_result.json"):
            if not any(path.endswith(suffix) for path in edit_paths):
                errors.append(f"Edit tool was not used on {suffix.rsplit('/', 1)[-1]}")
        if probe_path.read_text(
                encoding="utf-8") != f"QUALIFICATION_NONCE={nonce}\n":
            errors.append("nested visible evidence.md was modified")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            result = None
            errors.append(f"invalid probe_result.json: {exc}")
        if result != {"nonce": nonce, "status": "ok"}:
            errors.append(f"Read/Edit probe mismatch: {result!r}")
        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
        except Exception as exc:
            manifest_text = None
            errors.append(f"manifest is missing/unreadable: {exc}")
        if manifest_text != expected_manifest:
            errors.append("Edit probe changed more than the manifest name or did not edit it")
        try:
            card_text = card_path.read_text(encoding="utf-8")
        except Exception as exc:
            card_text = None
            errors.append(f"procedure card is missing/unreadable: {exc}")
        if card_text != expected_card:
            errors.append("Edit-create probe procedure card does not match the exact contract")
        if card_text and nonce in card_text:
            errors.append("nonce leaked into the procedure card")
        expected_files = {
            "visible/evidence.md", "probe_result.json",
            "candidate/manifest.yaml",
            "candidate/em/procedure/qualification_protocol.md",
        }
        actual_files = {p.relative_to(qdir).as_posix() for p in qdir.rglob("*") if p.is_file()}
        extras = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        if extras or missing:
            errors.append(f"qualification file set mismatch; extras={extras}, missing={missing}")
        lint = subprocess.run([PY, str(HERE / "lint.py"), "--pkg",
                               str(candidate.relative_to(ROOT)), "--domain", self.domain,
                               "--benchmark", getattr(self, "benchmark", "tau2")],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=300)
        if lint.returncode != 0:
            # stdout carries the lint report; stderr carries the reason the
            # linter never got as far as a report. Reporting only the first is
            # how a missing interpreter or a renamed module surfaced as
            # "failed lint:" with nothing after the colon.
            detail = ((lint.stdout or "").strip() or (lint.stderr or "").strip()
                      or f"no output, exit status {lint.returncode}")
            errors.append(f"qualification candidate failed lint: {detail[-1200:]}")
        verify_champions()
        report = {"ok": not errors, "requested_model": self.a.meta_model,
                  "reasoning_effort": self.a.meta_reasoning,
                  "temperature": META_TEMPERATURE,
                  "resolved_models": session.get("resolved_models", []),
                  "tool_surface": session.get("tool_surface", []),
                   "observed_tools": session.get("observed_tools", []),
                   "permission_denials": session.get("permission_denials", []),
                   "read_scope_violations": session.get("read_scope_violations", []),
                   "trace": session.get("trace"),
                  "nonce": nonce, "errors": errors}
        self._write_json_atomic(qdir / "report.json", report)
        if errors:
            raise RuntimeError("Meta-Agent harness qualification failed:\n- " + "\n- ".join(errors))
        self.log("Meta-Agent harness qualification PASS "
                 "(allowed Read/Edit + forbidden Read/Edit denial + lint)")

    def run(self):
        self.log(f"=== driver start run={self.a.run_id} domain={self.domain} "
                  f"train={len(self.train_ids)} dev={len(self.dev_ids)} k={self.a.k} "
                  f"protocol={getattr(self, 'benchmark_protocol', OFFICIAL_BENCHMARK_PROTOCOL)} "
                  f"meta_workflow={getattr(self, 'meta_workflow', 'split')} "
                  f"mode={'generation-only' if getattr(self, 'generate_only', False) else 'experiment'} "
                  f"gate={self.a.round_gate} "
                 f"worker={self.a.worker_model}/"
                 f"{self.a.worker_reasoning} meta={self.a.meta_model}/"
                 f"{self.a.meta_reasoning} ===")
        try:
            self.integrity()
            replay_requested = bool(
                getattr(self, "replay_source_run", None)
            )
            if replay_requested and not self.a.qualify_only:
                # This validates source/runtime/profile bytes without copying
                # hidden evidence or packages into the model-visible run.
                self.preflight_replay()
            if self.a.qualify_harness or self.a.qualify_only:
                self.qualify_harness()
            if self.a.qualify_only:
                self.log("=== qualification-only run done; Tau2 simulations=0 ===")
                return
            if replay_requested:
                self.prepare_replay()
            elif self.state["best"] is None:
                if self.a.base == "neutral":
                    self.neutral()
                elif self.a.base == "bootstrap":
                    self.bootstrap()
                else:
                    self.initialize_from_package()
            last_round = self.state["round_done"]
            if self.state["round_outcomes"].get(str(last_round)) == "converged":
                self.log(f"=== driver already converged at round {last_round}; no-op resume ===")
                return
            start = self.state["round_done"] + 1
            # --rounds is a target evolution round, not "N more" on resume.
            # Re-running the same command therefore cannot silently double the
            # adaptive-query budget.
            for rnd in range(start, self.a.rounds + 1):
                outcome = self.round(rnd)
                self.log(f"=== round {rnd}: {outcome} ===")
                if outcome == "converged":
                    break
            self.log(f"=== driver done. best={self.state['best']} "
                     f"sims={self.state['sims']} changelog={len(self.state['changelog'])} ===")
        finally:
            try:
                self.integrity()
            finally:
                try:
                    self.stop_proxy()
                finally:
                    self._release_run_lock()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domain", default="retail")
    ap.add_argument("--benchmark", choices=sorted(SUPPORTED_BENCHMARKS),
                    default="tau2",
                    help="downstream runtime (this release ships tau2)")
    ap.add_argument("--tau2-retrieval-config", default=None,
                    help="knowledge-domain KB access mode (banking_knowledge: "
                         "'openai_embeddings' matches the June baseline). "
                         "Injected explicitly into the sanitized downstream "
                         "env and recorded on every evaluation artifact.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--splits",
                    help="JSON train/dev/test split (required except with --qualify-only)")
    ap.add_argument("--base", default="neutral",
                    help="'neutral' (deterministic M0), 'bootstrap', or a read-only package path")
    ap.add_argument(
        "--reuse-artifacts-run",
        help=(
            "prior run whose sealed evaluation artifacts may be reused when "
            "every identity field (tag, tasks, k, package tree hash, agent, "
            "models, protocol) matches; mismatches fall back to a fresh run"
        ),
    )
    ap.add_argument(
        "--provisional-accept",
        action="store_true",
        help=(
            "let a gate-rejected candidate whose repair screen was decisively "
            "positive (net >= +4 cells) and whose held-out dev did not get "
            "worse (net >= 0 cells, regressions within --reg-cap) serve as "
            "the WORKING BASE for exactly one following round. The committed "
            "lineage never advances on a provisional; a later strict accept "
            "is always measured against the committed best, so it certifies "
            "the cumulative package. Designed for domains whose skill-memory "
            "value is superadditive across components (single increments sit "
            "below the gate's detection band)."
        ),
    )
    ap.add_argument(
        "--accept-source-change",
        metavar="REASON",
        help=(
            "on resume, accept that runtime/Meta-Agent sources changed since "
            "this run's provenance was recorded: the file-level delta and this "
            "REASON are appended to provenance as a source amendment and the "
            "new source record is adopted. Without this flag a source change "
            "still refuses to resume (fail-closed). Operator-only escape "
            "hatch for deliberate framework fixes between crash and resume."
        ),
    )
    ap.add_argument(
        "--replay-source-run",
        help=(
            "sealed autonomous run whose exact round-1 train evidence and M0 "
            "are replayed; all candidate gates remain fresh"
        ),
    )
    ap.add_argument(
        "--replay-source-round", type=int, default=1,
        help="frozen source round (replay schema v1 supports exactly round 1)",
    )
    ap.add_argument(
        "--generate-only", action="store_true",
        help=(
            "with a frozen replay source, generate, validate, and freeze the "
            "candidate before any held-out downstream evaluation"
        ),
    )
    ap.add_argument(
        "--generation-profile-spec",
        help=(
            "locked generic generation-profile JSON; valid only with "
            "--generate-only and an exact raw SHA-256"
        ),
    )
    ap.add_argument(
        "--generation-profile-sha256",
        help="expected SHA-256 of the raw generation-profile JSON bytes",
    )
    ap.add_argument("--arm", choices=["autonomous", "seeded-control"],
                    default="autonomous",
                    help="autonomous forbids package seeds; seeded-control labels explicit-seed arms")
    ap.add_argument("--round-gate",
                    choices=["relaxed", "strict", "calibrated", "progressive",
                             "always"],
                    default="relaxed",
                    help=("progressive: in-round commits still need the "
                          "calibrated significance test, but any net-positive "
                          "rejected candidate becomes (or replaces) the "
                          "working base with no expiry — progress accumulates "
                          "across rounds and the frozen-set settlement, not "
                          "the dev gate, certifies the cumulative package. "
                          "Dev keeps veto power over significant harm only. "
                          "Implies --provisional-accept."))
    ap.add_argument("--power-gate", choices=["off", "warn", "enforce"],
                    default="warn",
                    help=("after r0, compare the dev gate's minimal detectable "
                          "effect against --power-gate-mde-cap; enforce stops "
                          "the campaign with a required-k suggestion instead "
                          "of burning rounds on an underpowered instrument"))
    ap.add_argument("--power-gate-mde-cap", type=float, default=12.0,
                    help="largest acceptable clean-MDE in pp (default 12.0)")
    ap.add_argument(
        "--benchmark-protocol",
        choices=sorted(SUPPORTED_BENCHMARK_PROTOCOLS),
        default=None,
        help=("official tau2 inputs, or the separately reported diagnostic "
              "completion-guard-v1 inputs; defaults to the benchmark's "
              "official protocol"),
    )
    ap.add_argument("--reg-cap", type=int, default=1)
    ap.add_argument("--max-concurrency", type=int, default=8)
    ap.add_argument("--eval-timeout", type=int, default=3600)
    ap.add_argument(
        "--eval-resume-attempts", type=int, default=1,
        help=("number of same-checkpoint Tau2 resumes allowed after a bounded "
              "subprocess timeout (0 disables recovery)"),
    )
    ap.add_argument("--cc-timeout", type=int, default=2400)
    ap.add_argument("--max-sims", type=int, default=3000)
    ap.add_argument("--worker-model", default=DEFAULT_WORKER_MODEL,
                    help="frozen for formal runs")
    ap.add_argument("--worker-reasoning", choices=REASONING_LEVELS, default="medium",
                    help="frozen at medium for formal runs")
    ap.add_argument("--open-worker", action="store_true",
                    help=("evolve the memory for a downstream agent served "
                          "elsewhere over an OpenAI-compatible endpoint. "
                          "Requires --worker-model openai/<served-name> and "
                          "--worker-llm-args. The user simulator stays frozen, "
                          "so the arm remains single-variable."))
    ap.add_argument("--worker-llm-args", default=None,
                    help=("JSON for an --open-worker downstream. Required: "
                          "api_base, temperature (0.0), timeout (360), "
                          "num_retries (2). Optional: api_key, extra_body, "
                          "stop_token_ids, max_tokens, reasoning_effort, "
                          "allowed_openai_params. Validated by the same "
                          "checker the standalone --open-downstream arm uses."))
    ap.add_argument("--simulator-model", default=DEFAULT_SIMULATOR_MODEL,
                    help="frozen Tau2 user simulator model")
    ap.add_argument("--simulator-reasoning", choices=REASONING_LEVELS, default="medium",
                    help="frozen at medium for formal runs")
    ap.add_argument("--meta-model", default=DEFAULT_META_MODEL)
    ap.add_argument("--meta-reasoning", choices=REASONING_LEVELS, default="high")
    ap.add_argument(
        "--meta-workflow", choices=sorted(META_WORKFLOWS), default="split",
        help=(
            "split keeps diagnosis/review/editing in separate sessions; "
            "continuous diagnoses and edits in one session; hierarchical runs "
            "parallel diagnosis batches before one reducer edits the candidate"
        ),
    )
    ap.add_argument(
        "--diagnosis-workers", type=int, default=8,
        help="maximum parallel diagnosis workers for hierarchical workflow",
    )
    ap.add_argument(
        "--recursive-review",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=("in split workflow, review each valid diagnosis against recent "
              "same-task train evidence and validate retrieval changes"),
    )
    ap.add_argument("--proxy-port", type=int, default=4000)
    ap.add_argument("--qualify-harness", action="store_true",
                    help="qualify scoped Read/Edit before the first benchmark simulation")
    ap.add_argument("--qualify-only", action="store_true",
                    help="run only the zero-simulation Claude Code/Doubao qualification")
    args = ap.parse_args()
    if args.qualify_only:
        args.qualify_harness = True
    if not args.qualify_only and not args.splits:
        ap.error("--splits is required for an experiment run")
    if (args.rounds < 0 or args.k < 1 or args.max_sims < 0 or
            not 1 <= args.diagnosis_workers <= 16 or
            not 0 <= args.eval_resume_attempts <= 3):
        ap.error(
            "--rounds/--max-sims must be non-negative, --k must be positive, "
            "--diagnosis-workers must be between 1 and 16, and "
            "--eval-resume-attempts must be between 0 and 3"
        )
    if getattr(args, "provisional_accept", False) and (
            args.replay_source_run or args.generate_only or
            getattr(args, "generation_profile_spec", None)):
        ap.error(
            "--provisional-accept applies only to autonomous experiment runs; "
            "it cannot combine with replay or generation-profile arms"
        )
    if args.generate_only:
        if not args.replay_source_run:
            ap.error("--generate-only requires --replay-source-run")
        if args.qualify_only:
            ap.error("--generate-only is incompatible with --qualify-only")
        if args.max_sims != 0:
            ap.error("--generate-only requires --max-sims 0")
    if bool(args.generation_profile_spec) != bool(
            args.generation_profile_sha256):
        ap.error(
            "--generation-profile-spec and --generation-profile-sha256 "
            "must be provided together"
        )
    if args.generation_profile_spec and not args.generate_only:
        ap.error("--generation-profile-spec requires --generate-only")
    if args.generation_profile_spec and args.meta_workflow != "split":
        ap.error("--generation-profile-spec requires --meta-workflow split")
    Driver(args).run()


if __name__ == "__main__":
    main()
