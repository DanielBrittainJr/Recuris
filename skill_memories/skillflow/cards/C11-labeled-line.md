---
id: C11
type: report-format
trigger: "report must contain required fields as labeled lines; grader anchors on ^\\s*Label:"
source: Operational-Recovery-Planning (harbor_dc_recovery_01), Healthcare summary sections
---

# labeled-line

TRIGGER — A report/summary must expose required facts as labeled fields (`Actions:`, `Decision:`, `Root Cause:`), and the grader matches a line-start regex like `^\s*Actions:`.

TECHNIQUE
- Emit each required label as PLAIN text at the start of its line: `Actions: ...`. No markdown decoration on the label.
- Do NOT bold or wrap the label. `**Actions:**` inserts `*` before `Actions`, so `^\s*Actions:` does not match and the field reads as missing.
- Keep the colon immediately after the label word (`Label:`), value following on the same line or the next, per spec.
- One label per required field, spelled exactly as the spec gives it (case-sensitive).

SELF-CHECK — For each required field, run the grader's anchor mentally: does a line of your output start (after optional whitespace) with `Label:` and no leading `*`/`#`? If a label is bolded or indented under a heading, un-decorate it.

SOURCE: Operational-Recovery-Planning dc_recovery — labels were markdown-bolded (`**Actions:**`), so `^\s*Actions:` failed with "Scenario 1 is missing required field 'Actions:'".
