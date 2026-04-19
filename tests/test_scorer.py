from datetime import date

from engine.routes import Combo, Leg
from engine.scorer import find_best_points_option, score_combo


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
