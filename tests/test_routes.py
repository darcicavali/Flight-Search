from datetime import date

from engine.routes import deduplicate_legs, enumerate_routes


CFG = {
    "origin": "ORD",
    "final_destination": "GRU",
    "stopovers": {"candidates": ["ADZ", "CTG"], "min_days": 2, "max_days": 3},
    "domestic_leg": {"origin": "GRU", "destinations": ["NVT", "JOI"]},
    "travel_window": {"earliest_depart": "2026-07-26", "latest_depart": "2026-07-28"},
}


def test_enumerate_produces_direct_and_stopover():
    combos = enumerate_routes(CFG)
    types = {c.combo_type for c in combos}
    assert "direct" in types
    assert "stopover_caribbean" in types


def test_direct_has_two_legs_stopover_has_three():
    combos = enumerate_routes(CFG)
    for c in combos:
        if c.combo_type == "direct":
            assert len(c.legs) == 2
        elif c.combo_type == "stopover_caribbean":
            assert len(c.legs) == 3


def test_dedup_collapses_shared_legs():
    combos = enumerate_routes(CFG)
    legs = deduplicate_legs(combos)
    keys = [l.key for l in legs]
    assert len(keys) == len(set(keys))


def test_alternate_domestic_origin_produces_cgh_combos():
    cfg = dict(CFG)
    cfg["domestic_leg"] = {
        "origin": "GRU",
        "alternate_origins": ["CGH"],
        "destinations": ["NVT", "JOI"],
    }
    combos = enumerate_routes(cfg)
    dom_origins = {c.legs[-1].origin for c in combos}
    assert "GRU" in dom_origins
    assert "CGH" in dom_origins
