# Experiential memory for `_base` (empty; fill it with your own cards)

One card is one Markdown file. Adding knowledge means adding a file — there is
no index to update and no code to write.

## Card format

This frontmatter is the only contract you have to honour when adapting the
package to a new task:

```markdown
---
id: <unique id; matching the filename is the convention>
type: knowledge | procedure | action_result
trigger:            # optional: self-directed retrieval does not need it,
                    # event-triggered delivery does
  event: pre_write | turn_start | ...
  tool: <tool name, or "*">
source: "<where this came from: which failure post-mortem, which document>"
---
The body, in Markdown. This is what the model sees.
```

## Card types

- `knowledge` — facts, policies, formulas. The main target of self-directed
  retrieval.
- `procedure` — processes and checklists.
- `action_result` — worked examples of an action plus the mistakes that are
  common around it. The main target of event-triggered delivery. Write these
  with abstract placeholder identifiers so the model does not anchor on the
  specific values in the example.

## The one prohibition

Never write a task's answer into a card. A card carries how to approach a class
of situation; a card that carries the answer to a held-out task turns a
measurement into a leak.
