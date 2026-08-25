---
id: modify_pending_order_items
type: action_result
trigger:
  event: pre_write
  tool: modify_pending_order_items
source: "enriched from fork write_review.py — SIMULATED scenario values, challenger v2 2026-07-04"
---
Purpose: change pending-order items to DIFFERENT variants of the SAME product.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "I ordered the blue shirt but I'd rather have the red one."
  1. get_order_details("#W0000002") shows the pending order with:
     {"name": "Cotton Shirt", "item_id": "1000000003", "options": {"color": "blue"}}
  2. get_product_details for Cotton Shirt lists variants, including
     {"item_id": "1000000004", "options": {"color": "red"}}  <- the replacement
  3. Correct call:
     modify_pending_order_items(order_id="#W0000002",
       item_ids=["1000000003"], new_item_ids=["1000000004"],
       payment_method_id="gift_card_1000002")   # an EXISTING method from user profile

Common mistakes (with why):
- new_item_ids=["1000000003"] (same as item_ids): modifies nothing, always wrong.
  new_item_ids must be the DIFFERENT replacement variant id found via
  get_product_details, never the current id.
- Ids not present in earlier tool results: never invent ids. If you have not
  SEEN the id in this conversation, look it up first.
- Wrong position pairing: new_item_ids[k] replaces item_ids[k] (same position,
  same product, different variant).
- Calling twice on the same pending order: this tool can only be called ONCE
  per pending order — batch ALL item changes into a single call.
- payment_method_id must be an EXISTING method id from user/order details.
