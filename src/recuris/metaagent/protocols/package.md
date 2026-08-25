# Native Skill Memory package contract

A package contains `manifest.yaml` and optional Markdown cards under
`em/<type>/`. The allowed manifest sections are `name`, `wm`, `grounding`,
`delivery`, `checkers`, `gate`, `feasibility`, and `board`.

- Card types are `knowledge`, `procedure`, and `action_result`.
- Each card has YAML frontmatter with `id` and `type`.
- An `action_result` card also declares `trigger.event: pre_write` and an exact
  `trigger.tool`; it is paired with `exemplar_bounce`.
- A `procedure` card declares `trigger.event: intent_recorded` and an exact
  `trigger.tool`. In formal autonomous runs, need-driven retrieval is tool-scoped,
  capped to one delivery per card per episode, and non-settling.
- Knowledge/procedure cards are paired with a retrieval-family or explicitly
  bounded injection deliverer; memory delivery must not masquerade as task completion.
- Delivery/checker entries use `- use: <builtin>` and optional `cfg` containing
  only that builtin constructor's parameters.
- Select only builtin names listed in the phase schema. If the runtime lacks a
  needed primitive, record the limitation; never invent a manifest key.
- Keep initialization minimal. Add mechanisms only when current evolve evidence
  establishes a need, then rely on the held-out gate for admission.
