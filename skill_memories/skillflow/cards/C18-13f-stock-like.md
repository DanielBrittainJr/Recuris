---
id: C18
type: domain-13f
trigger: "SEC 13F analysis: resolve a manager query, filter INFOTABLE to 'stock-like holdings', aggregate VALUE by CUSIP, report AUM / top buys-sells / counts"
source: "SkillFlow autopsy 2026-07-07: SEC-13F-Financial-Analysis fund-shift-screen, fund-snapshot-canonical, cross-quarter-reconciliation, fund-class-breakdown, existing-brief-refresh"
---
TRIGGER — a 13F task asks for stock-like holdings only, and/or resolves a fund/manager by a name query across COVERPAGE/INFOTABLE.tsv.

TECHNIQUE
- "Stock-like" excludes DERIVATIVE rows where the PUTCALL column is populated (Put/Call). Filtering only on SSHPRNAMTTYPE=='SH' leaves options in and corrupts largest-buy/sell and counts — check PUTCALL AND the share/class fields.
- "Resolve/match a manager query" means FUZZY nearest-name match (e.g. rapidfuzz WRatio), not a literal string equality; disambiguate among multiple filers/amendments and pick the single correct accession before summing.
- Before trusting a substring class filter, print the distinct TITLEOFCLASS values and confirm the include/exclude rules capture the dominant equity encoding.

SELF-CHECK — treat these as RED FLAGS that force a re-derive, not shippable answers: an all-zero/empty result; an equity fund showing only ~11 of hundreds of holdings as stock; an AUM off by an order of magnitude (e.g. 60x) versus the filing total. Sanity-check stock count and stock AUM as a plausible fraction of the totals.

SOURCE: fund-shift-screen & cross-quarter-reconciliation (PUTCALL option rows not excluded), fund-snapshot-canonical (11/450 equity rows), fund-class-breakdown (literal 'elliott' grep -> all-zero shipped), existing-brief-refresh (wrong accession -> AUM 60x high).
