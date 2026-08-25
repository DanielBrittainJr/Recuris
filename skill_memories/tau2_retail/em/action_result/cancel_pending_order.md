---
id: cancel_pending_order
type: action_result
trigger:
  event: pre_write
  tool: cancel_pending_order
source: "enriched from fork write_review.py — SIMULATED scenario values, challenger v2 2026-07-04"
---
Purpose: cancel one pending order.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "please cancel my order, I don't need it anymore."
  1. get_order_details("#W0000004") confirms status is "pending".
  2. Correct call:
     cancel_pending_order(order_id="#W0000004", reason="no longer needed")

Common mistakes (with why):
- reason MUST be exactly 'no longer needed' or 'ordered by mistake' — no other
  wording. Passing the customer's own phrasing (e.g. reason="don't need it
  anymore" or reason="changed my mind") fails with 'Invalid reason'. Map the
  customer's intent onto one of the two exact strings.
- order_id="W0000004" without the leading '#': the order id must start with
  '#' and come verbatim from the order details of THIS conversation.
- Trying to cancel several orders with one call: one call cancels ONE order;
  cancel each order separately.
