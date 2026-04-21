"""Hard constraints that filter or flag combos — both pre- and post-fetch.

`apply_constraints` runs BEFORE fetching fares and only uses combo shape.
`prune_short_gru_connections` runs AFTER fetching and uses real flight times.
"""

from datetime import date as _date
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from engine.routes import Combo

# Airports where an international arrival can hand off to a domestic Brazil
# leg. Both GRU (São Paulo) and GIG (Rio) serve this role in our routings.
BRAZIL_GATEWAYS = {"GRU", "GIG"}


def _find_gru_intl_to_dom(combo: Combo):
    """Return (intl_leg, dom_leg, i) if combo hands off intl→domestic at a
    Brazil gateway (GRU or GIG)."""
    for i in range(len(combo.legs) - 1):
        a, b = combo.legs[i], combo.legs[i + 1]
        if (a.destination in BRAZIL_GATEWAYS
                and b.origin in BRAZIL_GATEWAYS
                and a.destination == b.origin):
            return a, b, i
    return None


def _is_separate_tickets(combo: Combo) -> bool:
    """Only true when the intl→domestic handoff at a Brazil gateway crosses a
    leg boundary. Through-ticket combos fold that handoff into a single PNR.
    """
    return _find_gru_intl_to_dom(combo) is not None


def apply_constraints(combos: List[Combo], constraints: dict) -> List[Combo]:
    """Pre-fetch: filter combos violating date-shape rules, attach flags."""
    min_gru_conn = float(constraints.get("gru_min_connection_hours", 3))

    valid: List[Combo] = []
    for combo in combos:
        flags: List[str] = []

        gru_pair = _find_gru_intl_to_dom(combo)
        if gru_pair:
            a, b, _ = gru_pair
            diff_hours = (b.date - a.date).days * 24.0
            if diff_hours == 0:
                diff_hours = 4.0  # same-day heuristic; real check runs post-fetch
            if diff_hours < min_gru_conn:
                continue  # hard reject

        if _is_separate_tickets(combo):
            flags.append("📋 SEPARATE TICKETS — no protection if earlier leg is delayed")

        if combo.combo_type == "through_direct":
            flags.append("🎫 SINGLE TICKET — airline owns all connections end to end")
        elif combo.combo_type == "through_stopover":
            flags.append("🎫 SINGLE-PNR LEGS — stopover→destination is one booking")

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
    `min_gru_conn`. Combos with missing time data pass through — the pre-fetch
    date-based filter already vetted them.
    """
    kept = []
    for sc in scored:
        pair = _find_gru_intl_to_dom(sc.combo)
        if pair is None:
            kept.append(sc)
            continue
        _, _, i = pair
        hours = _connection_hours_real(sc.legs_data[i], sc.legs_data[i + 1])
        if hours is None or hours >= min_gru_conn:
            kept.append(sc)
    return kept


def prune_impossible_after_fetch(scored, max_travel_hours: float):
    """Second-stage filter once we know actual flight durations."""
    return [s for s in scored if s.total_travel_hours <= max_travel_hours]
