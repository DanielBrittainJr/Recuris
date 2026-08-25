---
id: update_reservation_passengers
type: action_result
trigger: {event: pre_write, tool: update_reservation_passengers}
source: "airline policy.md passenger rules; wrong-write analysis 2026-07-05"
---
Purpose: edit passenger identities on one reservation (SAME count only).

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "fix the spelling of the second passenger's name."
  1. get_reservation_details("RID000001") shows 3 passengers.
  2. Correct call: update_reservation_passengers(reservation_id="RID000001",
     passengers=[<all 3, same count, with the corrected name>])

Rules:
- The NUMBER of passengers CANNOT change — not even a human agent can. Adding or
  removing a passenger errors "Number of passengers does not match".
- Each passenger = {first_name, last_name, dob}. Provide ALL passengers, not just
  the changed one.

Common mistakes (with why):
- Trying to add/remove a passenger — hard error; suggest a new booking instead.
- Sending only the edited passenger — the count then mismatches.
