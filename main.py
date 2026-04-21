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

from engine.constraints import (
    apply_constraints,
    prune_impossible_after_fetch,
    prune_short_gru_connections,
)
from engine.routes import Combo, deduplicate_legs, enumerate_routes
from engine.scorer import all_legs_found, score_combo
from fetchers.amadeus import fetch_cash_fares as fetch_amadeus
from fetchers.amadeus import merge_cash
from fetchers.kiwi import fetch_cash_fares as fetch_kiwi
from fetchers.letsfg import fetch_cash_fares as fetch_letsfg
from fetchers.seats_aero import fetch_award_fares
from fetchers.smiles import fetch_smiles_domestic
from output.digest import format_digest, format_digest_html, send_email
from output.sheets import append_to_sheets, load_previous_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("main")


def _log_fetcher_summary(name: str, data: Dict[str, dict], is_award: bool = False) -> None:
    """Log per-fetcher success/failure counts plus a few sample errors."""
    if not data:
        log.info("[%s] no data returned", name)
        return
    total = len(data)
    if is_award:
        ok = sum(1 for v in data.values() if v and (v.get("awards") or {}))
        metric = "legs with award space"
    else:
        ok = sum(1 for v in data.values() if v and v.get("price_usd") is not None)
        metric = "legs priced"
    errors: List[str] = []
    for leg_key, v in data.items():
        if not v:
            continue
        err = v.get("error")
        if err and len(errors) < 3:
            errors.append(f"{leg_key}: {err}")
    log.info("[%s] %d/%d %s", name, ok, total, metric)
    for e in errors:
        log.info("[%s] sample error → %s", name, e)


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
        kiwi_data, amadeus_data, letsfg_data, award_data, domestic_award_data = await asyncio.gather(
            fetch_kiwi(unique_legs, config, session=session),
            fetch_amadeus(unique_legs, config, session=session),
            fetch_letsfg(unique_legs, config, session=session),
            fetch_award_fares(unique_legs, config, session=session),
            fetch_smiles_domestic(unique_legs, config, session=session),
        )

    _log_fetcher_summary("kiwi", kiwi_data)
    _log_fetcher_summary("amadeus", amadeus_data)
    _log_fetcher_summary("letsfg", letsfg_data)
    _log_fetcher_summary("seats_aero", award_data, is_award=True)
    _log_fetcher_summary("smiles", domestic_award_data, is_award=True)

    cash_data = merge_cash(kiwi_data, amadeus_data, letsfg_data)
    priced = sum(1 for v in cash_data.values() if v and v.get("price_usd") is not None)
    log.info("Merged cash coverage: %d/%d legs priced across all sources",
             priced, len(cash_data))

    scored = []
    skipped = 0
    for combo in combos:
        legs_data = _assemble_legs(combo, cash_data, award_data, domestic_award_data)
        if not all_legs_found(legs_data):
            skipped += 1
            continue
        scored.append(score_combo(combo, legs_data, config))

    log.info("Scored %d combos (%d skipped: missing leg fares)", len(scored), skipped)

    constraints_cfg = config.get("constraints", {})
    max_travel = float(constraints_cfg.get("max_total_travel_hours", 36))
    min_gru_conn = float(constraints_cfg.get("gru_min_connection_hours", 3))

    before_travel = len(scored)
    scored = prune_impossible_after_fetch(scored, max_travel)
    before_conn = len(scored)
    scored = prune_short_gru_connections(scored, min_gru_conn)
    scored.sort(key=lambda x: x.score)
    log.info(
        "Post-fetch filtering: %d→%d (travel-time), %d→%d (GRU connection)",
        before_travel, before_conn, before_conn, len(scored),
    )

    prev_day = load_previous_day()
    digest = format_digest(scored, date.today(), config, prev_day_data=prev_day)
    digest_html = format_digest_html(scored, date.today(), config, prev_day_data=prev_day)

    if dry_run:
        print(digest)
    else:
        recipient = (
            os.environ.get("RECIPIENT_EMAIL")
            or config.get("notify", {}).get("email")
            or ""
        )
        sent = send_email(digest, recipient, html=digest_html)
        if not sent:
            print("\n[email not sent — falling back to stdout]\n")
            print(digest)

    append_to_sheets(scored, config)
    log.info("Done. (%d combos ranked)", len(scored))
    # Exit 0 even with zero scored combos — the digest was produced/delivered.
    # Empty results are a data-coverage signal, not a pipeline failure.
    return 0


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
