"""Builtin Deliverers — how EM reaches the model (SPI ③, proposals only).

Delivery taxonomy (aligned 2026-07-04):
  1. need_driven_retrieval - self-directed retrieval: WM gaps drive queries (type-1)
  2. standing_inject       - standing injection: unconditional reminder (type-2)
  3. exemplar_bounce       - event-triggered enforcement: call-time exemplars (type-3)
  4. boundary_inject       - boundary injection: opening/closing, predicate over WM
The kernel EXECUTES all actions (synthetic-id registration, replay safety).
"""

from __future__ import annotations

import re

from recuris.events import Event
from recuris.spi import CoverWithDoc, ExemplarBounceAction, InjectNote, TriggerContext
from recuris.writereview import NOT_EXECUTED_TEXT, build_review_text

_WORD_RE = re.compile(r"[a-zA-Z0-9_#]+")


class ExemplarBounceDeliverer:
    """τ² write-review: pre_write bounce with per-tool action_result exemplars."""

    name = "exemplar_bounce"
    events = (Event.PRE_WRITE.value,)

    def __init__(self, em_type: str = "action_result", refusal_branch: bool = False,
                 exact_only: bool = False):
        self.em_type = em_type
        self.refusal_branch = refusal_branch  # audit D1, challenger option
        # Backward-compatible default: missing exemplars still receive the
        # generic write-review fallback. exact_only is precision-first opt-in.
        self.exact_only = bool(exact_only)

    def _has_scoped_entry(self, em, tool: str) -> bool:
        exact = em.query(
            event=Event.PRE_WRITE.value, tool=tool, type=self.em_type
        )
        generic = em.query(
            event=Event.PRE_WRITE.value, tool="*", type=self.em_type
        )
        return bool(exact or generic)

    def select(self, ctx: TriggerContext, wm_view, em, draft=None):
        calls = ctx.extra.get("draft_calls") or []
        write_tools = ctx.extra.get("write_tools") or set()
        writes = [tc for tc in calls if tc.name in write_tools]
        if not writes:
            return []
        if self.exact_only and any(
            not self._has_scoped_entry(em, tc.name) for tc in writes
        ):
            # A bounce is draft-wide: partially reviewing a mixed write draft
            # would also inject synthetic NOT_EXECUTED results into unrelated
            # calls. Require complete exact/* coverage or leave it untouched.
            return []
        review_text = build_review_text(
            em, [tc.name for tc in writes], refusal_branch=self.refusal_branch
        )
        reviewed_ids = {tc.id for tc in writes}
        results = {
            tc.id: (review_text if tc.id in reviewed_ids else NOT_EXECUTED_TEXT)
            for tc in calls
        }
        return [
            ExemplarBounceAction(
                results_by_call=results, tools=tuple(tc.name for tc in writes)
            )
        ]


class StandingInject:
    """Type-2 delivery: pin selected EM entries into every turn's WM block.

    Weak-model evidence says this is nearly ineffective for behavioural
    skills - it is provided
    because the taxonomy needs the rung and strong models may cash it."""

    name = "standing_inject"
    events = (Event.TURN_START.value,)

    def __init__(self, em_type: str = "knowledge", tool: str = "*"):
        self.em_type = em_type
        self.tool = tool

    def select(self, ctx: TriggerContext, wm_view, em, draft=None):
        entries = em.query(event=Event.TURN_START.value, type=self.em_type)
        return [InjectNote(text=e.body, tag=f"standing:{e.id}") for e in entries]


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 2}


class NeedDrivenRetrieval:
    """Self-directed retrieval (type-1): pending WM gaps drive EM queries; hits
    under the model's nose AND grounded as covered via a delivery receipt.

    Scorer: lightweight lexical overlap (query tokens = entry description;
    doc tokens = title+body). Deliberately dependency-free; packages needing
    embeddings can ship a custom Deliverer. min_overlap guards precision."""

    name = "need_driven_retrieval"
    events = (Event.INTENT_RECORDED.value,)

    def __init__(self, em_types: tuple = ("knowledge", "procedure"), top_k: int = 1,
                 min_overlap: int = 2, settle: bool = True,
                 scope_by_tool: bool = False, max_per_episode: int = 0):
        self.em_types = tuple(em_types)
        self.top_k = int(top_k)
        self.min_overlap = int(min_overlap)
        # settle=False: deliver-only — knowledge delivery is not terminal
        # evidence for service ledgers (bank smoke fix 2026-07-06)
        self.settle = bool(settle)
        # Both controls are opt-in: existing packages remain unscoped and
        # unlimited. A zero episode cap means unlimited delivery.
        self.scope_by_tool = bool(scope_by_tool)
        self.max_per_episode = max(0, int(max_per_episode))

    def _in_tool_scope(self, doc, entry) -> bool:
        if not self.scope_by_tool or doc.type not in {"knowledge", "procedure"}:
            return True
        trigger_tool = getattr(doc, "trigger_tool", "") or ""
        entry_tool = getattr(entry, "tool", "") or ""
        # Missing metadata is not an implicit wildcard in scoped mode.
        return trigger_tool == "*" or (
            bool(trigger_tool) and trigger_tool == entry_tool
        )

    def select(self, ctx: TriggerContext, wm_view, em, draft=None):
        actions = []
        docs = [e for e in em.entries if e.type in self.em_types]
        if not docs:
            return actions
        already = ctx.extra.get("delivered_docs") or set()
        delivered_counts = {}
        if self.max_per_episode:
            for key in already:
                if isinstance(key, tuple) and key:
                    did = key[0]
                    delivered_counts[did] = delivered_counts.get(did, 0) + 1
        selected_counts = {}
        selected_keys = set()
        for entry in wm_view.pending():
            q = _tokens(entry.description)
            scored = []
            for d in docs:
                if not self._in_tool_scope(d, entry):
                    continue
                score = len(q & (_tokens(d.id) | _tokens(d.body[:400])))
                if score >= self.min_overlap:
                    scored.append((score, d))
            scored.sort(key=lambda x: -x[0])
            for _, d in scored[: self.top_k]:
                # audit fix #12: stable dedup key - entry ids are re-keyed every
                # ledger update, so (doc, description) prevents re-delivery loops.
                if (d.id, entry.description) in already:
                    continue
                if self.max_per_episode:
                    dedup_key = (d.id, entry.description)
                    if dedup_key in selected_keys:
                        continue
                    used = delivered_counts.get(d.id, 0) + selected_counts.get(d.id, 0)
                    if used >= self.max_per_episode:
                        continue
                actions.append(
                    CoverWithDoc(entry_id=entry.id, doc_id=d.id, content=d.body,
                                 settle=self.settle)
                )
                if self.max_per_episode:
                    selected_keys.add((d.id, entry.description))
                    selected_counts[d.id] = selected_counts.get(d.id, 0) + 1
        return actions


class StateReminder:
    """WM-state-driven per-item reminders (v3 challenger, designed 2026-07-05).

    - an UNAUTHORIZED executable pending entry -> inject "confirm with the
      customer before executing" phrasing, plus the item list
    - an AUTHORIZED pending entry -> inject "authorization obtained: check
      policy, then execute immediately" phrasing

    EM documents are fetched by id (pure data, swappable); a built-in fallback
    text is used when the id is absent. This deliverer only emits InjectNote --
    execution and sanitization stay in the kernel."""

    name = "state_reminder"
    events = (Event.INTENT_RECORDED.value,)

    DEFAULT_UNCONFIRMED = (
        "[STATE REMINDER — from the system] The requests below are recorded but NOT yet "
        "authorized by the customer. Do NOT execute them; confirm each with the customer "
        "first, then execute immediately upon authorization:"
    )
    DEFAULT_AUTHORIZED = (
        "[STATE REMINDER — from the system] The customer has AUTHORIZED the requests below "
        "(verified against their own words). First check each against policy: if not "
        "allowed, tell the customer why; if allowed, EXECUTE the tool call NOW — do not "
        "stall with more conversation:"
    )

    def __init__(self, unconfirmed_doc: str = "confirm_before_execute",
                 authorized_doc: str = "authorized_execute_check"):
        self.unconfirmed_doc = unconfirmed_doc
        self.authorized_doc = authorized_doc

    def _doc_body(self, em, doc_id: str, fallback: str) -> str:
        hit = next((e for e in em.entries if e.id == doc_id), None)
        return hit.body if hit else fallback

    def select(self, ctx: TriggerContext, wm_view, em, draft=None):
        pend = wm_view.pending()
        unconf = [e for e in pend if e.tool and not getattr(e, "auth", None)]
        auth = [e for e in pend if getattr(e, "auth", None)]
        notes = []
        if unconf:
            body = self._doc_body(em, self.unconfirmed_doc, self.DEFAULT_UNCONFIRMED)
            items = "\n".join(f"- {e.description}" for e in unconf)
            notes.append(InjectNote(text=f"{body}\n{items}", tag="state:unconfirmed"))
        if auth:
            body = self._doc_body(em, self.authorized_doc, self.DEFAULT_AUTHORIZED)
            items = "\n".join(
                f'- {e.description}  (authorized: "{(e.auth or {}).get("quote", "")[:60]}")'
                for e in auth
            )
            notes.append(InjectNote(text=f"{body}\n{items}", tag="state:authorized"))
        return notes


class BoundaryInject:
    """Boundary injection (harness-triggered by WM state): opening/closing notes.

    Fires only when its WM predicate holds — e.g. closing reminder only if
    pending entries remain. This is the 'observe agent-environment interaction plus WM state' hook
    the previous owner asked for, as a builtin."""

    name = "boundary_inject"

    def __init__(self, at: str = "turn_start", when: str = "always",
                 em_type: str = "procedure", tool: str = "*",
                 max_cards: int = 1):
        self.at = at
        # Carrier capacity. Default 1 reproduces the historical behaviour
        # byte-for-byte (EMStore.for_tool returned a single entry), so every
        # package written before this knob keeps its exact treatment. Archives
        # that accumulate cards — the Meta-Agent's only move on terminal
        # benchmarks — must raise this or newly written cards can never reach
        # the model, which reads as "the method does not work" when in fact
        # nothing was delivered.
        self.max_cards = max(1, int(max_cards))
        # audit fix #4: events derived from cfg - never advertise an event the
        # runtime does not dispatch (terminal_boundary belongs to the harness).
        self.events = (at,)
        self.when = when  # always | first_turn | has_pending
        self.em_type = em_type
        self.tool = tool

    def _predicate(self, ctx: TriggerContext, wm_view) -> bool:
        if self.when == "first_turn":
            return ctx.turn == 1
        if self.when == "has_pending":
            return bool(wm_view.pending())
        return True

    def select(self, ctx: TriggerContext, wm_view, em, draft=None):
        if ctx.event != self.at or not self._predicate(ctx, wm_view):
            return []
        if self.max_cards == 1:
            hit = em.for_tool(self.at, self.tool)
            return [InjectNote(text=hit.body, tag=f"boundary:{hit.id}")] if hit else []
        hits = em.all_for_tool(self.at, self.tool)[: self.max_cards]
        return [InjectNote(text=h.body, tag=f"boundary:{h.id}") for h in hits]
