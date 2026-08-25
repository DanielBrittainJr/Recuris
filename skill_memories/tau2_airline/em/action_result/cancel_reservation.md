---
id: cancel_reservation
type: action_result
trigger: {event: pre_write, tool: cancel_reservation}
source: "airline policy.md cancellation rules; wrong-write analysis 2026-07-05"
---
Purpose: cancel one reservation.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "cancel my trip, change of plans."
  1. get_reservation_details("RID000001") shows: {"cabin": "economy",
     "created_at": "2024-05-01T10:00:00", "insurance": "no",
     "flights": [{"flight_number": "HAT###", "date": "2024-05-22"}]}
  2. Now = 2024-05-15 15:00. Booked 14 days ago (NOT within 24h), economy (not
     business), no insurance, flight not airline-cancelled → NOT cancellable.
  3. Correct action: do NOT call cancel_reservation. Tell the customer it cannot
     be cancelled and why.

Cancellation is allowed ONLY if (check BEFORE calling — the API does NOT enforce it):
- any segment already flown → cannot help, transfer to human; else allowed if ANY:
- booked within the last 24 hours, OR
- the flight was cancelled by the airline, OR
- the cabin is business, OR
- the reservation has insurance AND the reason is insurance-covered (health/weather).

Common mistakes (with why):
- Cancelling a change-of-plan economy/basic-economy booking outside 24h: forbidden
  by policy, but the API will cancel it anyway — this is the top over-execution error.
- Cancelling when a segment already flew: must transfer to a human instead.
- reservation_id not taken verbatim from get_reservation_details.
