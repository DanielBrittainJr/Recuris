---
id: C12
type: report-content
trigger: "a companion narrative deliverable (Word/Markdown brief) that must 'mention the top-N X' or cite high-priority items/facts"
source: "SkillFlow autopsy 2026-07-07: Distribution-Center-Auditing harbor_cycle_count_variance_audit, harbor_service_queue_sla_audit"
---
TRIGGER — the task pairs a computed table with a prose brief (docx/md), and the spec says the brief must name the top-N entities, high-priority items, or key figures.

TECHNIQUE
- Graders grep the narrative for SPECIFIC entity names/values, not for good writing. Generic sentences like "several high-priority queues need attention" score zero.
- Compute the actual ranked entities from your own output table (top-N by the stated metric — e.g. by total error, by variance), then paste those exact names/IDs and their totals into the prose.
- Meet the stated count with margin: if it asks for "at least two", name three real ones.

SELF-CHECK — re-open the finished brief and grep it for each computed top-N name; assert the count of matches >= the required minimum before declaring done. Prose that names zero data-derived entities is a guaranteed fail.

SOURCE: harbor_cycle_count_variance_audit ("mention at least two high-priority facility-session combinations, assert 0>=2"), harbor_service_queue_sla_audit ("mention at least two high-priority queues, assert 0>=2").
