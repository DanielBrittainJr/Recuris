---
id: dont_escalate_basic_economy
type: procedure
trigger: {event: draft_ready, tool: transfer_to_human_agents}
source: "airline read-subset autopsy 2026-07-05 (B-deflect tasks 19/32)"
---
BEFORE transferring to a human: a basic-economy reservation whose FLIGHTS the
customer wants changed is NOT a dead end. Policy allows a CABIN UPGRADE even for
basic economy. So the in-policy path is: offer to upgrade the cabin to economy
(the customer pays the fare difference), THEN modify the flights. Only if the
customer declines the upgrade is the request truly unservable. Do not transfer
before offering this path.
