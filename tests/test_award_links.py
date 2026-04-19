from datetime import date

from engine.routes import Combo, Leg
from engine.scorer import score_combo
from output.award_links import build_award_links
from output.digest import format_digest


CPP = {
    "lifemiles": 1.4, "aeroplan": 1.8, "united_mp": 1.2,
    "flying_blue": 1.5, "smiles": 1.3,
}


def test_intl_leg_returns_intl_programs():
    links = build_award_links("ORD", "GRU", "2026-07-26", CPP)
    programs = {l.program for l in links}
    assert "aeroplan" in programs
    assert "lifemiles" in programs
    assert "united_mp" in programs
    assert "flying_blue" in programs
    # No Smiles for an international leg
    assert "smiles" not in programs


def test_br_domestic_leg_returns_domestic_programs():
    links = build_award_links("GRU", "NVT", "2026-07-29", CPP)
    programs = {l.program for l in links}
    assert "smiles" in programs
    # No international programs for a domestic leg
    assert "aeroplan" not in programs


def test_cpp_filter_excludes_programs_without_valuation():
    cpp_minimal = {"lifemiles": 1.4}
    links = build_award_links("ORD", "GRU", "2026-07-26", cpp_minimal)
    programs = {l.program for l in links}
    assert programs == {"lifemiles"}


def test_urls_include_leg_params():
    links = build_award_links("ORD", "GRU", "2026-07-26", CPP)
    for link in links:
        assert "ORD" in link.url
        assert "GRU" in link.url
        assert "2026-07-26" in link.url


def test_digest_includes_manual_award_check_for_legs_without_awards():
    config = {
        "origin": "ORD",
        "travel_window": {"earliest_depart": "2026-07-26",
                          "latest_depart": "2026-08-02"},
        "scoring_weights": {"total_cost": 0.5, "total_time": 0.3,
                            "connection_quality": 0.2},
        "cpp_valuations": CPP,
        "constraints": {"baggage_checked_bags": 0},
    }
    combo = Combo(
        legs=[
            Leg("ORD", "GRU", date(2026, 7, 26), 1),
            Leg("GRU", "NVT", date(2026, 7, 27), 2),
        ],
        combo_type="direct",
    )
    legs_data = [
        {"origin": "ORD", "destination": "GRU", "date": "2026-07-26",
         "price_usd": 750, "airline": "UA", "duration_hours": 10,
         "duration_str": "10h00m", "awards": {}, "booking_url": None},
        {"origin": "GRU", "destination": "NVT", "date": "2026-07-27",
         "price_usd": 120, "airline": "G3", "duration_hours": 1.5,
         "duration_str": "1h30m", "awards": {}, "booking_url": None},
    ]
    sc = score_combo(combo, legs_data, config)
    digest = format_digest([sc], date(2026, 4, 19), config)
    assert "MANUAL AWARD CHECK" in digest
    assert "aircanada.com" in digest or "Aeroplan" in digest
    assert "smiles.com.br" in digest or "Smiles" in digest


def test_digest_skips_manual_check_for_legs_with_award_data():
    config = {
        "origin": "ORD",
        "travel_window": {"earliest_depart": "2026-07-26",
                          "latest_depart": "2026-08-02"},
        "scoring_weights": {"total_cost": 0.5, "total_time": 0.3,
                            "connection_quality": 0.2},
        "cpp_valuations": CPP,
        "constraints": {"baggage_checked_bags": 0},
    }
    combo = Combo(
        legs=[Leg("ORD", "GRU", date(2026, 7, 26), 1)],
        combo_type="direct",
    )
    legs_data = [
        {"origin": "ORD", "destination": "GRU", "date": "2026-07-26",
         "price_usd": 750, "airline": "UA", "duration_hours": 10,
         "duration_str": "10h00m",
         "awards": {"aeroplan": {"points": 60000, "fees_usd": 90}},
         "booking_url": None},
    ]
    sc = score_combo(combo, legs_data, config)
    digest = format_digest([sc], date(2026, 4, 19), config)
    # All legs have award data, so manual section should be the placeholder
    assert "all legs have automated award data" in digest
