---
id: authorized_execute_check
type: procedure
trigger:
  event: intent_recorded
source: "v3 challenger design 2026-07-05"
---
[STATE REMINDER — from the system] The customer has AUTHORIZED the requests below —
verified against their own words. Before executing each one, check it against policy:
if it is NOT allowed (wrong refund destination, invalid reason, same-variant exchange),
explain the refusal to the customer instead. If it IS allowed, EXECUTE the tool call in
THIS turn — a confirmation reply is not the action itself.
