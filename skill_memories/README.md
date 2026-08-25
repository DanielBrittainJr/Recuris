# Skill Memory packages

**This subtree is the portable artefact.** Everything under `src/recuris/` is
the machine: a frozen runtime that does not change between arms. Everything
here is the memory: what the meta-agent evolved, and the only thing that
differs between a bare arm and a skill arm.

A package is a directory. Its `manifest.yaml` declares the four components of
`M = (E, W, rho, C)`:

| Component | In the manifest | On disk |
|---|---|---|
| `E` — experiential memory | `em:` types the deliverers may draw on | `em/**/*.md`, one card per file |
| `W` — working memory | `wm:` entry kind, manager, schema | — |
| `rho` — invocation policy | `delivery:` list | — |
| `C` — checkers | `checkers:` list, `grounding:` matcher | optional `plugin.py` for domain rulings |

Loading a package is `recuris.skillmemory.load_skill_memory(path)`. The format
is documented in `docs/skill-memory-format.md`.

## What is here

| Package | Use it for | Notes |
|---|---|---|
| `tau2_retail` | **the retail arm, on any model** | The settled retail package. Every reported retail number used this. |
| `tau2_airline` | **the airline arm, on any model** | The settled airline package. Carries a `plugin.py`: airline feasibility is a domain ruling, so it lives in the package and is human-reviewed, never in the kernel. |
| `skillflow` | **the SkillFlow skill arm** | Machine document, router, 22 cards, and one template per task family plus a universal fallback. See `skillflow/README.md`. |
| `tb21_seed` | **the Terminal-Bench 2.1 `m0` and `tta` arms** | The seed the adapting arms start from. |
| `_base` | starting point for a new task | Generic working memory plus self-directed retrieval. `em/` is deliberately empty: copy the directory, add cards, and you have a working package without writing code. |

These four are the settled packages, and they are all there is. On the command
line:

```bash
recuris tau2 --domain retail  --agent recuris_agent --skill-memory tau2_retail
recuris tau2 --domain airline --agent recuris_agent --skill-memory tau2_airline
```

## Directory name versus declared name

A manifest declares the package's own `name:`, and for the shipped artefacts
that name is historical — they are committed exactly as they were evaluated, so
their internal names are whatever they were called at the time. The directory
is how you refer to a package; the declared name is what appears in a run's
logs.

| Directory | Declared name |
|---|---|
| `tau2_retail` | `tau2_retail_v3` |
| `tau2_airline` | `tau2_airline` |
| `skillflow` | `skillflow` |
| `tb21_seed` | `tb21_m0_v2` |
| `_base` | `_base` |

Renaming the directories was worth doing: a reader choosing a package should
not have to work out which internal version won. Rewriting the manifests to
match was not, because it would make the shipped packages differ from the ones
the numbers were measured on, for a cosmetic gain.

The same applies to everything else inside a package. Internal names, paths and
tool references in these files are historical, and one manifest comment still
names the project by the name it had before it was called Recuris. Nothing
outside this directory does; the CI name check enforces that for source code and
documentation, and stops here for the same reason.

## The protected set

`champions.lock.json` records the exact byte content of the four settled
packages, and `integrity/anchors.json` at the repository root holds the
aggregate digest it must match. The campaign driver verifies both before every
round: a settled package cannot be edited without the run stopping.

To change one deliberately:

```bash
python scripts/reanchor_integrity.py     # rewrites both files
```

Commit both. `--check` is what CI runs, so a package edited without a
re-anchor fails the build.

## Packages are shipped as they were measured

Every package except `_base` is a research artefact: the output of a campaign,
and the input to a reported number. Everything an arm can reach is committed
byte-for-byte, including the fragments of Chinese prose the meta-agent wrote
into some cards, manifests and comments. Translating those would produce a
repository that is tidier and results that no longer correspond to any package
we ran, so the repository's no-Chinese rule covers source code and
documentation and deliberately stops at this directory.

One deliberate exception, made when preparing the release. The SkillFlow
package also held twelve files that no routing policy can select: superseded
universal templates, short-form duplicates of per-family templates, one arm of
a three-way ablation, and six prose fragments that no template includes. They
were authoring residue rather than memory, and no arm could load them, so they
are not here. `recuris skillflow render-configs` resolves every one of the
twenty task families under both routing policies, and an unknown family still
falls back to `sf-universal.j2`.

`_base` is the exception in both directions: it is a template rather than a
measured artefact, it appears in no reported number, and it is written in
English because people are meant to copy it.
