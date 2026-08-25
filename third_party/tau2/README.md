# tau2-Bench

This directory is a seam, not a copy. It holds the upstream reference, the
patch we apply to it, and the checksums of the payload our results were
produced against. `setup.sh` assembles the tree; nothing of Sierra Research's
is committed here.

Upstream: <https://github.com/sierra-research/tau2-bench> (MIT, © 2025 Sierra
Research). The MIT notice is reproduced in `../../THIRD_PARTY_NOTICES.md`.

## The version situation, stated plainly

The checkout `setup.sh` produces is a hybrid:

| Part | Version | Why |
|---|---|---|
| harness code | `8ebb749` (v1.0.0 lineage) | the code our patch was written against and every campaign ran on |
| `data/tau2` | tag `v1.0.1` (`fc0055dc`) | the corrected task data every reported number was measured on |

Neither a plain v1.0.0 nor a plain v1.0.1 checkout reproduces our runs.

This matters beyond bookkeeping. v1.0.1 corrected 75+ tasks in retail and
airline, and upstream's own release notes state that results are not comparable
across the two versions. Anything quoted from before that release — including
most published tau2 numbers — is measuring a different benchmark. Our numbers
are on the corrected data.

## What `recuris.patch` changes

Eleven files, in three groups.

**Orchestrator hooks** (`orchestrator.py`, `llm_agent.py`, `write_review.py`).
The terminal working-memory gate and the customer-visible status board are
harness-side mechanisms: they consult the agent through the duck-typed
`wm_pending_lines` / `wm_board_text` hooks and act on the result. They are
**off unless enabled**, by `TAU2_GATE_TERM`, `TAU2_GATE_TERM_WM` and
`TAU2_STATUS_BOARD` — so with those unset, an `llm_agent` arm behaves exactly
as upstream does. Which of them a given result ran under is recorded in that
run's `_params.json` and named in the README; see also
`recuris.adapters.tau2.treatment.treatment_triple`.

**Judge and environment model** (`config.py`). Upstream pins the NL-assertion
judge and the environment-interface model to `gpt-4.1-2025-04-14`. There is no
CLI flag for either, so the patch changes the constants. Consequence, and it is
a real one: **absolute scores in this repository are not comparable to the
public tau2-Bench leaderboard**, which uses a different judge. Cross-arm
comparisons within this repository are valid, because both arms are judged by
exactly the same model. This is stated again in the README because it is the
single most likely thing for a reader to miss.

**Embeddings for knowledge domains** (`knowledge/**`,
`domains/banking_knowledge/retrieval.py`, `domains/telecom/environment.py`).
An OpenAI-compatible embedder plus cache-key handling, so the knowledge-base
domains can run against the same endpoint as everything else.

The patch deliberately does **not** include the in-fork agent module or the
`registry.py` hunk that registers it. That was an in-fork agent from an
earlier design, superseded by `recuris.adapters.tau2.agent`, which registers
itself at run time and needs no benchmark modification. The registry hunk was
its registration inside a `try`/`ImportError`, so leaving both out changes
nothing.

## Verifying

`setup.sh` runs the check itself; to repeat it later:

```bash
python scripts/verify_checksums.py third_party/tau2/CHECKSUMS.json external/tau2-bench
```

Text files are LF-normalised before hashing, because git rewrites line endings
on some platforms and a checker that reports a false mismatch is a checker
people turn off.
