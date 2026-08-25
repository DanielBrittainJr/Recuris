---
id: route_change_is_cancel_rebook
type: procedure
trigger: {event: draft_ready, tool: transfer_to_human_agents}
source: "airline read-subset autopsy 2026-07-05 (B-deflect/A task 29)"
---
Changing the ORIGIN or DESTINATION (e.g. LGA -> JFK) of a reservation CANNOT be
done with update_reservation_flights — that tool keeps origin/destination/trip
type fixed. The in-policy path is: CANCEL the existing reservation (if cancellable)
and BOOK a new one for the new route. This is a normal agent task, not a
human-transfer case. Present the cancel + rebook plan to the customer.
