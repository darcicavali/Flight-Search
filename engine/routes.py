"""Enumerate every valid flight combination for a configured trip."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional


@dataclass
class Leg:
    origin: str
    destination: str
    date: date
    leg_number: int

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}-{self.date.isoformat()}"


@dataclass
class Combo:
    legs: List[Leg]
    combo_type: str
    stopover_city: Optional[str] = None
    stopover_days: Optional[int] = None
    flags: List[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return "|".join(leg.key for leg in self.legs)


def _date_range(start: date, end: date) -> List[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def enumerate_routes(config: dict) -> List[Combo]:
    """Generate all valid route combinations for a trip config.

    For ORD→GRU with Caribbean stopovers + domestic leg, the cross-product is
    (depart_dates) × (direct vs 3 stopovers × stop_days range) × (3 domestic dests).
    """
    window_start = date.fromisoformat(config["travel_window"]["earliest_depart"])
    window_end = date.fromisoformat(config["travel_window"]["latest_depart"])
    depart_dates = _date_range(window_start, window_end)

    final_dest = config["final_destination"]
    domestic_dests = config["domestic_leg"]["destinations"]
    stopover_candidates = config["stopovers"]["candidates"]
    min_stop = config["stopovers"]["min_days"]
    max_stop = config["stopovers"]["max_days"]

    combos: List[Combo] = []

    for depart in depart_dates:
        # Direct: ORD → GRU, then GRU → domestic (same day or next day)
        for dom_dest in domestic_dests:
            for dom_offset in (0, 1):
                combos.append(
                    Combo(
                        legs=[
                            Leg("ORD", final_dest, depart, 1),
                            Leg(final_dest, dom_dest,
                                depart + timedelta(days=dom_offset + 1), 2),
                        ],
                        combo_type="direct",
                    )
                )

        # Stopover: ORD → stopover → GRU → domestic
        for stopover in stopover_candidates:
            for stop_days in range(min_stop, max_stop + 1):
                gru_arrival = depart + timedelta(days=stop_days)
                for dom_dest in domestic_dests:
                    combos.append(
                        Combo(
                            legs=[
                                Leg("ORD", stopover, depart, 1),
                                Leg(stopover, final_dest, gru_arrival, 2),
                                Leg(final_dest, dom_dest,
                                    gru_arrival + timedelta(days=1), 3),
                            ],
                            combo_type="stopover_caribbean",
                            stopover_city=stopover,
                            stopover_days=stop_days,
                        )
                    )

    return combos


def deduplicate_legs(combos: List[Combo]) -> List[Leg]:
    """Collapse duplicate (origin, destination, date) legs across combos."""
    seen = {}
    for combo in combos:
        for leg in combo.legs:
            seen.setdefault(leg.key, leg)
    return list(seen.values())


if __name__ == "__main__":
    import yaml

    with open("config/trips.yaml") as f:
        cfg = yaml.safe_load(f)
    trip = cfg["trips"]["chicago_sao_paulo_jul2026"]
    combos = enumerate_routes(trip)
    print(f"Generated {len(combos)} combos")
    for c in combos[:5]:
        print(c.combo_type, "->", [l.key for l in c.legs])
    print(f"Unique legs: {len(deduplicate_legs(combos))}")
