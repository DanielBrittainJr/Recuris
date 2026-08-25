# Evidence-bounded diagnosis protocol

This protocol transfers construction strategy only. Do not use historical task
profiles, champion packages, held-out dev/test outcomes, or prior benchmark answer
data. Treat only the files explicitly exposed for this phase as evidence.

1. Read every trial record for each failing task: all failed trajectories and any
   successful counterfactual, including terminal tails, termination reasons, final
   actors, actual tool calls, and reward basis. Locate the first step where behavior
   diverges from the required action or policy.
2. Treat every termination reason, terminal marker, and final actor as an
   observation, not a causal label. Inspect the last point where the worker or
   runtime could have changed the outcome and compare successful
   counterfactuals. Do not infer an owner or repair from the surface form of a
   terminal message alone.
3. Record two independent causal variables for every cluster:
   - O (`action_opportunity`): `present`, `absent`, or `unknown`.
   - P (`patchability`): `memory`, `external`, or `unknown`.
   A Skill Memory patch is legal only for O=`present` and P=`memory`. If either
   variable is absent/unknown, use `fixes: []`. Never convert ambiguity into a
   patch opportunity merely because the trial failed.
   An adapter observation O=`unknown` is deliberately unresolved, not a causal
   verdict or a required plan value. Inspect all earlier assistant turns before
   deciding whether the cluster's O remains unknown or is supported as present.
   A successful counterfactual proves capability, not that the failed trace
   contained the same turn. Never cite a turn or call seen only in a passing
   trial as the failed trial's action opportunity. If a failed trace ends with a
   terminal user event and contains no later assistant draft or tool call, any
   post-terminal checker or write is unreachable; cite an earlier, actually
   observed intervention point or leave O absent/unknown.
   A hypothetical next actor is not an observed intervention point. When the
   evidence states `post_terminal_assistant_event: absent`, never place
   `draft_ready`, `pre_write`, a checker, or a write after that event. A repair
   may still target a concrete earlier assistant draft, such as an unnecessary
   extra confirmation, but it must cite that earlier turn from the failed trace.
   A concrete pre-failure assistant turn is an opportunity when the proposed
   component could have changed wording, visible pending state, delivery, or a
   checker decision on that turn; O is not limited to whether the missing write
   tool was already called.
4. Separate observation from hypothesis. Cite a concrete transcript moment, failed
   check, policy rule, or mechanism counter for every diagnosis.
   Express every proposed repair as one causal chain: exact reachable pre-failure
   event -> trigger condition -> memory delivered or checked -> expected immediate
   next behavior. Every link must be supported by the failed trace and disclosed
   runtime contract. If one link is hypothetical or post-terminal, abstain or move
   the intervention to an earlier observed event.
5. Cluster failures by shared causal step, not by surface story or task identifier.
6. Assign one primary owner:
   - E: missing or incorrect reusable knowledge, procedure, or action exemplar.
   - W: the verified task-state representation or grounding is wrong.
   - RHO: the needed memory exists but is not activated at the right moment.
   - C: unsupported progress or an executable authorized step escapes discipline.
   - MODEL: the model fails despite sufficient, correctly activated memory.
   - HARNESS: tool/runtime execution or observation is invalid.
   - BENCHMARK: reserve only for independently proven task, oracle, or verifier
     inconsistency.
7. Only E/W/RHO/C are patchable. MODEL/HARNESS/BENCHMARK are explicit
   non-patchable owners with `fixes: []`; never compensate for them with cards.
8. Prefer the smallest connected repair. A primary fix may have one necessary
   companion cluster (for example E plus the RHO wiring required to activate it),
   but unrelated changes are forbidden.
   When cross-round history is supplied, compare this causal chain with rejected
   interventions. A renamed card, narrower scope, or different component label is
   not a material revision if the event, trigger, intervention, and expected next
   behavior remain the same.
9. Select a mechanism only after identifying the first runtime event that can
   observe the failure. Verify the event, state, and tool preconditions against
   the disclosed capability contract. In particular, never route a
   non-mutating tool through a carrier that observes only the mutating-tool
   ledger. When scoped retrieval is proposed, include any W wiring needed for a
   tool-preserving WM entry kind and verify that the adapter marks the tool as
   mutating.
10. Use placeholders in proposed memory. Never copy real identifiers or a complete
   task answer into a reusable card.
11. The whole candidate will first face a deterministic carrier-reachability
   check, then repair and held-out regression gates. Do not use
   dev/test feedback in diagnosis and do not assume a proposed change is helpful.
