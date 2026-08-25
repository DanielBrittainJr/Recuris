---
id: update_reservation_baggages
type: action_result
trigger: {event: pre_write, tool: update_reservation_baggages}
source: "airline policy.md baggage rules; wrong-write analysis 2026-07-05"
---
Purpose: change the checked-baggage count of one reservation (ADD only).

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "add one more checked bag."
  1. get_reservation_details("RID000001") shows nonfree_baggages: 1.
  2. Correct call: update_reservation_baggages(reservation_id="RID000001",
     total_baggages=<current+1>, nonfree_baggages=<2 if beyond free allowance>,
     payment_id="credit_card_XXXX")
  Cost = 50 * max(0, new_nonfree - current_nonfree).

Rules:
- Bags can be ADDED but NOT removed. A request to reduce bags below the current
  count is not allowed (the API would still mutate — do not call it).
- payment_id: single gift/credit card in profile; certificate rejected.
- Count only bags BEYOND the free allowance (membership × cabin) as nonfree.

Common mistakes (with why):
- Decreasing nonfree_baggages/total_baggages — policy forbids removal.
- Using a certificate as payment_id — rejected.
