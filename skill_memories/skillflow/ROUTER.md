# ROUTER — Task-Prompt Signal to Card Map

TRIGGER: At task start, before writing any code, grep the task prompt for the signals below and load ONLY the matching card(s) into turn 1. Keeps context lean and avoids cross-family contamination. MACHINE.md is always on regardless of routing — the cards feed its pre-submit assertions.

TECHNIQUE — match the prompt substring, pull the card(s):

| task-prompt signal (substring) | card(s) |
|---|---|
| "pivot table" / native pivot / `_pivots` | C4 excel-pivot-xml |
| "starting at row 6" / detail sheets / rollforward / reconciliation | C1 excel-a1-header-block |
| named ranges / `Summary!` / `EE Calcs` / compensation model | C2 excel-exact-cell-map + C3 excel-formula-cached-value |
| INDEX/MATCH ... save to `result.xlsx` / yellow cells / SUMPRODUCT | C3 excel-formula-cached-value (inject cached `<v>`) |
| embedded xlsx inside pptx / reciprocal recompute | C3 excel-formula-cached-value |
| `.pptx` caption/restyle / "Arial N pt" / `#hex` / auto-numbered bullets | C14 pptx-xml-formatting |
| `*_metrics.json` + brief.md / tollgate / DMAIC / source_file | C8 json-nested-schema + C9 literal-verbatim |
| "Start of Week/Phase ... Past Due + Scheduled Demand = " | C15 seed-equation |
| "Total Prod" / cumulative POs column formula | C16 formula-column-semantics |
| summary must state totals / decision slug | C10 prose-number-token + C11 labeled-line |
| "copy the source exactly" / RawData | C5 excel-sheet-hygiene + C7 copy-fidelity |
| harmonize CSV / convert to conventional units | C17 unit-harmonization |
| 13F / INFOTABLE / CUSIP / manager query | C18 13f-stock-like |
| HP filter / Pearson correlation / deflate | C19 econ-pipeline-validation |
| OCR / extract ref/amount from images | C20 ocr-field-extraction |
| tolerance 1e-4 / compareCell | C6 numeric-precision |

SELF-CHECK: I grepped the prompt for these signals and loaded every card that matched (a task can hit several rows — e.g. a compensation model triggers C2 AND C3). If a signal matched, its card's SELF-CHECK line is now part of my pre-submit assertion list. If nothing matched, MACHINE.md still runs.

SOURCE: SkillFlow synthesis §3.2 (package ROUTER signal table), distilled from the 6 hard-zero families + mid-band trials.
