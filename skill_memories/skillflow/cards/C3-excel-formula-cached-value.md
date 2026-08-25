---
id: C3
type: spreadsheet-caching
trigger: spec mandates formulas (INDEX/MATCH, SUMPRODUCT) saved to xlsx but grader reads computed values / data_only
source: Weighted-Risk-Assessment (bedflow, port-throughput, api-sla, +cloud-reliability, campus-energy), Embedded-Data-Repair (emission-zone, fx-cross-rate, rebate-band), Compensation 03_university_faculty
---

# excel-formula-cached-value

TRIGGER — The spec says cells must be spreadsheet FORMULAS, but the grader reads their numeric results (e.g. `data_only=True`). openpyxl writes `<f>` formula text with NO cached value, so `data_only` returns None and every value test fails.

TECHNIQUE
- After writing each `<f>` formula, also inject the computed `<v>` cached value into the sheet XML (compute the number in Python, write both `<f>...</f>` and `<v>result</v>` on the cell). This satisfies BOTH the "must be a formula" structural test and the value test.
- Any openpyxl edit-and-save STRIPS cached values from ALL formula cells (even untouched dependents like a reciprocal). After editing, re-inject `<v>` on every affected cell, or recalc with LibreOffice headless (`--convert-to xlsx`) if available.
- Cached values also make grading independent of any recalculation engine: headless recalc tools (e.g. `ssconvert --recalculate`) are often unavailable or broken inside task images, so a workbook whose formula cells already carry computed `<v>` values is the only install-free way to satisfy a value check.

SELF-CHECK — Reopen with `data_only=True`; assert each graded cell returns a NUMBER (not None, not the formula string). Verify the value, not just that the formula text is present.

SOURCE: Weighted-Risk + Embedded-Data-Repair + Compensation — correct formulas scored zero (graded cells read as None because no cached value was stored).
