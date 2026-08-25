---
id: C22
type: domain-age
trigger: "a field asks for age 'as of <date>' or Korean 'full-year age' / 만 나이 computed from a birth date"
source: "SkillFlow autopsy 2026-07-07: HWPX-Document-Automation hwpx-clinic-intake-summary"
---
TRIGGER — filling an age field relative to a reference/visit date, especially "full-year age" or 만 나이.

TECHNIQUE
- "Full-year age" / 만 나이 = COMPLETED years as of the reference date = ref_year - birth_year, MINUS 1 if the birthday has not yet occurred in the reference year.
- This is the international/completed-age convention. Do NOT use the traditional East-Asian counting age (birth = 1, +1 each new year), which over-counts by one or two.
- Compare month/day: if (birth_month, birth_day) > (ref_month, ref_day), subtract one.

SELF-CHECK — verify the birthday-not-yet-reached branch: if the birthday falls later in the reference year than the reference date, the answer must be (ref_year - birth_year - 1), and it must be strictly less than the naive (ref_year - birth_year + 1) counting-age value.

SOURCE: hwpx-clinic-intake-summary (agent used traditional Korean counting age — birth-year + 1 convention — when the field wanted full-year 만 나이 as of the visit date, with the birthday not yet reached that year).
