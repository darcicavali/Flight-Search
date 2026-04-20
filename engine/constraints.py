"""Hard constraints that filter or flag combos — both pre- and post-fetch.

`apply_constraints` runs BEFORE fetching fares and only uses combo shape.
`prune_short_gru_connections` runs AFTER fetching and uses real flight times.
"""

from datetime import date as _date
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from engine.routes import Combo

# Time needed to transit between GRU and CGH/VCP airports on the ground. Added
# on top of the base minimum-connection requirement for cross-airport combos.
CROSS_AIRPORT_TRANSFER_HOURS = 2.0

ALT_SP_AIRPORTS = {"CGH", "VCP"}


def _find_gru_intl_to_dom(combo: Combo):
    """Return (intl_leg, dom_leg, i, cross_airport) if the combo routes
    international→domestic via GRU, including CGH/VCP alternates.
    """
    for i in range(len(combo.legs) - 1):
        a, b = combo.legs[i], combo.legs[i + 1]
        if a.destination != "GRU":
            continue
        if b.origin == "GRU":
            return a, b, i, False
        if b.origin in ALT_SP_AIRPORTS:
            return a, b, i, True
    return None


def _has_cgh_mismatch(combo: Combo) -> bool:
    for i in range(len(combo.legs) - 1):
        a, b = combo.legs[i], combo.legs[i + 1]
        if a.destination == "GRU" and b.origin in ALT_SP_AIRPORTS:
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
    """Pre-fetch: filter combos violating date-shape rules, attach flags."""
    min_gru_conn = float(constraints.get("gru_min_connection_hours", 3))
    flag_cgh = bool(constraints.get("flag_gru_cgr_mismatch", True))

    valid: List[Combo] = []
    for combo in combos:
        flags: List[str] = []

        gru_pair = _find_gru_intl_to_dom(combo)
        if gru_pair:
            a, b, _, cross_airport = gru_pair
            diff_hours = (b.date - a.date).days * 24.0
            if diff_hours == 0:
                diff_hours = 4.0  # same-day heuristic; real check runs post-fetch
            threshold = min_gru_conn + (CROSS_AIRPORT_TRANSFER_HOURS if cross_airport else 0)
            if diff_hours < threshold:
                continue  # hard reject

        if flag_cgh and _has_cgh_mismatch(combo):
            flags.append("⚠️ DOMESTIC LEG DEPARTS CGH NOT GRU — add 2h transfer + ~$30 taxi")

        if _is_separate_tickets(combo):
            flags.append("📋 SEPARATE TICKETS — no protection if earlier leg is delayed")

        combo.flags = flags
        valid.append(combo)

    return valid


def _parse_hhmm(s: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """Parse 'HH:MM' or 'HH:MM+N' → (hour, minute, day_offset). None if invalid."""
    if not s:
        return None
    day_offset = 0
    if "+" in s:
        s, plus = s.split("+", 1)
        try:
            day_offset = int(plus)
        except ValueError:
            return None
    try:
        h, m = s.split(":")
        return int(h), int(m), day_offset
    except ValueError:
        return None


def _connection_hours_real(intl_leg: dict, dom_leg: dict) -> Optional[float]:
    """Actual hours between intl arrival and domestic departure, or None if
    required time/date fields are missing.
    """
    arr = _parse_hhmm(intl_leg.get("arrive_time"))
    dep = _parse_hhmm(dom_leg.get("depart_time"))
    if not arr or not dep:
        return None
    try:
        intl_d = _date.fromisoformat(intl_leg["date"])
        dom_d = _date.fromisoformat(dom_leg["date"])
    except (KeyError, ValueError, TypeError):
        return None
    ah, am, aoff = arr
    dh, dm, doff = dep
    arrive_dt = datetime(intl_d.year, intl_d.month, intl_d.day, ah, am) + timedelta(days=aoff)
    depart_dt = datetime(dom_d.year, dom_d.month, dom_d.day, dh, dm) + timedelta(days=doff)
    return (depart_dt - arrive_dt).total_seconds() / 3600


def prune_short_gru_connections(scored, min_gru_conn: float):
    """Post-fetch: reject combos whose real GRU intl→domestic gap is below
    `min_gru_conn` (plus a 2h transfer buffer for CGH/VCP alternates).
    Combos with missing time data pass through — the pre-fetch date-based
    filter already vetted them.
    """
    kept = []
    for sc in scored:
        pair = _find_gru_intl_to_dom(sc.combo)
        if pair is None:
            kept.append(sc)
            continue
        _, _, i, cross_airport = pair
        threshold = min_gru_conn + (CROSS_AIRPORT_TRANSFER_HOURS if cross_airport else 0)
        hours = _connection_hours_real(sc.legs_data[i], sc.legs_data[i + 1])
        if hours is None or hours >= threshold:
            kept.append(sc)
    return kept


def prune_impossible_after_fetch(scored, max_travel_hours: float):
    """Second-stage filter once we know actual flight durations."""
    return [s for s in scored if s.total_travel_hours <= max_travel_hours]
