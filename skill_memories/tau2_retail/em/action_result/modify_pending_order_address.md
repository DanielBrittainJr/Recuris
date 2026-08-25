---
id: modify_pending_order_address
type: action_result
trigger:
  event: pre_write
  tool: modify_pending_order_address
source: "enriched from fork write_review.py — SIMULATED scenario values, challenger v2 2026-07-04"
---
Purpose: change the shipping address of one pending order.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "ship my pending order to my new place in New York instead."
  1. get_order_details("#W0000005") confirms status is "pending"; addresses you
     already saw in the user/order details show the system FORMAT — in
     particular state is a 2-LETTER code.
  2. Correct call (copy the field FORMAT from addresses already seen):
     modify_pending_order_address(order_id="#W0000005",
       address1="123 Placeholder St", address2="",
       city="New York", state="NY", country="USA", zip="10000")

Common mistakes (with why):
- state="New York" (written out in full) -> WRONG; state="NY" -> RIGHT.
  The system data uses the 2-letter code everywhere; full state names do not
  match anything in the database.
- Missing zip, or omitting address2: every field is required — use
  address2="" if there is no second line.
- Fields copied from customer speech without normalizing to the system format
  seen in existing user/order addresses.
