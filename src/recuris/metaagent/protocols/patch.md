# Evidence-bounded patch protocol

Implement the validated plan exactly in the disposable candidate. The plan and
current evolve evidence are the only task-derived inputs. Never read or imitate a
historical champion, static task profile, held-out result, or unexposed benchmark
file.

General rules:

1. Make the smallest connected change rooted at the plan's primary component.
   Companion wiring is allowed only when the plan explicitly includes it.
2. Preserve all unrelated package behavior. Do not rewrite a whole manifest or card
   when a local edit suffices.
3. A reusable card teaches a rule, decision boundary, procedure, or simulated call
   pattern. It must not memorize a task identifier, user record, database value, or
   evaluator-specific answer instance.
4. Every card must be reachable: action-result cards require action-moment delivery;
   knowledge/procedure cards require a matching retrieval or bounded injection path.
   A knowledge card carried by `standing_inject` must declare `turn_start`; a
   boundary-injected card must declare the configured event/tool pair.
   Preserve the plan's full causal chain: the card or checker must activate at the
   named pre-failure event and make the named immediate next behavior possible.
   Never encode an instruction whose first usable trigger occurs after the episode
   has terminated.
5. Prefer targeted activation over always-on context. Use standing injection only
   for genuinely universal content.
   Every procedure/action-result card must implement the plan's exact
   `trigger_event` and `trigger_tool`. When a procedure is paired with
   `need_driven_retrieval`, use event `intent_recorded`, `scope_by_tool: true`,
   `max_per_episode: 1`, and `settle: false`; reading memory is not execution proof.
   This route also requires a tool-preserving WM entry kind and a mutating domain
   tool because retrieval sees the write-tool ledger, not the model's next draft.
   A draft-level or non-mutating decision may use only a checker/carrier that the
   current adapter contract explicitly says can observe that event and surface
   the matching card. Do not infer such reachability from a tool name.
   Action-result cards use exact tool/event routing and `exemplar_bounce` with
   `exact_only: true`, so unrelated writes remain untouched.
6. W changes must preserve verified-state semantics; C changes may react only to
   observable state/evidence and must not invent progress.
7. Use only the schema and builtin vocabulary supplied in this phase. Do not add
   plugin code, commands, new runtime primitives, or files outside the candidate.
8. Complete every admitted fix and nothing else. The deterministic referee, repair
   evaluation, and held-out gate decide whether the candidate survives.
