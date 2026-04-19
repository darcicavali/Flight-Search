"""Scoring engine: combine cash, points, time into a single rank."""

from dataclasses import dataclass, field
from typing import List, Optional

from engine.routes import Combo


@dataclass
class ScoredCombo:
    combo: Combo
    legs_data: List[dict]
    total_cash_usd: float
    best_points_option: Optional[dict]
    total_travel_hours: float
    score: float
    verdict: str
    booking_links: List[dict] = field(default_factory=list)


def estimate_baggage_fees(legs_data: List[dict], constraints: dict) -> float:
    """Flat per-leg checked-bag estimate. Domestic BR LCC legs charge more."""
    bags = int(constraints.get("baggage_checked_bags", 0))
    if bags <= 0:
        return 0.0
    fee_per_leg = 40.0  # rough; tune after observing real totals
    return bags * fee_per_leg * len(legs_data)


def find_best_points_option(legs_data: List[dict], cpp_valuations: dict) -> Optional[dict]:
    """Find the single loyalty program that covers ALL legs at lowest equiv USD."""
    if not legs_data:
        return None
    # Set of programs present on the first leg
    programs = set((legs_data[0].get("awards") or {}).keys())
    for leg in legs_data[1:]:
        programs &= set((leg.get("awards") or {}).keys())

    best = None
    for program in programs:
        total_points = sum(leg["awards"][program]["points"] for leg in legs_data)
        total_fees = sum(leg["awards"][program]["fees_usd"] for leg in legs_data)
        cpp = cpp_valuations.get(program, 1.0)
        equiv_usd = (total_points * cpp / 100.0) + total_fees
        if best is None or equiv_usd < best["equiv_usd"]:
            best = {
                "program": program,
                "total_points": total_points,
                "fees_usd": round(total_fees, 2),
                "equiv_usd": round(equiv_usd, 2),
                "cpp_used": cpp,
            }
    return best


def extract_booking_links(legs_data: List[dict]) -> List[dict]:
    out = []
    for i, leg in enumerate(legs_data):
        if leg.get("booking_url"):
            out.append({"leg": i + 1, "url": leg["booking_url"]})
    return out


def _connection_quality(combo: Combo, legs_data: List[dict]) -> float:
    """Score 0..1 (higher = better). Penalizes 0-day connections and very long waits."""
    if len(combo.legs) < 2:
        return 1.0
    scores = []
    for i in range(len(combo.legs) - 1):
        diff = (combo.legs[i + 1].date - combo.legs[i].date).days
        if diff <= 0:
            scores.append(0.3)  # same day — risky
        elif diff == 1:
            scores.append(1.0)  # ideal
        elif diff <= 4:
            scores.append(0.8)  # stopover territory
        else:
            scores.append(0.5)  # too long
    return sum(scores) / len(scores)


def score_combo(combo: Combo, legs_data: List[dict], config: dict) -> ScoredCombo:
    weights = config["scoring_weights"]
    cpp = config["cpp_valuations"]
    constraints = config.get("constraints", {})

    total_cash = sum((leg.get("price_usd") or 0) for leg in legs_data)
    total_cash += estimate_baggage_fees(legs_data, constraints)

    best_points = find_best_points_option(legs_data, cpp)
    total_time = sum((leg.get("duration_hours") or 0) for leg in legs_data)

    cost_score = (total_cash or 0) / 1000.0
    time_score = (total_time or 0) / 40.0
    conn_quality = _connection_quality(combo, legs_data)
    conn_score = 1.0 - conn_quality  # lower is better

    score = (
        weights["total_cost"] * cost_score
        + weights["total_time"] * time_score
        + weights["connection_quality"] * conn_score
    )

    if best_points and total_cash and best_points["equiv_usd"] < total_cash * 0.85:
        verdict = "⚡ POINTS WIN"
    elif best_points and total_cash and best_points["equiv_usd"] < total_cash:
        verdict = "POINTS SLIGHTLY BETTER"
    elif not total_cash:
        verdict = "CHECK MANUALLY"
    else:
        verdict = "CASH WINS"

    return ScoredCombo(
        combo=combo,
        legs_data=legs_data,
        total_cash_usd=round(total_cash, 2),
        best_points_option=best_points,
        total_travel_hours=round(total_time, 2),
        score=round(score, 4),
        verdict=verdict,
        booking_links=extract_booking_links(legs_data),
    )


def all_legs_found(legs_data: List[dict]) -> bool:
    return all(leg.get("price_usd") is not None for leg in legs_data)
