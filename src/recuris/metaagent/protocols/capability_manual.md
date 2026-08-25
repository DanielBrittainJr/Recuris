# Recuris Public Capability Contract

This is the public interface for an autonomous Meta-Agent. It describes
configuration and behavior, not implementation. Use only named mechanisms; do
not create plugin code, custom strategies, or runtime primitives.

## 1. Package contract

A package contains `manifest.yaml` and an `em/` Markdown library:

```yaml
name: synthetic_skill_memory
wm:
  entry_kind: service_request
  manager: self_maintain_each_turn
  manager_cfg: {}
  schema:
    binding_key: object_id
    collection_key: item_ids
    max_entries: 8
    machine_id_pattern: ".*_ids?"
grounding: {matcher: receipt_binding_match}
delivery:
  - use: <builtin_deliverer>
    cfg: {}
checkers:
  - use: <builtin_checker>
    cfg: {}
gate: {lines: all_pending}
board: {}
```

Allowed top-level keys are `name`, `wm`, `grounding`, `delivery`, `checkers`,
`gate`, `feasibility`, and `board`. `wm.schema` accepts `binding_key`,
`collection_key`, `max_entries`, `machine_id_pattern`, and `board_marker`.
`gate.lines` is `all_pending` or `authorized_only`. Unknown section keys do
nothing; unknown constructor keys fail loading.

Matchers are `receipt_binding_match` (requires `wm.schema.binding_key`; settles
matching pending work from real successful writes) and `delivery_receipt`
(settles the exact retrieval target when `settle: true`). Optional binding-level
`at` must be a role-valid event; do not move builtins unless explicitly allowed
below. `feasibility` may only select an already supplied reviewed oracle. Do not
add executable files or local strategies.

## 2. Card contract

Every loaded card is a `.md` file below `em/` (except `README.md`) with YAML
frontmatter:

```markdown
---
id: <snake_case_card_id>
type: knowledge | procedure | action_result
trigger:
  event: <runtime_event>
  tool: <exact_tool_name>
source: metaagent_generated
---

Reusable rule, decision boundary, procedure, or simulated call.
```

`procedure` and `action_result` require an exact event and non-wildcard tool.
Action results use `pre_write`; retrieval procedures use `intent_recorded`.
These defaults assume a runtime that raises those events — the adapter
extension for the current benchmark is authoritative about which events its
runtime actually raises, and a binding to an un-raised event never fires.
Adapter-declared checker routes may define additional card/event pairings.
Knowledge may omit routing only when its carrier does not match event/tool
metadata. `standing_inject` knowledge declares `turn_start`; boundary-injected
knowledge declares the configured event/tool pair. Bodies must be reusable and
use placeholders such as `<OBJECT_ID>`—never records, identifiers, answer
instances, or outcomes.

## 3. Formal TurnRuntime event model

Turn order: admit prior receipts -> deliver `turn_start` -> maintain WM ->
deliver `intent_recorded` after a successful update -> draft -> check
`draft_ready` (at most one correction/redraft) -> internal commit -> deliver
`pre_write` for mutating calls (at most one review/redraft) -> sanitize/output.

Role-valid events are deliverer=`turn_start|intent_recorded|pre_write` and
checker=`draft_ready`.

This dispatch set describes the Tau2 conversation runtime. Other runtimes
raise a subset of it — the adapter extension section for the current benchmark
states which events are actually raised there, and that statement wins over
this section. A binding whose event the current runtime never raises passes
schema validation but is a silent no-op in every episode; the activation
referee rejects such bindings.

The wider event vocabulary also names `message_out`, `terminal_boundary`,
`agent_declares_done`, and `pre_answer`, but formal TurnRuntime does not dispatch
them. A generated package must not bind a builtin to those events.

`pre_write` sees only `mutates_state: true` tools, never read/generic decisions.

## 4. Working Memory

### Entry kinds

- `service_request`: stores `description`, `tool`, and `params`. It preserves a
  proposed tool only when that tool is in the runtime's mutating-tool set.
- `service_request_auth`: adds a verbatim authorization proposal. The harness
  verifies it against genuine user text; the model cannot self-authorize.
- `generic_item`: stores only `description`; it drops tool and parameter metadata.
- `knowledge_need`: stores `description`, `function` (`knowledge`, `procedure`,
  or `action`), and `needs_external`; it drops tool metadata.

Only the two service-request kinds carry tools.

### Managers

- `self_maintain_each_turn`: defaults `template: ""`, `temperature: 0.0`;
  re-derives WM on each non-empty genuine user turn.
- `blueprint_then_delta` has the same defaults. It creates a first-turn blueprint
  and revises it on later user/tool turns while work remains.

Empty `template` selects the entry kind's builtin format.

## 5. Deliverers

### `exemplar_bounce`

Default event: `pre_write`.

```yaml
cfg:
  em_type: action_result       # default
  refusal_branch: false        # default
  exact_only: false            # default; formal new cards must set true
```

It reviews matching mutating draft calls before execution and asks the worker to
resubmit, correct, or take the enabled refusal branch. Review results are
synthetic, never execution evidence. Reachability requires the configured card
type, `pre_write`, and an exact mutating tool. New action-result routes require
`exact_only: true`. This is draft-wide: one uncovered mutating call prevents the
whole bounce.

### `need_driven_retrieval`

Default event: `intent_recorded`.

```yaml
cfg:
  em_types: [knowledge, procedure]  # default
  top_k: 1                          # default
  min_overlap: 2                    # default
  settle: true                      # default
  scope_by_tool: false              # default
  max_per_episode: 0                # default; 0 means unlimited
```

It ranks cards for each pending description by lexical overlap with card ID/body
and injects `top_k`. Scoped knowledge/procedure requires card tool = entry tool;
missing metadata is not wildcard. Generated procedures use exact tools, event
`intent_recorded`, `scope_by_tool: true`, `max_per_episode: 1`,
`settle: false`, a tool-preserving entry kind, and a mutating tool. `settle:
true` closes the WM entry on delivery, so it is unsafe for unfinished service
actions. `top_k` must be positive.

### `embedding_retrieval`

Default event: `intent_recorded`.

```yaml
cfg:
  embedder: doubao_vision             # default
  em_types: [knowledge, procedure]    # default
  top_k: 2                            # default
  min_sim: 0.30                       # default
  head_chars: 300                     # default
  settle: true                        # default
  scope_by_tool: false                # default
  max_per_episode: 0                  # default
```

It has the same scope, deduplication, settlement, and safe procedure settings,
but ranks embedded pending descriptions against card IDs/body prefixes.
`top_k,head_chars > 0`, `max_per_episode >= 0`, and numeric `min_sim <= 1`.

### `standing_inject`

Default event: `turn_start`.

```yaml
cfg:
  em_type: knowledge   # default
  tool: "*"            # accepted default
```

It injects every matching-type `turn_start` card every turn. `tool` is not an
effective filter. Reserve it for universal content; regression risk is high.

### `state_reminder`

Default event: `intent_recorded`.

```yaml
cfg:
  unconfirmed_doc: confirm_before_execute       # default card ID
  authorized_doc: authorized_execute_check      # default card ID
```

It injects unconfirmed or authorized-execute reminders plus affected pending
descriptions. Custom text is selected by exact card ID; otherwise builtin text
is used. The authorized branch needs an authorization-capable entry kind.

### `boundary_inject`

Its event is configured rather than fixed.

```yaml
cfg:
  at: turn_start       # default
  when: always         # default: always | first_turn | has_pending
  em_type: procedure   # accepted default
  tool: "*"            # default
  max_cards: 1         # default; cards per firing
```

It injects event/tool matches (then wildcard fallback) when the predicate
holds. `max_cards` caps how many; the default `1` delivers only the first, so
extra cards here reach nobody until raised. Selection does not use `em_type`. Use only `turn_start` or
`intent_recorded`; `pre_write` consumes bounce actions, not notes. Omit
binding-level `at`, or make it equal `cfg.at`.

## 6. Checkers

Checkers run in manifest order at `draft_ready`. A checker returns either no
action or a correction `Bounce`; only the first bounce is applied per turn.
Checker names, constructor fields, observable state, and any checker-carried
card routes are supplied by the current benchmark adapter. A package may use
only checkers present in that adapter capability extension.

## 7. Hard reachability rules

Every card must ignite through at least one configured carrier:

- `action_result` -> `exemplar_bounce` at `pre_write`;
- intent-routed `procedure` -> lexical or embedding retrieval;
- universal `turn_start` content -> `standing_inject` or a matching boundary;
- named authorization reminder -> `state_reminder`;
- checker-routed card -> the exact route declared by the current adapter.

An exact tool must appear in the supplied tool-capability profile.
`pre_write` and tool-scoped retrieval require `mutates_state: true`.
Tool-scoped retrieval additionally requires a tool-preserving entry kind.
Constructor-valid configurations that cannot emit an action are still invalid.
The card metadata must match the carrier; configuring a carrier alone is not
proof that a metadata-free card can ignite.
Offline ignition proves wiring only; downstream evaluation determines
usefulness.

## 8. Fully synthetic examples

The following names and objects are fictional.

### A. Retrieve a scoped procedure

```markdown
---
id: configure_device_safely
type: procedure
trigger:
  event: intent_recorded
  tool: set_device_mode
source: synthetic_example
---
Before changing a mode: identify the device, verify the requested mode is
supported, obtain any required authorization, then call the tool with only
established placeholder-derived arguments.
```

```yaml
wm:
  entry_kind: service_request
  manager: self_maintain_each_turn
delivery:
  - use: need_driven_retrieval
    cfg:
      em_types: [procedure]
      top_k: 1
      min_overlap: 2
      settle: false
      scope_by_tool: true
      max_per_episode: 1
```
