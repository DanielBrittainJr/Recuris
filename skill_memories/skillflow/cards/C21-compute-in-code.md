---
id: C21
type: discipline
trigger: "a task whose formulas have several multiplicative factors (annualization, dose-per-day, revisions) and you are tempted to write the resulting numbers straight into JSON/cells"
source: "SkillFlow autopsy 2026-07-07: Healthcare-Cost-Benefit-Analysis harbor_infusionbatch_7v14"
---
TRIGGER — a multi-factor model (per-day dose x days x weeks x count / unit) where the final figures feed a graded JSON or workbook.

TECHNIQUE
- Implement each stated formula in a runnable script and EXECUTE it; read every reported number back from the code output. Never hand-compute in your reasoning and type the result in.
- Mental arithmetic silently DROPS factors — e.g. leaving out an annual x52 dose multiplier makes annual_drug_cost off by exactly 52, and every downstream margin/total inherits the error.
- Write the formula with every factor as an explicit variable so a missing one is visible in the code, not buried in a mental step.

SELF-CHECK — before finishing, confirm every graded number originated from executed code (not from reasoning), and re-read each spec formula token-by-token to verify no factor was omitted. If a magnitude looks too small/large by a round factor, you probably dropped one.

SOURCE: harbor_infusionbatch_7v14 (agent hand-typed annual_drug_cost into the JSON and silently dropped the annual x52 dose factor, making that value and every downstream margin/total wrong).
