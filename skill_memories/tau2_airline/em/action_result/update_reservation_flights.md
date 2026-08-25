---
id: update_reservation_flights
type: action_result
trigger: {event: pre_write, tool: update_reservation_flights}
source: "airline policy.md modify-flight rules; wrong-write analysis 2026-07-05"
---
Purpose: change the flight segments (and/or cabin) of one reservation.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "move my outbound to the next day."
  1. get_reservation_details("RID000001") shows cabin "economy" (NOT basic_economy),
     two segments.
  2. Correct call: update_reservation_flights(reservation_id="RID000001",
     cabin="economy", flights=[<ALL segments of the NEW reservation, even the
     unchanged return leg>], payment_id="credit_card_XXXX")

Rules (check BEFORE calling — the API does NOT enforce them):
- Basic economy flights CANNOT be modified (a cabin upgrade is still allowed, but
  changing the flight segments is not).
- Origin, destination, and trip type must stay the same.
- `flights` must list the ENTIRE new reservation — include unchanged segments too,
  or they are silently dropped.
- payment_id must be a SINGLE gift card or credit card already in the profile.
  A certificate is rejected ("Certificate cannot be used to update reservation").

Common mistakes (with why):
- Calling this on a basic_economy reservation — forbidden; the API mutates anyway.
- Omitting the unchanged return leg from `flights` — drops that segment.
- Using a certificate as payment_id — the tool errors.
- Changing origin/destination — not allowed.
