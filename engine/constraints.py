"""Hard constraints that filter or flag combos before scoring.

These run BEFORE fetching fares — they only depend on the combo shape (legs,
dates, airports) not on API data.
"""

from datetime import datetime, time
from typing import List

from engine.routes import Combo


def _find_gru_intl_to_dom(combo: Combo):
    """Return (intl_leg, dom_leg) if combo has an international→domestic transfer at GRU."""
    for i in range(len(combo.legs) - 1):
        a, b = combo.legs[i], combo.legs[i + 1]
        if a.destination == "GRU" and b.origin == "GRU":
            return a, b
    return None


def _has_cgh_mismatch(combo: Combo) -> bool:
    for i in range(len(combo.legs) - 1):
        a, b = combo.legs[i], combo.legs[i + 1]
        if a.destination == "GRU" and b.origin in {"CGH", "VCP"}:
            return True
    return False


def _is_separate_tickets(combo: Combo) -> bool:
    # Phase 1 assumption: intl legs (leg 1–2) and domestic leg (leg 3) are always
    # sold on separate tickets since no single carrier sells ORD→stopover→GRU→SC as
    # one PNR in our target set.
    return len(combo.legs) >= 2 and any(
        leg.destination in {"NVT", "JOI", "CWB"} for leg in combo.legs
    )


def _connection_hours(combo: Combo) -> float:
    """Approximate connection time between legs (in hours, using date-only).

    Without time-of-day data at the combo-shape stage, we use date diff × 24.
    Real connection enforcement happens post-fetch when we have actual times.
    """
    # Rough same-day connection heuristic: if leg[i+1].date == leg[i].date,
    # assume 4h; if next day, assume 24h.
    min_hours = float("inf")
    for i in range(len(combo.legs) - 1):
        diff = (combo.legs[i + 1].date - combo.legs[i].date).days
        hours = 4.0 if diff == 0 else diff * 24.0
        min_hours = min(min_hours, hours)
    return min_hours if min_hours != float("inf") else 0.0


def apply_constraints(combos: List[Combo], constraints: dict) -> List[Combo]:
    """Filter combos that violate hard rules, attach flags to the rest."""
    min_gru_conn = float(constraints.get("gru_min_connection_hours", 3))
    max_travel = float(constraints.get("max_total_travel_hours", 36))
    flag_cgh = bool(constraints.get("flag_gru_cgr_mismatch", True))

    valid: List[Combo] = []
    for combo in combos:
        flags: List[str] = []

        gru_pair = _find_gru_intl_to_dom(combo)
        if gru_pair:
            a, b = gru_pair
            diff_hours = (b.date - a.date).days * 24.0
            if diff_hours == 0:
                diff_hours = 4.0  # assumed same-day connection
            if diff_hours < min_gru_conn:
                continue  # hard reject

        if flag_cgh and _has_cgh_mismatch(combo):
            flags.append("⚠️ DOMESTIC LEG DEPARTS CGH NOT GRU — add 2h transfer + ~$30 taxi")

        if _is_separate_tickets(combo):
            flags.append("📋 SEPARATE TICKETS — no protection if earlier leg is delayed")

        combo.flags = flags
        valid.append(combo)

    return valid


def prune_impossible_after_fetch(scored, max_travel_hours: float):
    """Second-stage filter once we know actual flight durations."""
    return [s for s in scored if s.total_travel_hours <= max_travel_hours]
