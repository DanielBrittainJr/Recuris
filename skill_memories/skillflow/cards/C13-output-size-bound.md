---
id: C13
type: output-constraint
trigger: deliverable states an explicit size limit ("4 to 8 non-empty lines", max N words)
source: Healthcare-Cost-Benefit-Analysis (harbor_oncocooler_10v20)
---

# output-size-bound

TRIGGER — A deliverable states an explicit size bound: "4 to 8 non-empty lines", "no more than N words", "a short summary".

TECHNIQUE
- Take the bound literally and count the FINISHED file. "Non-empty lines" = every line with visible content, INCLUDING markdown headings (`# Title`, `## Key Findings`) and label lines.
- Decorative structure counts against the budget. Adding `# Summary`, `## Findings`, `## Decision` on top of the required content lines is what pushes a compliant body over the cap.
- If over the limit, trim decorative headings/blank-but-styled lines first; keep the required content lines. Correct content over the line cap still fails.

SELF-CHECK — Reopen the finished file and count non-empty lines exactly as the grader will (`len([l for l in text.splitlines() if l.strip()])`). Assert it is within the stated `[min, max]`. If `9 <= 8`, delete heading lines until it fits.

SOURCE: Healthcare oncocooler — analysis passed; the summary had 9 non-empty lines (decorative headings added) against a "4 to 8" cap, failing `assert 9 <= 8`.
