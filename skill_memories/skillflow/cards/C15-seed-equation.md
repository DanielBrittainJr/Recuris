---
id: C15
type: seed-derivation
trigger: "a catch-up / backlog / rollforward plan whose first period gives an initial condition of the form 'Start of Week/Phase Past Due + Scheduled Demand = <constant>'"
source: "SkillFlow autopsy 2026-07-07: Production-Capacity-Planning (harbor_gdpval_36 family, tasks 1/3/4/6/7/8) and Operational-Recovery-Planning (harbor_gdpval_36, soc_alert_recovery, radiology_reading_backlog) — ~9 trials, the single highest-ROI fix in the corpus"
---
TRIGGER — the spec states a period-1 identity like "A + B = K" (Start-of-Period Past-Due/Queue + Scheduled Demand = a given total), then you roll a recurrence forward from that seed.

TECHNIQUE
- Read "A + B = K" as an EQUATION, not an assignment. B (first-period demand) is given, so solve A = K - (first-period demand). NEVER set Start = K.
- Setting Start = K double-counts the first period's demand; because End feeds the next Start, that single error cascades and corrupts every downstream row.
- If demand itself is derived (e.g. Effective Demand = base + adjustment), compute B first, then back out A.

SELF-CHECK — before propagating, assert the row-1 identity holds: abs((Start_1 + Demand_1) - K) <= tolerance. If Start_1 + Demand_1 != K, you seeded wrong — fix it before running the plan. Do not "verify" against your own misreading.

SOURCE: harbor_gdpval_36 tasks 1/3/4/6/7/8, new_task_1_soc_alert_recovery, new_task_2_radiology_reading_backlog (each: agent set Start = total, off by exactly first-period demand).
