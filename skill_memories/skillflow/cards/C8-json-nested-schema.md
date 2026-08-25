---
id: C8
type: schema-fidelity
trigger: schema-graded JSON where tests index nested sub-keys (metrics.json, tollgate report)
source: DMAIC-Quality-Analysis (harbor_devops_pipeline_02, harbor_gdpval_35)
---

# json-nested-schema

TRIGGER — You emit a JSON report and the grader indexes NESTED sub-keys, not just top-level containers (e.g. `metrics["build_duration"]["n"]`, `ranking[i]["process"]`, `momentum_plan["day_30"]`).

TECHNIQUE
- Reproduce every nested SHAPE, not just the named top-level keys. If the grader does `[r["process"] for r in ranking]`, `ranking` must be a list of `{"process":..., "cv":...}` objects — a list of strings raises `TypeError: string indices must be integers`.
- Include every sub-key the grader will index: per-block `n` / `points` / `rows`; a `day_30` / `day_60` / `day_90` dict; snake_case labels that exactly equal the top-level key names.
- Infer required sub-keys from the acceptance criteria / example schema and emit them verbatim, even when their value is trivial. A missing sub-key is a `KeyError`, which is a hard zero.

SELF-CHECK — Reopen the JSON; walk each container the spec mentions and assert its inner keys/types match (list-of-dict vs list-of-str, presence of every `n`/`points`/`rows`/`day_*`). Mentally run the grader's indexing on your object.

SOURCE: DMAIC-Quality-Analysis — correct numbers, wrong shape: `KeyError 'n'`, `KeyError 'points'`, `KeyError 'rows'`, `TypeError: string indices must be integers` on a list-of-strings ranking, missing `day_30/60/90`.
