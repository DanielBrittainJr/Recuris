# Architecture

## The one idea

An agent that fails a task the same way twice has a memory problem, not a
capability problem. So separate the two and change only one of them:

- a **machine** — the turn loop, the gates, the checkers — that is byte-identical
  across every arm of every comparison;
- a **memory** `M = (E, W, ρ, C)` that is the only thing that differs.

Everything else follows from wanting that separation to be checkable rather
than asserted. `src/recuris/` is the machine, `skill_memories/` is the memory,
and nothing crosses.

## M = (E, W, ρ, C)

| | Name | What it is | Where it lives |
|---|---|---|---|
| `E` | experiential memory | cards distilled from prior failures | `em/**/*.md`, one card per file |
| `W` | working memory | the entry schema and its ledger | `wm:` in the manifest |
| `ρ` | invocation policy | when a card is delivered, and which | `delivery:` in the manifest |
| `C` | checkers | what must hold before the agent may claim progress | `checkers:` plus an optional `plugin.py` |

The split matters because these fail differently. A missing card is a knowledge
gap. A card that exists but never fires is a routing failure, and it looks
exactly like a knowledge gap from the score alone. That is why the activation
probe exists: it asks whether a carrier *can* fire, offline, before anything is
measured.

## The invariant turn

`src/recuris/runtime.py` is the loop every arm runs. In outline:

1. classify the incoming message;
2. update the working-memory ledger from the model's proposal, under write
   permissions;
3. deliver whatever `ρ` selects for this event;
4. draft;
5. run the checkers over the draft; a bounce sends it back for one redraft;
6. ground the result against real tool receipts;
7. commit.

Three properties are load-bearing, and each of them exists because its absence
cost us something.

**The model cannot write its own progress.** `DONE` is set only by the harness,
with a real receipt behind it. When the model could write it, its belief that
it had finished landed in the ledger verbatim, and that single false `DONE`
disabled the termination gate, the truth protocol and the status board in one
chain.

**A synthetic receipt is not evidence.** Receipts fabricated by the pre-write
review are registered and rejected on the way back in. Without that, a bounced
draft's fake `error=False` message was read as a successful execution.

**No fallback.** An executed write that matches no pending entry is counted and
logged, never invented into the ledger. The fallback that used to do so pushed
raw tool-call signatures onto the customer-visible board.

## The gate

`src/recuris/metaagent/gates.py` is the part that decides what is kept, and it
is deliberately small enough to read in one sitting.

A candidate is admitted only if the paired held-out difference has a bootstrap
interval excluding zero **and** no more than `reg_cap` items regressed. The
bootstrap resamples *items*, not trials: trials within an item are not
independent, and resampling them reports an interval far narrower than the
evidence supports.

Nothing in that file consults a model. A model proposes; arithmetic disposes.
That asymmetry is the claim, so it lives in one place where it can be checked.

Two supporting checks exist because a bare score cannot distinguish the cases
they separate:

- **leakage** — a card containing a test-set answer is rejected outright;
- **fingerprint** — the prescribed mechanism must actually have fired, so a
  change that improved the score for an unrelated reason cannot be credited to
  the mechanism it claimed to fix, and the next round cannot build on a false
  diagnosis.

## The loop

`src/recuris/metaagent/driver.py` runs the campaign. One round:

```
evaluate the current best on the train split
        -> failing tasks, sanitized trajectories, mechanism fingerprint
diagnose and patch                       (an external coding agent)
        -> plan.json + a disposable candidate package
validate                                 (code)
        -> plan schema, lint, leak scan, activation probe, repair screen
decide                                   (code)
        -> held-out paired gate: admit or discard
record                                   (code)
        -> ledger, changelog, lesson
```

Only the second step is generative, and it is the only step that leaves the
repository. Everything the loop remembers between rounds is a file the driver
writes and re-injects — the review, the lessons, the ledger tail, the
changelog, the regression suspects. Never the model's own context. That is what
makes a round reproducible from its artefacts.

## Adapters

`src/recuris/adapters/` is thin by construction. An adapter's job is to
translate one benchmark's message shapes into the kernel's, and nothing else.
No benchmark is modified: the tau2 agent registers itself with tau2's own
registry at run time, and the Terminal-Bench agent attaches through harbor's
import-path mechanism.

Anything that would otherwise be adapter-specific policy lives in the package
instead. The airline feasibility oracle is the clearest case: it is domain
code, it lives in `skill_memories/tau2_airline/plugin.py`, and a human reviews
it. The kernel provides the call site and the write permission, and the model
can never rule on feasibility itself.

## Where to look

| Question | File |
|---|---|
| What does a turn do? | `src/recuris/runtime.py` |
| What is kept, and why? | `src/recuris/metaagent/gates.py` |
| How does a package become a configuration? | `src/recuris/skillmemory.py` |
| Where is the model allowed to write? | `src/recuris/wm/ledger.py` |
| What does a round look like? | `src/recuris/metaagent/driver.py`, `round()` |
| What is the package format? | `docs/skill-memory-format.md` |
