from dataclasses import dataclass
from datetime import date
from typing import List

from engine.constraints import apply_constraints, prune_short_gru_connections
from engine.routes import Combo, Leg


@dataclass
class _FakeScored:
    combo: Combo
    legs_data: List[dict]


def _leg_data(origin, dest, d, depart=None, arrive=None):
    return {
        "origin": origin, "destination": dest, "date": d,
        "depart_time": depart, "arrive_time": arrive,
    }


def _combo_stopover_same_day_gru_to_nvt():
    # Intl arrives GRU 2026-07-28, domestic GRU→NVT same day — violates 3h min.
    return Combo(
        legs=[
            Leg("ORD", "ADZ", date(2026, 7, 26), 1),
            Leg("ADZ", "GRU", date(2026, 7, 28), 2),
            Leg("GRU", "NVT", date(2026, 7, 28), 3),
        ],
        combo_type="stopover_caribbean",
        stopover_city="ADZ",
        stopover_days=2,
    )


def _combo_next_day_transfer():
    return Combo(
        legs=[
            Leg("ORD", "ADZ", date(2026, 7, 26), 1),
            Leg("ADZ", "GRU", date(2026, 7, 28), 2),
            Leg("GRU", "NVT", date(2026, 7, 29), 3),
        ],
        combo_type="stopover_caribbean",
        stopover_city="ADZ",
        stopover_days=2,
    )


def _combo_cgh_mismatch():
    return Combo(
        legs=[
            Leg("ORD", "GRU", date(2026, 7, 26), 1),
            Leg("CGH", "NVT", date(2026, 7, 27), 2),
        ],
        combo_type="direct",
    )


def test_filters_same_day_gru_connection_below_minimum():
    constraints = {"gru_min_connection_hours": 3, "max_total_travel_hours": 60,
                   "flag_gru_cgr_mismatch": True}
    # Same-day connection yields 4h by heuristic → should PASS (4h > 3h)
    combos = apply_constraints([_combo_stopover_same_day_gru_to_nvt()], constraints)
    assert len(combos) == 1  # 4h heuristic > 3h threshold


def test_hard_reject_when_min_connection_over_four_hours():
    constraints = {"gru_min_connection_hours": 5, "max_total_travel_hours": 60,
                   "flag_gru_cgr_mismatch": True}
    combos = apply_constraints([_combo_stopover_same_day_gru_to_nvt()], constraints)
    assert len(combos) == 0


def test_next_day_transfer_passes():
    constraints = {"gru_min_connection_hours": 3, "max_total_travel_hours": 60,
                   "flag_gru_cgr_mismatch": True}
    combos = apply_constraints([_combo_next_day_transfer()], constraints)
    assert len(combos) == 1


def test_cgh_mismatch_flagged():
    constraints = {"gru_min_connection_hours": 3, "max_total_travel_hours": 60,
                   "flag_gru_cgr_mismatch": True}
    combos = apply_constraints([_combo_cgh_mismatch()], constraints)
    assert len(combos) == 1
    assert any("CGH" in f for f in combos[0].flags)


def test_real_time_gru_connection_rejects_short_gap():
    """Intl arrives GRU 22:00; domestic departs GRU 23:30 same day → 1.5h < 3h."""
    combo = _combo_next_day_transfer()
    # Override the domestic leg date to match the intl arrival day → real gap.
    combo.legs[2] = Leg("GRU", "NVT", date(2026, 7, 28), 3)
    legs_data = [
        _leg_data("ORD", "ADZ", "2026-07-26", depart="10:00", arrive="14:00"),
        _leg_data("ADZ", "GRU", "2026-07-28", depart="18:00", arrive="22:00"),
        _leg_data("GRU", "NVT", "2026-07-28", depart="23:30", arrive="00:45+1"),
    ]
    scored = [_FakeScored(combo=combo, legs_data=legs_data)]
    kept = prune_short_gru_connections(scored, min_gru_conn=3.0)
    assert kept == []


def test_real_time_gru_connection_keeps_adequate_gap():
    """Intl arrives 18:00, domestic departs 22:00 → 4h > 3h."""
    combo = _combo_next_day_transfer()
    combo.legs[2] = Leg("GRU", "NVT", date(2026, 7, 28), 3)
    legs_data = [
        _leg_data("ORD", "ADZ", "2026-07-26", depart="10:00", arrive="14:00"),
        _leg_data("ADZ", "GRU", "2026-07-28", depart="12:00", arrive="18:00"),
        _leg_data("GRU", "NVT", "2026-07-28", depart="22:00", arrive="23:15"),
    ]
    scored = [_FakeScored(combo=combo, legs_data=legs_data)]
    kept = prune_short_gru_connections(scored, min_gru_conn=3.0)
    assert len(kept) == 1


def test_real_time_gru_connection_passes_when_times_missing():
    combo = _combo_next_day_transfer()
    legs_data = [
        _leg_data("ORD", "ADZ", "2026-07-26"),
        _leg_data("ADZ", "GRU", "2026-07-28"),
        _leg_data("GRU", "NVT", "2026-07-29"),
    ]
    scored = [_FakeScored(combo=combo, legs_data=legs_data)]
    kept = prune_short_gru_connections(scored, min_gru_conn=3.0)
    assert len(kept) == 1


def test_cgh_cross_airport_adds_2h_transfer_buffer():
    """GRU→CGH transfer. Gap 4h — under 3h+2h threshold, should reject."""
    combo = Combo(
        legs=[
            Leg("ORD", "GRU", date(2026, 7, 26), 1),
            Leg("CGH", "NVT", date(2026, 7, 27), 2),
        ],
        combo_type="direct",
    )
    legs_data = [
        _leg_data("ORD", "GRU", "2026-07-26", depart="22:00", arrive="09:00+1"),
        _leg_data("CGH", "NVT", "2026-07-27", depart="13:00", arrive="14:30"),
    ]
    scored = [_FakeScored(combo=combo, legs_data=legs_data)]
    # 4h gap < (3 + 2) = 5h threshold → reject
    kept = prune_short_gru_connections(scored, min_gru_conn=3.0)
    assert kept == []

    # Extend domestic departure to 16:00 → 7h gap > 5h → accept
    legs_data[1]["depart_time"] = "16:00"
    kept = prune_short_gru_connections(scored, min_gru_conn=3.0)
    assert len(kept) == 1
