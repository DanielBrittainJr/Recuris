---
id: book_reservation
type: action_result
trigger: {event: pre_write, tool: book_reservation}
source: "airline policy.md booking rules; wrong-write analysis 2026-07-05"
---
Purpose: create a new reservation.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "book me DFW to LAX, economy, with insurance, one bag."
  1. Confirm cabin word EXACTLY: "basic_economy" and "economy" are DISTINCT classes.
  2. payment_methods must SUM to the total price:
     price = seat prices + (insurance ? 30 * n_passengers : 0) + 50 * nonfree_baggages
  3. Correct call: book_reservation(user_id="uuu_###", origin="DFW",
     destination="LAX", flight_type="one_way", cabin="economy",
     flights=[{flight_number:"HAT###", date:"YYYY-MM-DD"}],
     passengers=[{first_name, last_name, dob}], payment_methods=[{payment_id, amount}],
     total_baggages=1, nonfree_baggages=<0 if within free allowance>, insurance="yes")

Rules:
- Every payment_id must already exist in the user profile.
- At most: 1 certificate, 1 credit card, 3 gift cards; at most 5 passengers.
- Free-baggage allowance depends on membership × cabin — only count bags BEYOND it
  as nonfree_baggages.
- Do NOT add checked bags the user did not request; confirm insurance with the user.

Common mistakes (with why):
- cabin "economy" vs "basic_economy" mismatch — different class, different rules.
- payment amounts that don't sum to price → "Payment amount does not add up" (forgot
  insurance +30/pax or +50/nonfree bag).
- payment_id not in profile; nonfree_baggages over-charging within free allowance.
