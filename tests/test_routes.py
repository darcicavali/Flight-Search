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


def test_through_tickets_produce_one_leg_direct_and_two_leg_stopover():
    cfg = dict(CFG)
    cfg["through_tickets"] = {"enabled": True}
    combos = enumerate_routes(cfg)
    by_type = {}
    for c in combos:
        by_type.setdefault(c.combo_type, []).append(c)
    assert "through_direct" in by_type
    assert "through_stopover" in by_type
    assert all(len(c.legs) == 1 for c in by_type["through_direct"])
    assert all(len(c.legs) == 2 for c in by_type["through_stopover"])
    # Through-direct leg goes straight from ORD to the final domestic destination
    assert all(c.legs[0].origin == "ORD" and c.legs[0].destination in {"NVT", "JOI"}
               for c in by_type["through_direct"])


def test_through_tickets_disabled_by_default():
    combos = enumerate_routes(CFG)
    assert all(c.combo_type in {"direct", "stopover_caribbean"} for c in combos)
