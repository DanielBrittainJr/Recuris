---
id: C1
type: template-fidelity
trigger: line items "start at row N>1" / rollforward / reconciliation / detail sheets / "same structure as reference"
source: Financial-Statement-Rolling (peregrine_rebate, atlas_refund_reserve, solstice_commission, cedar_accrual) + Inventory transit_subsidy_rollforward
---

# excel-a1-header-block

TRIGGER — A spec says detail/line items "start at row 6" (any row N>1), or "keep the same structure as the reference reconciliation," or you build a rollforward/reconciliation workbook from a template.

TECHNIQUE
- Rows 1..N-1 are a REQUIRED header band on EVERY detail sheet (not only Summary). The grader checks each header cell against an exact string and `raise SystemExit` on the first mismatch, which shows as INTERNALERROR and masks the WHOLE suite before any numeric test runs — so one wrong header cell zeros an otherwise-correct workbook. Fill the full band, not just A1:
  - `A1` = the company/organization name (verbatim as the spec gives it).
  - `A2` = the sheet TITLE. Financial rollforward/reconciliation sheets conventionally title as `"<this sheet's own name> as of <period-end date>"` (e.g. the detail sheet named in the spec, then the period ending). Compose it from the sheet's exact name plus the period-ending date, writing the DATE in the same format the source package / output filename uses (don't reformat it). If the spec also names a subtitle row (A3), fill it likewise.
- If a template file is provided, open and edit it IN PLACE, writing data from row N down. Do NOT `create_sheet` fresh — that drops the template's header band.

SELF-CHECK — Reopen the saved file; for EACH detail sheet assert `ws["A1"]` equals the company name AND `ws["A2"]` equals the composed title (and any A3 subtitle) — none blank, none reformatted. A wrong A2 raises the SAME SystemExit as a wrong A1.

SOURCE: Financial-Statement-Rolling hard-zero family + Inventory transit_subsidy_rollforward — every trial sank on a blank detail-sheet A1 ("SystemExit: FAIL: Channel Rebates #6120!A1 company mismatch").
