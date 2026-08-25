---
id: C4
type: excel-pivot-technique
trigger: spec asks for a native Excel PivotTable; grader inspects workbook[sheet]._pivots
source: Sales-Pivot-Analysis (quality-control-pivot, student-performance-pivot, budget-reconciliation-pivot)
---

# excel-pivot-xml

TRIGGER — The task asks for one or more native Excel "pivot table" sheets, and grading reads `pivots = workbook[sheet_name]._pivots`.

TECHNIQUE
- A real PivotTable is a pair of XML parts: a pivotCache (definition + records) plus a pivotTable definition, wired into the sheet. It is NOT a computed table.
- pandas `groupby` / `pivot_table` / `crosstab` written via `to_excel(...)` produces ordinary STATIC cells — `_pivots` stays empty and every pivot check scores zero (`No pivot table found ... assert 0 > 0`, then `IndexError` on `_pivots[0]`).
- openpyxl cannot synthesize a pivotCache from scratch. Hand-author the pivotCache/pivotTable XML parts (or use a library/template that emits real PivotTable XML). Still write the enriched SourceData sheet normally — that part is graded separately and usually passes.
- This is the heaviest, highest-execution-risk card; get one pivot sheet working end-to-end first, then replicate.

SELF-CHECK — Reopen the workbook; for each required pivot sheet assert `len(workbook[sheet]._pivots) > 0` and that `_pivots[0]` exists. Static cells that "look like a pivot" do not count.

SOURCE: Sales-Pivot-Analysis hard-zero family — pandas aggregates dumped with `to_excel` left `_pivots` empty across every pivot sheet.
