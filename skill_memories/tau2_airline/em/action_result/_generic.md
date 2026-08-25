---
id: _generic
type: action_result
trigger: {event: pre_write, tool: "*"}
source: "airline package bootstrap"
---
Verify every argument against the tool description and the reservation/user data you
retrieved earlier in THIS conversation. Never invent reservation ids, payment ids, or
flight numbers. If the request violates policy, do not call the tool — explain to the customer.
