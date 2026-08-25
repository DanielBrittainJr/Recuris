---
id: C19
type: quant-validation
trigger: "a chained econometric pipeline (deflate by price index -> log -> HP filter lambda=100 -> Pearson correlation) judged to 5 dp with a tight ~0.001 tolerance"
source: "SkillFlow autopsy 2026-07-07: Industry-Correlation-Analysis econ-equipment-software, econ-housing-materials, econ-service-hospitality, econ-wholesale-packaging correlation"
---
TRIGGER — a multi-step deflate/log/HP-filter/correlate task where the oracle tolerance is tight (~1e-3) and there is no reference to check against.

TECHNIQUE
- A clean run with no exceptions is NOT a correctness proof. These pipelines land ~0.004 off from one un-pinned convention.
- Pin every convention explicitly: deflator base/year alignment, the partial-2025 rule (average-of-available-quarters AND deflate-vs-aggregate ORDER), HP lambda, and alias->canonical dedup by the exact (series, period)+priority key.
- Validate intermediate series before the final stat: expected observation count, full year coverage (e.g. 1994/1995-2025), no duplicate years, and a plausible spot value for the annualized 2025 point.
- Recompute the final coefficient a second way (e.g. alternate 2025 handling) and reconcile before committing.

SELF-CHECK — do not accept the first number that runs. Assert the intermediate invariants above, then confirm two independent computations agree within tolerance; only then write the answer.

SOURCE: econ-equipment-software, econ-housing-materials, econ-service-hospitality, econ-wholesale-packaging correlation — each a plausible-looking pipeline that landed a few thousandths outside the 0.001 tolerance from one un-pinned convention (2025 partial-period, deflate-vs-aggregate order, or alias dedup).
