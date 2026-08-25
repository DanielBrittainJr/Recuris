# Terminal-Bench 2.1

Upstream: <https://github.com/harbor-framework/terminal-bench-2-1> at commit
`5c8eadf` (Apache-2.0). 91 tasks. We run it unmodified — there is no patch in
this directory, because the Recuris agent attaches through harbor's
`import_path` mechanism and the benchmark itself does not have to change.

Harbor 0.20.0 is the runner. See `../harbor/`.

## Results here depend on the container images, not only on the tasks

Each task builds a container image, and the image is not pinned by the task
repository. We measured **34.5% versus 40.0% for the same baseline agent on two
hosts that differed only in their image snapshot.** That is larger than most of
the effects anyone would want to report.

Two consequences, and neither is optional:

1. The task stratification in `splits/tb21/tta_taskset_v3.json` was derived on
   one snapshot. `splits/tb21/split_manifest.json` records it as a hard
   precondition. Running against different images gives a differently
   stratified set, and per-stratum numbers will not match ours.
2. Build the images once and keep them. Harbor's default is `--rmi all`, which
   deletes the task image when a job finishes; every config the TTA driver
   emits sets `delete: false` for exactly this reason. Rebuilding is not free
   and it is not idempotent.

## Test-time adaptation

```bash
recuris tta run --taskset splits/tb21/tta_taskset_v3.json \
    --run-id demo --arm tta --rounds 2 --concurrency 3
```

Three arms, all on the same attempt budget so the comparison is not N attempts
against one:

| `--arm` | What it isolates |
|---|---|
| `bare` | the stock agent, `rounds` independent attempts |
| `m0` | the seed package, `rounds` independent attempts, **no learning between them** — this separates the machine from the learning |
| `tta` | the seed package, and after each failure the meta-agent writes a card into a per-task archive that the next attempt carries |

Rounds 1 of `m0` and `tta` are identical by construction: neither has
learned anything yet, so any difference between them at round 1 is noise.

The information contract is enforced in code, not by prompt discipline. The
meta-agent sees the task instruction (which the worker also sees), the failed
attempt's trajectory, and one bit: a hidden verifier scored it zero. It never
sees the verifier, the tests, the expected output, or any other attempt's
reward. Each run records that contract in its `provenance.json`.
