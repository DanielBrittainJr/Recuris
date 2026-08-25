---
id: C7
type: copy-fidelity
trigger: "'copy the source exactly' / preserve RawData / mirror an input sheet or column"
source: Distribution-Center-Auditing (source-copy fidelity failures)
---

# copy-fidelity

TRIGGER — The spec says to copy/preserve a source sheet or column "exactly", mirror RawData, or carry input values through unchanged.

TECHNIQUE
- pandas silently mutates on read: it turns `"N/A"`/blank into NaN and parses date-looking strings into Timestamps. That breaks an exact copy.
- Read with `keep_default_na=False` and `dtype=str` (or `na_filter=False`) so text like `'N/A'` stays the literal string and numbers/dates keep their source form.
- For a true cell-for-cell copy, prefer openpyxl cell-value copy over a pandas round-trip, so types and formatting are not coerced.
- Do not reformat, re-type, or "clean" copied values unless the spec asks for it.

SELF-CHECK — Reopen the output; spot-check that preserved cells match the source byte-for-byte: `'N/A'` is still text (not blank/NaN), dates are still the original string (not a Timestamp), leading zeros survive.

SOURCE: Distribution-Center-Auditing — pandas coerced copied source values (`'N/A'` → NaN, dates → Timestamps), so the "copy exactly" checks failed.
