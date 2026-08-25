---
id: modify_pending_order_payment
type: action_result
trigger:
  event: pre_write
  tool: modify_pending_order_payment
source: "enriched from fork write_review.py — SIMULATED scenario values, challenger v2 2026-07-04"
---
Purpose: switch the payment method of one pending order.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "pay for that pending order with my gift card instead."
  1. get_user_details shows the user's EXISTING payment methods:
     "credit_card_1000001" and "gift_card_1000002".
  2. get_order_details("#W0000006") confirms status "pending" and that the
     order is currently paid with "credit_card_1000001".
  3. Correct call:
     modify_pending_order_payment(order_id="#W0000006",
       payment_method_id="gift_card_1000002")   # an EXISTING, DIFFERENT method

Common mistakes (with why):
- payment_method_id="paypal_9999999" that does not exist in the user's profile:
  never invent ids — the method must appear in get_user_details / order
  details of THIS conversation.
- payment_method_id equal to the order's CURRENT method: the tool rejects it
  ('The new payment method should be different from the current one'). Check
  the order's payment_history first.
- Switching to a gift card whose balance cannot cover the order amount: the
  tool fails with insufficient gift card balance.
