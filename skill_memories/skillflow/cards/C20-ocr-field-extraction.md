---
id: C20
type: ocr-technique
trigger: "OCR nested/scanned document images and extract structured fields (document_ref / date / amount), often dedup by an extracted id, into an exact-match oracle sheet"
source: "SkillFlow autopsy 2026-07-07: OCR-Data-Extraction case_settlement_packets, nested_fuel_packets, utility_bills_template_update"
---
TRIGGER — extracting labeled fields (reference IDs, amounts, dates) from OCR text where the grader compares each cell against an oracle, or dedups on an extracted id.

TECHNIQUE
- Anchor each field regex to its SPECIFIC document label; validate the captured value against the expected shape (e.g. an id like ^[A-Z]{2}-[A-Z]-\d{3}$, an amount with 2 decimals). Reject/retry an implausible capture (a 2-letter fragment where an ID is expected) rather than writing it.
- Handle SPLIT layouts: when a label ("AMOUNT DUE") sits on one line and its value on the NEXT line, scan following lines; normalize comma thousands separators so a present value is never left blank.
- Canonicalize OCR-confusable glyphs in IDs before dedup/matching: O<->0, I<->1, S<->5. Exact-string dedup on un-normalized ids lets true duplicates survive and inflates the row count.

SELF-CHECK — before finishing: no extractable cell is blank; every captured id matches its expected format; and the final row count equals what the stated dedup rule implies. A single blank cell or extra duplicate row fails the exact oracle-diff.

SOURCE: case_settlement_packets (loose regex captured a 2-letter fragment where a structured ID was expected), nested_fuel_packets (O/0 glyph confusion left true duplicates uncollapsed, inflating the row count), utility_bills_template_update (amount on the line after the keyword + comma separators left the cell blank).
