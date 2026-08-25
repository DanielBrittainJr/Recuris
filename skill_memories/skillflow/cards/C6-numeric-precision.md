---
id: C6
type: numeric-precision
trigger: computed cells compared with a tight tolerance (tol 1e-4) / compareCell / rounding not requested
source: Inventory-&-Finance-Integration (computed-field precision failures)
---

# numeric-precision

TRIGGER — A grader compares computed numeric cells within a tight absolute tolerance (e.g. 1e-4), or you are about to `round()` a value the spec never told you to round.

TECHNIQUE
- Do NOT round computed values unless the spec explicitly says so. Rounding to fewer places than the tolerance guarantees a miss.
- If the tolerance is 1e-4, keep at least 4 decimal places (ideally full float precision). Shipping `8.57` when the true value is `8.5714` fails `abs(diff) <= 1e-4`.
- Only apply the exact rounding the spec dictates (e.g. "round to 2 dp") and apply it to the stated fields only — don't over-round everything.

SELF-CHECK — For each tolerance-graded cell, confirm you kept enough decimal places that `abs(your_value - full_precision) < tolerance`. Reopen and spot-check that stored values are not truncated.

SOURCE: Inventory-&-Finance-Integration — computed cells were rounded below the 1e-4 comparison tolerance, missing the compareCell check.
