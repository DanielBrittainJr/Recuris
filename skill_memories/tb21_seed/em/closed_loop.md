---
id: terminal_closed_loop
type: procedure
trigger:
  event: turn_start
  tool: "*"
source: task-neutral-seed
---
Use a closed terminal loop: inspect the current state, make the smallest needed
change, then validate the actual artifact or behavior. Prefer a task-provided
check or the project's native test command. Read the validation output and run
it again after the final edit before declaring the task complete.
