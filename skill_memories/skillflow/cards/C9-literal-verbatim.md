---
id: C9
type: literal-fidelity
trigger: spec supplies a required field value or an "impact phrase" list; grader equality/substring-matches strings
source: DMAIC-Quality-Analysis (harbor_university_it_analyze_04, harbor_field_service_analyze_03, harbor_gdpval_35)
---

# literal-verbatim

TRIGGER — A field must hold a specific value form, or a narrative must contain phrases the grader greps for (e.g. `source_file`, an enumerated list of "operational impact" phrases, a `Decision:` slug).

TECHNIQUE
- Match the exact FORM the spec implies. `source_file` wants the BASENAME (`it_helpdesk_data.csv`), not the full path (`/root/it_helpdesk_data.csv`). Emit `os.path.basename(...)`.
- When the spec enumerates an allowed phrase list ("choose at least two from ..."), COPY those phrases character-for-character and case-sensitive (`escalation backlog for advisors`, `missed service-level commitments`) — do NOT paraphrase. Graders substring/equality-match; a paraphrase scores `assert 0 >= 2`.
- Include any mandated marker lines verbatim (`project codename:`, `Decision:`). Correct computation never rescues a mismatched literal.

SELF-CHECK — List every literal the spec mandates; for each, assert it appears verbatim in the output (basename form; each required phrase present with exact casing; `hit_count >= required_count`). Grep your own file the way the grader will.

SOURCE: DMAIC-Quality-Analysis — statistics passed but literals sank it: `assert '/root/it_helpdesk_data.csv' == 'it_helpdesk_data.csv'`, and paraphrased impacts gave `assert 0 >= 2`.
