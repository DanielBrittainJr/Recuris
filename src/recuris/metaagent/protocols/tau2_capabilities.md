# Tau2 Adapter Capability Extension

This section is supplied only by the Tau2 adapter. It describes mechanisms
available in Tau2's multi-turn, tool-using runtime. These mechanisms are
capabilities, not diagnoses: select them only when the current trajectories and
policy establish that their observable conditions match the failure.

## Status board

`board.status_board: true` renders the harness-verified service-request ledger
after each assistant response. Pending work is shown as `NOT YET EXECUTED`; only
a successful mutating-tool receipt changes it to `DONE`.
`board.notice_in_wm` accepts `always`, `never`, or `when_board_enabled`.

`board.stay_notice: true` appends a harness-authored footer while executable work
remains. `board.stay_notice_text` may replace the default footer. This option is
specific to Tau2's user-facing conversation runtime.

## Checkers

### `truth_protocol`

```yaml
cfg:
  level: full          # default: full | v2only
  patterns: null       # default
```

At `draft_ready`, `full` compares a text-only draft with pending ledger state and
can reject an unsupported completion claim, a confirmation stall, or omission
of executable unfinished work. `v2only` checks only the confirmation condition.
Any draft containing a tool call bypasses it. Optional `patterns` contains only
the regex strings `claim`, `claim_neg`, `confirm_act`, and `status_ack`. Its
default patterns target Tau2 customer-service English. Executable-state checks
need a tool-preserving entry kind.

### `execution_gate`

It has no configuration (`cfg: {}`). At `draft_ready`, it rejects a text-only
draft when pending ledger work has both a mutating tool and harness-verified
authorization. Any draft tool call, or the absence of an authorized executable
entry, makes it inert. It therefore normally requires
`wm.entry_kind: service_request_auth`.

### `anti_escalation`

```yaml
cfg:
  transfer_tool: transfer_to_human_agents   # default
```

At `draft_ready`, it inspects draft tool calls. If the draft invokes
`transfer_tool` while a feasible pending service request remains, it rejects the
draft. If only policy-blocked requests remain, it also rejects the draft and
asks the worker to explain the denial. An empty ledger permits the transfer.

On rejection it appends procedure cards whose `trigger.event` is `draft_ready`
and whose exact `trigger.tool` equals `transfer_tool`. Because the checker reads
draft calls directly, this route does not require the transfer tool to mutate
state. Such a guide is reachable only when the matching checker is configured.

## Knowledge-heavy domains

Some Tau2 domains (for example `banking_knowledge`) put most of their difficulty
in knowledge-base retrieval and domain facts rather than in mutating-tool
discipline. Two structural facts matter when planning there:

- Knowledge-base search tools are read-only: they never raise `pre_write`, so
  `action_result` exemplars cannot target retrieval behavior, and a card whose
  substance is "query the KB differently" has no write event to ride.
- A `knowledge` card without `trigger` metadata is reachable **only** through an
  unscoped carrier. Tool-scoped retrieval (`scope_by_tool: true`) cannot carry
  it, and a plan whose only carriers are tool-scoped fails lint with
  `CARD HAS NO REACHABLE CARRIER`. The supported routes for such cards are
  `standing_inject` at `turn_start`, `boundary_inject`, or unscoped
  `need_driven_retrieval` / `embedding_retrieval` at `intent_recorded` — the
  last two fire when the ledger updates from a customer turn, which is when a
  stated customer need can select the matching knowledge card.

Preferring one of those routes is the intended design for knowledge gaps; do
not respond to the lint rejection by abandoning `knowledge` cards in a domain
whose failures are knowledge-shaped.
