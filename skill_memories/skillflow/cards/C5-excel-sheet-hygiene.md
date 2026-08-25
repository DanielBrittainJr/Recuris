---
id: C5
type: workbook-hygiene
trigger: workbook must have an exact set/order of sheets; multiple to_excel writes or a re-run build script
source: Inventory-&-Finance-Integration (new_task_10_maintenance_calcfields_restock)
---

# excel-sheet-hygiene

TRIGGER — The spec requires an exact sheet set/order (e.g. exactly `Part_Results`, `Additional_Resupply_Needed`), and your script writes metadata + data separately or you re-run the build.

TECHNIQUE
- Two `to_excel(writer, sheet_name="X")` calls with the SAME name make pandas auto-rename the second to `X1`, `X2`, ... . Re-running a writer over an existing file accumulates more (`X1..X7`).
- Write each sheet exactly ONCE into a single worksheet object. If metadata goes at `startrow=0` and data at `startrow=5`, target the SAME sheet (via `if_sheet_exists='overlay'` or by building one DataFrame/worksheet), not two `to_excel` calls.
- Always write to a FRESH output file each run (delete/overwrite the target first) so stale duplicate sheets don't survive a re-run.

SELF-CHECK — Reopen the file; assert `wb.sheetnames == <the exact required list, same order>`. Any `Name1`/`Name2` sheet means you duplicated a write — rebuild.

SOURCE: Inventory maintenance_restock — sheet set was `['Part_Results','Part_Results1',...,'Part_Results7','Additional_Resupply_Needed']` from double `to_excel` + four re-runs, failing the exact-sheet-set check.
