---
id: flight_duration_source
type: procedure
trigger: {event: draft_ready, tool: transfer_to_human_agents}
source: "airline read-subset autopsy 2026-07-05 (B-deflect task 44)"
---
Flight DURATION / "fastest option" comes from search_direct_flight (and
search_onestop_flight): those return each segment's departure and arrival times,
so duration = arrival − departure and total = last_arrival − first_departure.
get_flight_status returns ONLY availability/status, never times — it CANNOT give
durations. If you concluded "no tool retrieves duration", you used the wrong tool.
Do NOT transfer; re-query with search_direct_flight and compute the times.
