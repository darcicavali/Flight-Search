from datetime import date

from engine.constraints import apply_constraints
from engine.routes import Combo, Leg


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
    assert any("CGH" in f for f in combos[0].flags)
