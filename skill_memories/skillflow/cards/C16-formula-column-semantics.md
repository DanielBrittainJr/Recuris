---
id: C16
type: formula-semantics
trigger: "an Excel plan with formula columns (E/H/J) where a derived column such as 'Total Prod'/'Total Production' must be written as a formula referencing specific source columns"
source: "SkillFlow autopsy 2026-07-07: Operational-Recovery-Planning harbor_dc_recovery_01, harbor_gdpval_41, harbor_ag_recovery_03"
---
TRIGGER — a spec names a derived column (e.g. "Total Prod") and the grader asserts its FORMULA string references particular cell tokens (e.g. "J4 formula is missing token I4").

TECHNIQUE
- Map the column to its STATED MEANING before writing the formula. A "Total Production" column is the SUM OF ALL production columns (every commodity/line contributing production, e.g. C + F + I), never a subset and never a variance.
- Do not copy a similar-looking expression: not planned-minus-due (C+F)-(D+G), and not the cumulative-open-PO / running-balance columns (E + H).
- List every source column that logically feeds the derived column, then make the formula reference each one.

SELF-CHECK — for each derived-column formula, confirm the formula text contains a token for EVERY required source column (grep the produced cell's formula for each expected column letter). A total that omits a contributing column will fail the missing-token assertion.

SOURCE: harbor_dc_recovery_01 & harbor_ag_recovery_03 (J = (C+F)-(D+G), missing I), harbor_gdpval_41 (J = E+H, missing C) — all needed Total Prod = C+F+I.
