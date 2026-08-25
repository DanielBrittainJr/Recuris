---
id: return_delivered_order_items
type: action_result
trigger:
  event: pre_write
  tool: return_delivered_order_items
source: "enriched from fork write_review.py — SIMULATED scenario values, challenger v2 2026-07-04"
---
Purpose: return delivered items for a refund.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "I want to return the water bottle and the pet bed from my order."
  1. get_order_details("#W0000003") shows the DELIVERED order with items
     {"item_id": "1000000005", "name": "Water Bottle", ...} and
     {"item_id": "1000000006", "name": "Pet Bed", ...}, and payment_history
     showing the order was paid with "gift_card_1000002".
  2. Correct call:
     return_delivered_order_items(order_id="#W0000003",
       item_ids=["1000000005", "1000000006"],
       payment_method_id="gift_card_1000002")   # the ORDER'S ORIGINAL method

REFUND-DESTINATION RULE (frequent failure — check the order's payment_history FIRST):
  A refund may ONLY go to the order's original payment method, or to a gift card
  already on the account. It CANNOT go to a different card just because the
  customer asks.
  X SIMULATED failure: order "#W0000003" above was PAID BY GIFT CARD; the
    customer says "refund it to my credit card" and you set
    payment_method_id="credit_card_1000001" -> the tool fails:
    'Payment method should be the original payment method'. So if the customer
    asks for the refund on their credit card but the order was paid by gift
    card, tell them up front it must go back to the gift card — do not attempt
    the credit-card refund.
  OK Read payment_history of the order; use that same payment_method_id (or an
    existing gift card if the customer accepts).

Other common mistakes (with why):
- item_ids not taken verbatim from the order details: each item_id must belong
  to THIS order, copied exactly from get_order_details of THIS conversation.
