# Operations

Failure modes that cost us real time, and what they actually were. None of
these are obvious from the error you get.

## Runs and resuming

**`--resume` continues an interrupted run of the same code version.** Without
it, a non-empty save directory is an error. That is deliberate: results from
two code versions merging silently is worse than a stopped run.

**A resume only re-runs cells that failed for infrastructure reasons.** A task
that legitimately scored zero is not retried. So "resume finished instantly"
usually means the run was already complete, not that resume did nothing.

**Count what is owed, not what is null.** A killed arm has *missing* cells, not
null ones, so a completeness check keyed on nulls declares it clean. Compare
against the expected count: `tasks × k`.

## Paired comparisons

**Two arms must share a commit, a split, `k`, and their decoding settings.**
The treatment validators enforce the model and decoding parts; the rest is on
you. Use the shipped configs, which inherit from a common base precisely so an
arm cannot differ where it does not say it differs.

**Never pair a partially-complete arm.** A task with one lucky trial out of an
intended four reads as 100%. We reported a +18.65 that fell to +15.57 once the
arm finished. `recuris compare` refuses to pair when fewer than 40 tasks are
common or when either side has a task short of `k`, and those guards exist
because of that.

**Selecting a subset by outcome and then comparing is regression to the mean.**
It will produce a confident interval on an unchanged package. If you subset,
run the same subsetting on a null contrast first and report what it gives.

## τ²-Bench

**The checkout must be a real git clone.** tau2 records the benchmark revision
by running `git rev-parse HEAD` in the working directory; an unpacked archive
fails at startup. `third_party/tau2/setup.sh` produces a clone.

**The three treatment switches are read by the benchmark's orchestrator, not by
the kernel.** `TAU2_GATE_TERM`, `TAU2_GATE_TERM_WM` and `TAU2_STATUS_BOARD`
therefore do not appear in any Recuris log unless you put them there. Every
shipped config declares all three, the run prints them at startup, and each run
writes them into `_params.json`.

**The judge is not the leaderboard's judge.** Absolute scores here are not
comparable to published τ² numbers. Cross-arm comparisons within this
repository are, because both arms share the judge exactly.

## SkillFlow

**Run one harbor job at a time.** Concurrent jobs exhaust the Docker IPv4
address pool, and what you see is a container-networking error that looks like
something else entirely.

**Configs are generated, never committed.** A committed config carries a
credential and drifts from its pair. `recuris skillflow render-configs`
produces both arms from one function, so they cannot differ except in the flag.

**Check that the skill arm actually injected its template.** `NoInstallQwenCode`
overrides `run()`, which bypasses harbor's template hook; before our patch, a
skill arm with a template configured ran as a bare baseline and produced a
perfectly plausible "no effect" result. `third_party/skillflow/setup.sh`
applies the fix. After rendering, check that the config names a template file
that exists and that the prompt the agent receives contains the machine
document's marker: a silent regression to bare reads as a clean negative.

## Terminal-Bench 2.1

**Never let harbor delete task images.** Its default is `--rmi all`, which
removes the image when a job finishes. Every config the TTA driver emits sets
`delete: false`.

**Image snapshots change results.** The same baseline agent scored 34.5% and
40.0% on two hosts differing only in their image snapshot. The stratification
in `splits/tb21/` is valid only for the snapshot recorded in its manifest.

**No `result.json` is infrastructure, not a zero.** The driver trips a breaker
after three consecutive missing results and leaves the remaining tasks
unattempted. Recording them as failures would produce an arm that finished in
seconds with a fabricated score, which is worse than crashing because it looks
like data.

## The evolution loop

**Run `recuris metaagent qualify` first.** It exercises the session plumbing
with zero simulations and fails on the things that are painful to discover four
hours in.

**Raise the coding agent's shell timeout.** Package evaluation takes minutes.
At the default the call is backgrounded at 120 seconds, the session gets an
empty output file and no failure signal, and it then diagnoses from nothing.
`claude_code_env.sh` sets it.

**Do not let a session persist across phases.** Each phase starts from the
evidence the driver assembled for it. A session carrying context from the
previous phase has seen material that was deliberately withheld.

**A round with no accept is a result.** The gate rejecting everything is the
gate working. What needs investigating is a round that accepts something the
fingerprint says never fired.

## Reading a result

Read the mechanism before the score. `recuris scorecard` refuses to print a
score without first printing gate fires, truth bounces, review behaviour and
sanitizer strips — because a package that never fired and a package that fired
and did not help produce the same number, and only one of them is a result
about memory.
