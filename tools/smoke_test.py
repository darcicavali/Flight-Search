"""Local smoke test for the LetsFG fetcher.

Runs the real LetsFG fetcher against a small sample of legs from the trip
config (or ad-hoc legs passed on the CLI) and reports per-leg timings plus
coverage. Use this to estimate CI runtime before pushing — the daily run
fetches one leg per (origin, dest, date) in the enumerated route set, so
total CI time ≈ (total_unique_legs ÷ concurrency) × per-leg time.

Usage:
  python -m tools.smoke_test --trip chicago_sao_paulo_jul2026 --sample 3
  python -m tools.smoke_test --leg ORD:GRU:2026-07-26 --leg GRU:NVT:2026-07-28
  python -m tools.smoke_test --trip chicago_sao_paulo_jul2026 --all
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import date
from typing import List

import aiohttp
import yaml

from engine.routes import Leg, deduplicate_legs, enumerate_routes
from fetchers.letsfg import fetch_cash_fares


def _parse_leg_spec(spec: str) -> Leg:
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"bad --leg '{spec}', expected ORIG:DEST:YYYY-MM-DD")
    origin, dest, iso = parts
    return Leg(origin, dest, date.fromisoformat(iso), 1)


def _pick_sample(legs: List[Leg], n: int) -> List[Leg]:
    """Pick a spread-out sample: first, last, and evenly-spaced middle."""
    if n >= len(legs):
        return legs
    if n <= 1:
        return legs[:1]
    step = (len(legs) - 1) / (n - 1)
    idxs = sorted({int(round(i * step)) for i in range(n)})
    return [legs[i] for i in idxs]


async def _run(legs: List[Leg], config: dict) -> None:
    print(f"\nSmoke-testing {len(legs)} legs via LetsFG "
          f"(mode={config.get('fetchers', {}).get('letsfg', {}).get('mode', 'fast')})\n")
    print(f"{'#':<3} {'leg':<28} {'elapsed':>8}  {'price':>10}  {'airline':<8}  status")
    print("-" * 80)

    async with aiohttp.ClientSession() as session:
        t0 = time.perf_counter()
        # Run legs one at a time so we can measure each. The fetcher itself
        # supports concurrency; we're deliberately serial here to get per-leg timings.
        results = {}
        per_leg_times = []
        for i, leg in enumerate(legs, 1):
            t_leg = time.perf_counter()
            res = await fetch_cash_fares([leg], config, session=session)
            dt = time.perf_counter() - t_leg
            per_leg_times.append(dt)
            data = res.get(leg.key) or {}
            price = data.get("price_usd")
            airline = data.get("airline") or "-"
            err = data.get("error")
            status = "ok" if price is not None else f"miss: {err or '—'}"
            price_str = f"${price:.2f}" if price is not None else "—"
            print(f"{i:<3} {leg.key:<28} {dt:>7.1f}s  {price_str:>10}  {airline:<8}  {status}")
            results[leg.key] = data
        total = time.perf_counter() - t0

    priced = sum(1 for v in results.values() if v.get("price_usd") is not None)
    avg = (sum(per_leg_times) / len(per_leg_times)) if per_leg_times else 0.0
    print("-" * 80)
    print(f"Priced: {priced}/{len(legs)}   total: {total:.1f}s   avg/leg: {avg:.1f}s")

    concurrency = int((config.get("fetchers") or {}).get("letsfg", {}).get("concurrency", 2))
    print(f"\nCI projection at concurrency={concurrency}:")
    for total_legs in (10, 20, 40, 80):
        projected = (total_legs / concurrency) * avg if avg else 0
        print(f"  {total_legs:>3} unique legs → ~{projected:>5.0f}s ({projected/60:.1f} min)")


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="LetsFG smoke test")
    parser.add_argument("--trip", help="trip key in config/trips.yaml")
    parser.add_argument("--config", default="config/trips.yaml")
    parser.add_argument("--sample", type=int, default=3,
                        help="how many legs to sample from the trip's unique legs")
    parser.add_argument("--all", action="store_true",
                        help="run every unique leg in the trip (slow!)")
    parser.add_argument("--leg", action="append", default=[],
                        help="ad-hoc leg as ORIG:DEST:YYYY-MM-DD (repeatable)")
    parser.add_argument("--mode", default="fast",
                        help="LetsFG mode: 'fast' (~25 connectors) or '' for all (~100)")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    if args.leg:
        legs = [_parse_leg_spec(s) for s in args.leg]
    elif args.trip:
        with open(args.config) as f:
            all_cfg = yaml.safe_load(f)
        if args.trip not in all_cfg["trips"]:
            print(f"Unknown trip '{args.trip}'. "
                  f"Available: {list(all_cfg['trips'].keys())}", file=sys.stderr)
            return 1
        trip_cfg = all_cfg["trips"][args.trip]
        combos = enumerate_routes(trip_cfg)
        unique = deduplicate_legs(combos)
        print(f"Trip '{args.trip}': {len(combos)} combos → {len(unique)} unique legs")
        legs = unique if args.all else _pick_sample(unique, args.sample)
    else:
        parser.error("pass --trip TRIP or one or more --leg specs")

    # Build a minimal config dict for the fetcher
    letsfg_cfg = {"concurrency": args.concurrency, "timeout_sec": args.timeout}
    if args.mode:
        letsfg_cfg["mode"] = args.mode
    else:
        letsfg_cfg["mode"] = None  # full mode = all ~100 connectors
    cfg = {"fetchers": {"letsfg": letsfg_cfg}}

    asyncio.run(_run(legs, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
