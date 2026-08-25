---
id: C17
type: domain-medical
trigger: "harmonize a clinical/lab CSV: detect alternate-unit values by physiological range and convert to US conventional units, round, drop rows"
source: "SkillFlow autopsy 2026-07-07: Medical-Data-Standardization icu-metabolic, neonatal-sepsis, thyroid-monitoring, cardio-panel-template, electrolyte-rounding harmonization"
---
TRIGGER — a spec says "convert only out-of-range / alternate-unit values" to conventional units and forbids residual out-of-range values.

TECHNIQUE
- GATE conversion on the TARGET-unit plausible range: for each analyte, convert a value only if it lies OUTSIDE the US-conventional normal range; leave already-in-range values untouched (do not scale a Troponin 0.02 up to 16).
- Get the direction right per analyte (multiply vs divide) using authoritative factors, not memory guesses — e.g. creatinine umol/L->mg/dL divides by ~88.4, glucose mmol/L->mg/dL multiplies by ~18, CRP mg/L<->mg/dL factor 10.
- Drop ONLY the rows the spec names (e.g. missing measurements). Never invent an extra "still out-of-range -> drop" rule, and never add conversions for fields the spec does not list.

SELF-CHECK — after conversion, assert EVERY output value falls inside its analyte's conventional range; a residual out-of-range value means a missed or wrong-direction conversion. Confirm final row count equals the spec's stated drop rule, and spot-check that a known-normal sample was left unchanged.

SOURCE: icu-metabolic (Ca/Mg residual out-of-range), neonatal-sepsis (creatinine not converted), thyroid-monitoring (invented ranges + extra drop rule -> 3 of 11 rows), cardio-panel & electrolyte-rounding (over-converted in-range / wrong direction).
