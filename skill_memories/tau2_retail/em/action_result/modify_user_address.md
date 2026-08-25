---
id: modify_user_address
type: action_result
trigger:
  event: pre_write
  tool: modify_user_address
source: "enriched from fork write_review.py — SIMULATED scenario values, challenger v2 2026-07-04"
---
Purpose: change the user's default address.

SIMULATED walk-through (example values — NEVER copy any id below; every real id
must come from THIS conversation's tool results):
  Customer: "I just moved to Denver, please update my default address."
  1. get_user_details("jane_doe_1000001") shows the current address in the
     system FORMAT, e.g. {"state": "CO", "country": "USA", ...} — state is a
     2-LETTER code.
  2. Correct call (same field format as existing addresses in the system):
     modify_user_address(user_id="jane_doe_1000001",
       address1="456 Placeholder Ave", address2="Apt 1",
       city="Denver", state="CO", country="USA", zip="80000")

Common mistakes (with why):
- state="Colorado" (written out in full) -> WRONG; state="CO" -> RIGHT.
  The system data uses the 2-letter code (e.g. 'NY', 'CO') everywhere.
- Fields copied from customer speech without normalizing to the system format:
  match the field FORMAT of addresses already seen in this conversation.
- Omitting fields: zip must be included, and address2="" if there is no
  second line.
- user_id must be the one already resolved in THIS conversation (via
  find_user_id_by_email / get_user_details), never guessed.
