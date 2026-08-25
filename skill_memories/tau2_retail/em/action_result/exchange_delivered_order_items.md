---
id: exchange_delivered_order_items
type: action_result
trigger:
  event: pre_write
  tool: exchange_delivered_order_items
source: "enriched from fork write_review.py — SIMULATED scenario values, challenger v2 2026-07-04"
---
Purpose: swap delivered items for DIFFERENT variants of the SAME product.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "my desk lamp is too small, I want the medium one."
  1. get_order_details("#W0000001") shows the delivered lamp:
     {"name": "Desk Lamp", "item_id": "1000000001", "options": {"size": "small"}}
  2. get_product_details for Desk Lamp lists variants, including
     {"item_id": "1000000002", "options": {"size": "medium"}}  <- the replacement
  3. Correct call:
     exchange_delivered_order_items(order_id="#W0000001",
       item_ids=["1000000001"], new_item_ids=["1000000002"],
       payment_method_id="credit_card_1000001")   # an EXISTING method from user profile

Common mistakes (with why):
- new_item_ids=["1000000001"] (same as item_ids): exchanges nothing — the tool
  accepts it SILENTLY and the exchange is wrong. new_item_ids must be the
  REPLACEMENT variant id found via get_product_details, never the current id.
- new_item_ids=["2000000009"] when no earlier tool result ever showed that id:
  invented ids fail or corrupt the order. If you have not SEEN the id in this
  conversation, look it up first.
- Wrong pairing on multi-item exchanges: new_item_ids[k] replaces item_ids[k]
  (same position, same product, different variant).
- payment_method_id="paypal_9999999" not present in the user's profile: must be
  an EXISTING method id from get_user_details / order details.
