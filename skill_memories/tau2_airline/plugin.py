"""Airline feasibility oracle: policy-driven BLOCKED rulings (a local: plugin).

Domain policy is domain code. It lives in the package, where a human reviews
it, not in the kernel. This reads objective fields out of real READ receipts
(get_reservation_details / get_user_details), judges feasibility against the
airline policy document, and emits a BlockRuling. The kernel records BLOCKED
with writer=ORACLE, so the model can never rule on feasibility itself --
otherwise over-reach and self-deception regress.

Policy source: tau2-Bench data/tau2/domains/airline/policy.md.
"Now" is fixed at 2024-05-15T15:00:00, matching the benchmark's own clock.
"""

from __future__ import annotations

from datetime import datetime

from recuris.spi import BlockRuling

NOW = datetime(2024, 5, 15, 15, 0, 0)
FLOWN = {"flying", "landed"}
# policy covers ONLY health- or weather-related cancellations; matched as
# substrings against the model's free-text reason. Kept synonym-rich to cut
# false ORACLE-BLOCKs of legitimate insured cancellations (a hard, non-
# overridable block), while avoiding short strings that collide with unrelated
# words (e.g. no bare "ill"/"flu"). NOTE residual limit: the reason is the
# model's own free text — if it omits the cause, coverage can still be missed.
INSURANCE_REASONS = (
    # health / medical
    "health", "medical", "sick", "illness", "unwell", "injur", "hospital",
    "surgery", "disease", "covid",
    # weather
    "weather", "hurricane", "storm", "typhoon", "blizzard", "snowstorm",
    "snow", "flood", "evacuat",
)


def _reservations(evidence: dict) -> dict:
    """reservation_id -> reservation dict, from get_reservation_details receipts
    (later receipts win — reflects latest queried state)."""
    out = {}
    for r in evidence.get("get_reservation_details", []) or []:
        if isinstance(r, dict) and r.get("reservation_id"):
            out[str(r["reservation_id"])] = r
    return out


def _users(evidence: dict) -> dict:
    out = {}
    for u in evidence.get("get_user_details", []) or []:
        if isinstance(u, dict):
            uid = u.get("user_id") or (u.get("name") and str(u.get("user_id")))
            if uid:
                out[str(uid)] = u
    return out


def _date_in_past(raw) -> bool:
    """Segment date strictly before today (NOW is fixed) => it has already
    operated. Same-day flights are unknowable from the date alone -> not past
    (conservative: don't over-block); unparseable dates likewise."""
    if not raw:
        return False
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date() < NOW.date()
    except ValueError:
        return False


def _flown_or_cancelled(res: dict) -> tuple[bool, bool]:
    """(any_flown, any_airline_cancelled).

    Real get_reservation_details receipts carry NO per-segment status
    (ReservationFlight = flight_number/origin/destination/date/price; the
    operational status lives on the flight DB object, reachable only via
    get_flight_status, whose bare-string replies never survive the adapter's
    JSON parse) — so the status checks below only serve fixtures/future
    enrichment, and the load-bearing signal is the segment DATE: dated before
    today => already operated => flown (policy.md: any flown portion -> human
    agent, overrides every allowance). Known limitation: a PAST segment the
    airline cancelled is indistinguishable from a flown one here and gets the
    same hard block though policy would allow the cancel; zero such cases in
    the 26 A/B tasks — resolving it needs get_flight_status receipts."""
    flown = cancelled = False
    for f in res.get("flights", []) or []:
        st = str(f.get("status") or "")
        if st in FLOWN:
            flown = True
        elif st == "cancelled":
            cancelled = True
        elif _date_in_past(f.get("date")):
            flown = True
    return flown, cancelled


def _within_24h(res: dict) -> bool:
    ts = res.get("created_at")
    if not ts:
        return False
    try:
        created = datetime.fromisoformat(str(ts))
    except Exception:
        return False
    return (NOW - created).total_seconds() <= 24 * 3600


def _cancel_block(res: dict, reason_text: str) -> str | None:
    """Return a BLOCK reason if cancel is infeasible, else None (allowed)."""
    flown, air_cancelled = _flown_or_cancelled(res)
    if flown:
        return "a flight segment has already been flown — cancellation needs a human agent"
    if _within_24h(res):
        return None
    if air_cancelled:
        return None
    if res.get("cabin") == "business":
        return None
    if res.get("insurance") == "yes" and any(k in reason_text.lower() for k in INSURANCE_REASONS):
        return None
    return ("cancellation not allowed: not within 24h of booking, not business cabin, "
            "flight not airline-cancelled, and no insurance-covered reason")


def _same_segments(new_flights, cur_flights) -> bool:
    """True when the proposed flights are the same segments as the reservation's
    (order-insensitive) — i.e. a cabin-only change, which policy allows for ALL
    cabins: "all reservations, including basic economy, can change cabin without
    changing the flights" (policy.md, Change cabin)."""
    def keys(fs):
        return sorted((str(f.get("flight_number") or ""), str(f.get("date") or ""))
                      for f in fs if isinstance(f, dict))
    return keys(new_flights) == keys(cur_flights)


def _update_flights_block(res: dict, params: dict) -> str | None:
    """A/B autopsy 2026-07-06 (root-cause fix for 112/147 false blocks):
    the old predicate blocked
    EVERY update_reservation_flights on basic_economy, including the cabin-only
    upgrades that policy explicitly allows — its own reason text even said
    "(cabin upgrade is still allowed)". Now we block only when the SEGMENTS
    change; no flights payload recorded => cannot prove a segment change =>
    don't guess (conservative, same ethos as the missing-reservation case)."""
    if res.get("cabin") != "basic_economy":
        return None
    new = params.get("flights")
    if not isinstance(new, list) or not new:
        return None  # intent recorded without segment payload -> cannot rule
    if _same_segments(new, res.get("flights", []) or []):
        return None  # cabin-only change — allowed for basic economy too
    return "basic economy flights cannot be modified (cabin upgrade is still allowed)"


def _baggage_block(res: dict, params: dict) -> str | None:
    cur = res.get("nonfree_baggages")
    new = params.get("nonfree_baggages")
    if isinstance(cur, int) and isinstance(new, int) and new < cur:
        return "checked bags can be added but not removed"
    return None


class AirlineFeasibilityOracle:
    name = "airline_feasibility"

    def rule(self, pending, evidence):
        reservations = _reservations(evidence)
        rulings = []
        for e in pending:
            tool = getattr(e, "tool", "")
            params = getattr(e, "params", {}) or {}
            rid = str(params.get("reservation_id") or "")
            res = reservations.get(rid)
            if res is None:
                continue  # no objective state queried yet -> cannot rule (don't guess)
            reason = None
            if tool == "cancel_reservation":
                reason = _cancel_block(res, e.description)
            elif tool == "update_reservation_flights":
                reason = _update_flights_block(res, params)
            elif tool == "update_reservation_baggages":
                reason = _baggage_block(res, params)
            if reason:
                rulings.append(BlockRuling(entry_id=e.id, reason=reason))
        return rulings
