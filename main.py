"""Flight digest orchestrator — entry point for daily runs and manual testing."""

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from typing import Dict, List

import aiohttp
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from engine.constraints import apply_constraints, prune_impossible_after_fetch
from engine.routes import Combo, deduplicate_legs, enumerate_routes
from engine.scorer import all_legs_found, score_combo
from fetchers.amadeus import fetch_cash_fares as fetch_amadeus
from fetchers.amadeus import merge_cash
from fetchers.duffel import fetch_cash_fares as fetch_duffel
from fetchers.kiwi import fetch_cash_fares as fetch_kiwi
from fetchers.seats_aero import fetch_award_fares
from fetchers.smiles import fetch_smiles_domestic
from output.digest import format_digest, send_email
from output.sheets import append_to_sheets, load_previous_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("main")


def _assemble_legs(
    combo: Combo,
    cash_data: Dict[str, dict],
    award_data: Dict[str, dict],
    domestic_award_data: Dict[str, dict],
) -> List[dict]:
    """Merge cash + awards (intl + domestic) for each leg in the combo."""
    merged: List[dict] = []
    for leg in combo.legs:
        cash = cash_data.get(leg.key) or {
            "origin": leg.origin, "destination": leg.destination,
            "date": leg.date.isoformat(), "price_usd": None,
            "airline": None, "duration_hours": None, "duration_str": None,
            "stops": None, "booking_url": None, "awards": {}, "source": None,
        }
        leg_data = dict(cash)
        leg_data["awards"] = dict(cash.get("awards") or {})

        intl_awards = (award_data.get(leg.key) or {}).get("awards") or {}
        dom_awards = (domestic_award_data.get(leg.key) or {}).get("awards") or {}
        leg_data["awards"].update(intl_awards)
        leg_data["awards"].update(dom_awards)
        merged.append(leg_data)
    return merged


async def run_trip(trip_name: str, config: dict, dry_run: bool = False) -> int:
    log.info("Running flight search for: %s", trip_name)

    combos = enumerate_routes(config)
    log.info("Generated %d route combinations", len(combos))

    combos = apply_constraints(combos, config.get("constraints", {}))
    log.info("%d combos after constraint filtering", len(combos))

    unique_legs = deduplicate_legs(combos)
    log.info("Fetching fares for %d unique legs", len(unique_legs))

    async with aiohttp.ClientSession() as session:
        kiwi_data, amadeus_data, duffel_data, award_data, domestic_award_data = await asyncio.gather(
            fetch_kiwi(unique_legs, config, session=session),
            fetch_amadeus(unique_legs, config, session=session),
            fetch_duffel(unique_legs, config, session=session),
            fetch_award_fares(unique_legs, config, session=session),
            fetch_smiles_domestic(unique_legs, config, session=session),
        )
    cash_data = merge_cash(kiwi_data, amadeus_data, duffel_data)

    scored = []
    skipped = 0
    for combo in combos:
        legs_data = _assemble_legs(combo, cash_data, award_data, domestic_award_data)
        if not all_legs_found(legs_data):
            skipped += 1
            continue
        scored.append(score_combo(combo, legs_data, config))

    log.info("Scored %d combos (%d skipped: missing leg fares)", len(scored), skipped)

    max_travel = float(config.get("constraints", {}).get("max_total_travel_hours", 36))
    scored = prune_impossible_after_fetch(scored, max_travel)
    scored.sort(key=lambda x: x.score)
    log.info("%d combos after post-fetch filtering", len(scored))

    prev_day = load_previous_day()
    digest = format_digest(scored, date.today(), config, prev_day_data=prev_day)

    if dry_run:
        print(digest)
    else:
        recipient = (
            os.environ.get("RECIPIENT_EMAIL")
            or config.get("notify", {}).get("email")
            or ""
        )
        sent = send_email(digest, recipient)
        if not sent:
            print("\n[email not sent — falling back to stdout]\n")
            print(digest)

    append_to_sheets(scored, config)
    log.info("Done.")
    return 0 if scored else 2


def main():
    parser = argparse.ArgumentParser(description="Flight digest runner")
    parser.add_argument("--trip", required=True, help="trip key in config/trips.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="print digest instead of sending email")
    parser.add_argument("--config", default="config/trips.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        all_cfg = yaml.safe_load(f)
    if args.trip not in all_cfg["trips"]:
        log.error("Unknown trip '%s'. Available: %s",
                  args.trip, list(all_cfg["trips"].keys()))
        sys.exit(1)

    trip_cfg = all_cfg["trips"][args.trip]
    exit_code = asyncio.run(run_trip(args.trip, trip_cfg, dry_run=args.dry_run))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
