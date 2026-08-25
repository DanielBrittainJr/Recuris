---
id: send_certificate
type: action_result
trigger: {event: pre_write, tool: send_certificate}
source: "airline policy.md compensation rules; wrong-write analysis 2026-07-05"
---
Purpose: send a compensation certificate to a user. Be careful — eligibility-gated.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer (asks for compensation): "my flight was cancelled, anything you can do?"
  1. get_user_details("uuu_###") — check membership; get reservation — check facts.
  2. Eligible ONLY if: member is silver/gold OR insurance=="yes" OR cabin=="business".
  3. Correct call: send_certificate(user_id="uuu_###", amount=<100 * n_passengers>)
     for a cancelled-flight complaint (or 50 * n_passengers for a delayed flight,
     and only AFTER the change/cancel is done).

Rules:
- Do NOT proactively offer compensation — only when the user asks.
- Do NOT compensate a regular member with no insurance flying (basic) economy.
- Amount is EXACTLY 100*pax (cancelled) or 50*pax (delayed); no other reason/amount.

Common mistakes (with why):
- Wrong amount (not tied to passenger count / complaint type).
- Compensating an ineligible user, or offering it unprompted.
