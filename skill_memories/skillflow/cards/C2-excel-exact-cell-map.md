---
id: C2
type: layout-discipline
trigger: spec pins named ranges / headers / blocks by explicit sheet!cell (compensation model, faculty model)
source: Compensation-Scenario-Modeling (01_orchestra_foundation, 02_orchestra_archive_refresh, 03_university_faculty)
---

# excel-exact-cell-map

TRIGGER — The spec (or a reference template) pins cells by explicit address: a title in B1, a header label at A3, data at A4:E106, named ranges targeting `Summary!C5`. Rebuilding a spec'd/reference workbook.

TECHNIQUE
- Do NOT free-form a plausible-looking layout. Build a checklist of EVERY required address before writing: title cell, each header label, data-start row, and each named-range target sheet!cell.
- Named ranges must point at the exact sheet the spec names (`MWS_Yr1 -> Summary!$C$5`, never `Assumptions!$G$5`). Header cells must hold the literal label string (`A3 == "Employee #"`, not `2` or a formula `=Roster!B7`).
- Do not skip the task by hunting for a nonexistent "xlsx skill"; anchor to coordinates and write directly.

SELF-CHECK — Reopen the file; for each checklist entry assert `produced_cell == expected_address_value` (title B1, header A3, roster row 4 == the seed tuple, each defined name's target). If your own verify script flags a mismatch, FIX the layout — never rationalize it as a "bad check."

SOURCE: Compensation-Scenario-Modeling hard-zero family — free-formed layouts failed every address ("Defined name MWS_Yr1 expected Summary!$C$5, got 'Assumptions'!$G$5"; "A3 expected 'Employee #'"), and one trial shipped anyway after watching its check fail.
