from datetime import date

from engine.routes import Combo, Leg
from engine.scorer import all_legs_found, find_best_points_option, score_combo


CONFIG = {
    "scoring_weights": {"total_cost": 0.5, "total_time": 0.3, "connection_quality": 0.2},
    "cpp_valuations": {"lifemiles": 1.4, "aeroplan": 1.8, "united_mp": 1.2, "smiles": 1.3},
    "constraints": {"baggage_checked_bags": 0},
}


def _mock_legs():
    return [
        {
            "origin": "ORD", "destination": "ADZ", "date": "2026-07-26",
            "price_usd": 400, "duration_hours": 6, "duration_str": "6h00m",
            "airline": "AV", "awards": {"lifemiles": {"points": 25000, "fees_usd": 50}},
            "booking_url": "https://kiwi.com/leg1",
        },
        {
            "origin": "ADZ", "destination": "GRU", "date": "2026-07-28",
            "price_usd": 350, "duration_hours": 8, "duration_str": "8h00m",
            "airline": "AV", "awards": {"lifemiles": {"points": 20000, "fees_usd": 30}},
            "booking_url": "https://kiwi.com/leg2",
        },
        {
            "origin": "GRU", "destination": "NVT", "date": "2026-07-29",
            "price_usd": 120, "duration_hours": 1.5, "duration_str": "1h30m",
            "airline": "G3", "awards": {"smiles": {"points": 8000, "fees_usd": 15}},
            "booking_url": "https://kiwi.com/leg3",
        },
    ]


def test_find_best_points_requires_all_legs_covered():
    legs = _mock_legs()
    # Only leg 3 has smiles; first two only have lifemiles
    best = find_best_points_option(legs, CONFIG["cpp_valuations"])
    assert best is None


def test_find_best_points_when_all_legs_have_program():
    legs = _mock_legs()
    for leg in legs:
        leg["awards"]["lifemiles"] = {"points": 10000, "fees_usd": 10}
    best = find_best_points_option(legs, CONFIG["cpp_valuations"])
    assert best["program"] == "lifemiles"
    assert best["total_points"] == 30000
    assert best["fees_usd"] == 30


def test_score_combo_returns_verdict_and_score():
    combo = Combo(
        legs=[
            Leg("ORD", "ADZ", date(2026, 7, 26), 1),
            Leg("ADZ", "GRU", date(2026, 7, 28), 2),
            Leg("GRU", "NVT", date(2026, 7, 29), 3),
        ],
        combo_type="stopover_caribbean", stopover_city="ADZ", stopover_days=2,
    )
    legs = _mock_legs()
    sc = score_combo(combo, legs, CONFIG)
    assert sc.total_cash_usd == 870
    assert sc.verdict in {"CASH WINS", "POINTS SLIGHTLY BETTER", "⚡ POINTS WIN"}
    assert len(sc.booking_links) == 3


def test_all_legs_found_false_when_any_leg_missing_cash_price():
    legs = _mock_legs()
    legs[1]["price_usd"] = None
    assert all_legs_found(legs) is False


def test_score_combo_lower_cash_produces_lower_score():
    combo = Combo(
        legs=[Leg("ORD", "GRU", date(2026, 7, 26), 1)],
        combo_type="through_direct",
    )
    cheap_leg = {
        "price_usd": 450, "duration_hours": 10, "awards": {}, "booking_url": "x",
    }
    expensive_leg = dict(cheap_leg, price_usd=500)
    sc_cheap = score_combo(combo, [cheap_leg], CONFIG)
    sc_expensive = score_combo(combo, [expensive_leg], CONFIG)
    assert sc_cheap.score < sc_expensive.score


def test_award_heavy_combo_can_flip_to_points_win():
    combo = Combo(
        legs=[Leg("ORD", "GRU", date(2026, 7, 26), 1)],
        combo_type="through_direct",
    )
    legs = [{
        "price_usd": 1000,
        "duration_hours": 10,
        "awards": {"smiles": {"points": 30000, "fees_usd": 40}},
        "booking_url": "x",
    }]
    sc = score_combo(combo, legs, CONFIG)
    assert sc.best_points_option["equiv_usd"] == 430.0
    assert sc.verdict == "⚡ POINTS WIN"


def test_total_travel_hours_is_sum_of_leg_durations():
    combo = Combo(
        legs=[
            Leg("ORD", "ADZ", date(2026, 7, 26), 1),
            Leg("ADZ", "GRU", date(2026, 7, 28), 2),
            Leg("GRU", "NVT", date(2026, 7, 29), 3),
        ],
        combo_type="stopover_caribbean",
    )
    legs = _mock_legs()
    sc = score_combo(combo, legs, CONFIG)
    assert sc.total_travel_hours == 15.5


def test_missing_duration_hours_does_not_crash_and_counts_as_zero():
    combo = Combo(legs=[Leg("ORD", "GRU", date(2026, 7, 26), 1)], combo_type="through_direct")
    sc = score_combo(combo, [{"price_usd": 200, "duration_hours": None, "awards": {}}], CONFIG)
    assert sc.total_travel_hours == 0
    assert sc.score >= 0


def test_baggage_fee_knob_changes_total_cash_and_score():
    combo = Combo(legs=[Leg("ORD", "GRU", date(2026, 7, 26), 1)], combo_type="through_direct")
    legs = [{"price_usd": 200, "duration_hours": 8, "awards": {}}]
    no_bags_cfg = dict(CONFIG, constraints={"baggage_checked_bags": 0})
    with_bags_cfg = dict(CONFIG, constraints={"baggage_checked_bags": 1})
    sc_no_bags = score_combo(combo, legs, no_bags_cfg)
    sc_with_bags = score_combo(combo, legs, with_bags_cfg)
    assert sc_no_bags.total_cash_usd == 200
    assert sc_with_bags.total_cash_usd == 240
    assert sc_with_bags.score > sc_no_bags.score
