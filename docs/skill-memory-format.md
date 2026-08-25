# The Skill Memory package format

A package is a directory. Its `manifest.yaml` declares the four components of
`M = (E, W, ρ, C)`; its `em/` holds the cards, one per file. Loading it is
`recuris.skillmemory.load_skill_memory(path)`.

The format is deliberately boring. Adding knowledge should be adding a file,
and the meta-agent should be able to write a valid package without writing
code. Everything that could be data, is.

## The manifest

```yaml
name: tau2_retail

wm:                                    # W: the working-memory contract
  entry_kind: service_request_auth
  manager: self_maintain_each_turn
  schema:
    binding_key: order_id              # what an entry is keyed on
    collection_key: item_ids
    max_entries: 8
    board_marker: "--- Progress (system-verified)"

grounding:
  matcher: receipt_binding_match       # how a real receipt settles an entry

delivery:                              # rho: when a card reaches the model
  - use: exemplar_bounce
    cfg: {em_type: action_result, refusal_branch: true}
  - use: state_reminder

checkers:                              # C: what must hold before a claim
  - use: truth_protocol
    cfg: {level: full}

board:
  status_board: true
  stay_notice: true
  notice_in_wm: always

gate:
  lines: all_pending                   # or authorized_only
```

Every `use:` names a builtin from `recuris.builtin`, or a `local:` class in the
package's own `plugin.py`. Its `cfg` is validated against that class's
constructor at load time, so a misspelled option is a startup error rather than
a silently disabled mechanism.

## Cards

One card, one Markdown file, under `em/<type>/`:

```markdown
---
id: authorized_execute_check
type: procedure
trigger:
  event: intent_recorded
source: "v3 challenger design 2026-07-05"
---
[STATE REMINDER — from the system] The customer has AUTHORIZED the requests
below, verified against their own words. Before executing each one, check it
against policy: if it is NOT allowed, explain the refusal instead. If it IS
allowed, EXECUTE the tool call in THIS turn — a confirmation reply is not the
action itself.
```

| Field | Meaning |
|---|---|
| `id` | unique; matching the filename is the convention |
| `type` | `knowledge`, `procedure`, or `action_result` |
| `trigger` | optional. Self-directed retrieval needs none; event-triggered delivery does |
| `source` | where this came from: which post-mortem, which document |

The three types are not decoration; they route differently.

- `knowledge` — facts, policies, formulas. The main target of self-directed
  retrieval.
- `procedure` — processes and checklists.
- `action_result` — a worked example plus the mistakes common around it. The
  main target of event-triggered delivery. Write these with abstract
  placeholder identifiers, so the model does not anchor on the example's
  specific values.

`source` is not bookkeeping. A card whose provenance nobody can state is a card
nobody can decide to remove.

## The one prohibition

**Never write a task's answer into a card.** A card carries how to approach a
class of situation. A card carrying the answer to a held-out task turns a
measurement into a leak, and the leak scan in the loop rejects the obvious
forms of it outright.

## Adding a package

Copy `skill_memories/_base`, fill in `em/`, and you have a working package with
no Python at all. That is the point of `_base`: it is generic working memory
plus self-directed retrieval, and a task that needs only "retrieve the relevant
card" needs nothing else.

Write `plugin.py` only for domain *rulings* — judgements about the world that
must not be delegated to the model. The airline feasibility oracle is the
example: it reads objective fields out of real receipts and rules BLOCKED, and
it lives in the package because domain policy is domain code, reviewed by a
human, never in the kernel.

## The protected set

`skill_memories/champions.lock.json` records the exact byte content of the
packages that must not change. `integrity/anchors.json` holds the aggregate it
must match. The campaign driver verifies both before every round, so a settled
package cannot be edited without the run stopping.

To change one deliberately:

```bash
python scripts/reanchor_integrity.py          # rewrites both files
python scripts/reanchor_integrity.py --check  # verifies, writes nothing
```

Commit both files it rewrites.

## Packages are shipped as they were measured

Every package except `_base` is a research artefact: the output of a campaign,
and the input to a reported number. They are committed byte-for-byte, including
fragments of Chinese prose the meta-agent wrote into some cards and manifests.
Tidying them would produce a repository whose packages no longer correspond to
anything we ran. The repository's no-Chinese rule covers code and
documentation, and stops here on purpose.
