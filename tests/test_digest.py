from datetime import date

from engine.routes import Combo, Leg
from engine.scorer import score_combo
from output.digest import format_digest, format_digest_html


CONFIG = {
    "origin": "ORD",
    "travel_window": {"earliest_depart": "2026-07-26", "latest_depart": "2026-08-02"},
    "scoring_weights": {"total_cost": 0.5, "total_time": 0.3, "connection_quality": 0.2},
    "cpp_valuations": {"lifemiles": 1.4, "aeroplan": 1.8, "united_mp": 1.2, "smiles": 1.3},
    "constraints": {"baggage_checked_bags": 0},
}


def _sample_scored():
    combo = Combo(
        legs=[
            Leg("ORD", "GRU", date(2026, 7, 26), 1),
            Leg("GRU", "NVT", date(2026, 7, 27), 2),
        ],
        combo_type="direct",
    )
    combo.flags = ["📋 SEPARATE TICKETS — no protection if earlier leg is delayed"]
    legs_data = [
        {"origin": "ORD", "destination": "GRU", "date": "2026-07-26",
         "price_usd": 750, "airline": "UA", "duration_hours": 10,
         "duration_str": "10h00m", "awards": {
             "united_mp": {"points": 40000, "fees_usd": 80}}, "booking_url": "https://ua.com/x"},
        {"origin": "GRU", "destination": "NVT", "date": "2026-07-27",
         "price_usd": 120, "airline": "G3", "duration_hours": 1.5,
         "duration_str": "1h30m", "awards": {
             "smiles": {"points": 7000, "fees_usd": 15}}, "booking_url": "https://gol.com/y"},
    ]
    return score_combo(combo, legs_data, CONFIG)


def test_format_digest_contains_key_sections():
    scored = [_sample_scored()]
    out = format_digest(scored, date(2026, 4, 19), CONFIG)
    assert "FLIGHT DIGEST" in out
    # Full Ranking is the canonical "best combos" view; "Best Cash Combo"
    # was dropped because it was always the same as rank #1.
    assert "FULL RANKING" in out
    assert "BEST DIRECT" in out
    assert "AWARD SPACE ALERTS" in out


def test_format_digest_handles_empty():
    out = format_digest([], date(2026, 4, 19), CONFIG)
    assert "No complete flight combinations" in out


def test_html_digest_contains_sections_and_hides_urls_in_text():
    scored = [_sample_scored()]
    html = format_digest_html(scored, date(2026, 4, 19), CONFIG)
    assert "Full Ranking" in html
    assert "Best Direct" in html
    # Booking URL should live inside an <a href="…"> not as visible text
    assert 'href="https://ua.com/x"' in html
    assert ">Book leg 1</a>" in html


def test_codeshare_carrier_rendered_with_operator():
    combo = Combo(legs=[Leg("ORD", "GRU", date(2026, 7, 26), 1)], combo_type="direct")
    legs_data = [{
        "origin": "ORD", "destination": "GRU", "date": "2026-07-26",
        "price_usd": 750, "airline": "BA", "operated_by": "UA",
        "duration_hours": 10, "duration_str": "10h00m",
        "awards": {}, "booking_url": "https://example.com/x",
        "segments": [{"carrier": "BA", "flight_no": "0117"}],
    }]
    sc = score_combo(combo, legs_data, CONFIG)
    text_out = format_digest([sc], date(2026, 4, 19), CONFIG)
    html_out = format_digest_html([sc], date(2026, 4, 19), CONFIG)
    assert "British Airways (op. by United" in text_out
    assert "British Airways (op. by United" in html_out


def test_per_leg_alternatives_renders_per_route_top_5():
    scored = [_sample_scored()]
    per_leg_offers = {
        "ORD-GRU-2026-07-26": [
            {"origin": "ORD", "destination": "GRU", "date": "2026-07-26",
             "price_usd": 750, "airline": "UA", "duration_str": "10h00m",
             "stops": 0, "segments": [{"carrier": "UA", "flight_no": "UA823"}],
             "booking_url": "https://ua.com/x", "depart_time": "10:00",
             "arrive_time": "21:00", "duration_hours": 10},
            {"origin": "ORD", "destination": "GRU", "date": "2026-07-26",
             "price_usd": 820, "airline": "AA", "duration_str": "11h00m",
             "stops": 1, "layovers": ["MIA 2h"], "depart_time": "08:00",
             "segments": [{"carrier": "AA", "flight_no": "AA456"}],
             "booking_url": "https://aa.com/y", "duration_hours": 11},
        ],
        "GRU-NVT-2026-07-27": [
            {"origin": "GRU", "destination": "NVT", "date": "2026-07-27",
             "price_usd": 100, "airline": "G3", "duration_str": "1h30m",
             "stops": 0, "segments": [{"carrier": "G3", "flight_no": "G31000"}],
             "booking_url": "https://gol.com/y", "duration_hours": 1.5},
        ],
    }
    html = format_digest_html(scored, date(2026, 4, 19), CONFIG,
                              per_leg_offers=per_leg_offers)
    text = format_digest(scored, date(2026, 4, 19), CONFIG,
                         per_leg_offers=per_leg_offers)
    # Section header rendered
    assert "Top alternatives per route" in html
    assert "TOP ALTERNATIVES PER ROUTE" in text
    # Both ORD→GRU offers appear, sorted cheapest-first
    assert "ORD → GRU" in html
    assert html.find("750") < html.find("820")
    # GRU→NVT also appears
    assert "GRU → NVT" in html


def test_non_codeshare_does_not_annotate():
    combo = Combo(legs=[Leg("ORD", "GRU", date(2026, 7, 26), 1)], combo_type="direct")
    legs_data = [{
        "origin": "ORD", "destination": "GRU", "date": "2026-07-26",
        "price_usd": 750, "airline": "UA", "operated_by": None,
        "duration_hours": 10, "duration_str": "10h00m",
        "awards": {}, "booking_url": "https://ua.com/x",
    }]
    sc = score_combo(combo, legs_data, CONFIG)
    out = format_digest([sc], date(2026, 4, 19), CONFIG)
    assert "op. by" not in out
