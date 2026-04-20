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


def test_filters_same_day_gru_connection_below_minimum():
    constraints = {"gru_min_connection_hours": 3, "max_total_travel_hours": 60}
    # Same-day connection yields 4h by heuristic → should PASS (4h > 3h)
    combos = apply_constraints([_combo_stopover_same_day_gru_to_nvt()], constraints)
    assert len(combos) == 1  # 4h heuristic > 3h threshold


def test_hard_reject_when_min_connection_over_four_hours():
    constraints = {"gru_min_connection_hours": 5, "max_total_travel_hours": 60}
    combos = apply_constraints([_combo_stopover_same_day_gru_to_nvt()], constraints)
    assert len(combos) == 0


def test_next_day_transfer_passes():
    constraints = {"gru_min_connection_hours": 3, "max_total_travel_hours": 60}
    combos = apply_constraints([_combo_next_day_transfer()], constraints)
    assert len(combos) == 1


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


def test_through_direct_gets_single_ticket_flag_not_separate_tickets():
    combo = Combo(
        legs=[Leg("ORD", "NVT", date(2026, 7, 26), 1)],
        combo_type="through_direct",
    )
    combos = apply_constraints([combo], {"gru_min_connection_hours": 3})
    assert len(combos) == 1
    flags = combos[0].flags
    assert any("SINGLE TICKET" in f for f in flags)
    assert not any("SEPARATE TICKETS" in f for f in flags)


def test_through_stopover_single_pnr_flag_no_separate_flag():
    combo = Combo(
        legs=[
            Leg("ORD", "AUA", date(2026, 7, 26), 1),
            Leg("AUA", "NVT", date(2026, 7, 28), 2),  # stopover→dom as one ticket
        ],
        combo_type="through_stopover",
        stopover_city="AUA",
        stopover_days=2,
    )
    combos = apply_constraints([combo], {"gru_min_connection_hours": 3})
    assert len(combos) == 1
    flags = combos[0].flags
    assert any("SINGLE-PNR" in f for f in flags)
    assert not any("SEPARATE TICKETS" in f for f in flags)


def test_classic_stopover_still_gets_separate_tickets_flag():
    combo = _combo_next_day_transfer()  # ORD→ADZ, ADZ→GRU, GRU→NVT
    combo.combo_type = "stopover_caribbean"
    combos = apply_constraints([combo], {"gru_min_connection_hours": 3})
    assert len(combos) == 1
    assert any("SEPARATE TICKETS" in f for f in combos[0].flags)
