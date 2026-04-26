"""Flight digest orchestrator — entry point for daily runs and manual testing.

Runs one or more trips defined in config/trips.yaml. If --trip is given,
only that trip runs; otherwise every trip with enabled != false runs and the
digests are combined into a single email.

See docs/ADDING_A_TRIP.md for how to add new trips.
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from typing import Dict, List, Optional

import aiohttp
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Expand LetsFG's fast-mode connector set with full-service carriers that
# require a browser scraper (Playwright + headed Chrome). When the runner has
# Chrome and LETSFG_BROWSERS=1 (set in the GitHub Actions workflow), these run
# and surface fares Kiwi often misses — especially Copa via PTY, Avianca via
# BOG, LATAM via SCL/GRU, and the US legacy carriers' NDC inventory. On
# environments without Chrome (Render free tier) letsfg's engine silently
# filters them out, so adding them here is safe everywhere.
from letsfg.connectors.engine import (
    _BROWSER_SOURCES,
    _FAST_MODE_SOURCES as _LFG_FAST,
    _TEMPORARILY_DISABLED,
)
_KEEP_BROWSER_CONNECTORS = {
    "copa_direct",
    "avianca_direct",
    "latam_direct",
    "american_direct",
    "united_direct",
    "delta_direct",
}
# API-only full-service carriers — no browser required, not subject to
# anti-bot blocking the same way browser scrapers are. Add to fast mode so
# they run alongside Kiwi/Travelstart and contribute their own NDC fares
# (which sometimes differ from what aggregators see via GDS).
_API_ONLY_FULL_SERVICE = {
    "aircanada_direct",
    "airfrance_direct",
    "klm_direct",
    "britishairways_direct",
    "lufthansa_direct",
    "iberia_direct",
    "tap_direct",
    "austrian_direct",
    "brusselsairlines_direct",
}
_LFG_FAST.update(_KEEP_BROWSER_CONNECTORS)
_LFG_FAST.update(_API_ONLY_FULL_SERVICE)

# Restrict the browser-scraper pool to the curated set above. Without this,
# fast mode's other ~10 browser-based OTAs (Despegar, eSky, IXIGO, etc.)
# also queue for browser slots, blowing each leg out from ~10s to several
# minutes. Disabling them here means only our 6 target carriers consume
# browser time. No effect when LETSFG_BROWSERS=0 (browsers off entirely).
_TEMPORARILY_DISABLED.update(_BROWSER_SOURCES - _KEEP_BROWSER_CONNECTORS)


# Workaround for a bug in letsfg's Copa connector (CopaConnectorClient).
# Its _search_ow tries the fast Sputnik HTTP API first, then date-filters
# the results, then unconditionally reads sputnik_offers[0].currency — which
# crashes with IndexError when the date filter empties the list (Sputnik
# returns offers for some dates but not the requested one). The crash was
# observed in CI: "CM Sputnik GRU→NVT: 10 offers" → "copa_direct crashed:
# IndexError: list index out of range".
# Fix: pre-filter Sputnik results inside _try_sputnik. If nothing matches
# the requested date, return [] so the upstream `if sputnik_offers:` check
# correctly falls through to the slow Chrome path (which handles empty
# correctly via _empty()). Reported upstream; this can be removed once
# letsfg ships a fix.
def _patch_copa_connector():
    from datetime import datetime
    from letsfg.connectors import copa as _copa_mod

    _orig_try_sputnik = _copa_mod.CopaConnectorClient._try_sputnik

    async def _patched_try_sputnik(self, req):
        offers = await _orig_try_sputnik(self, req)
        if not offers:
            return offers
        target_date = (
            req.date_from.date() if isinstance(req.date_from, datetime)
            else req.date_from
        )
        return [
            o for o in offers
            if o.outbound and o.outbound.segments
            and o.outbound.segments[0].departure.date() == target_date
        ]

    _copa_mod.CopaConnectorClient._try_sputnik = _patched_try_sputnik


_patch_copa_connector()


# Workaround for letsfg's Etraveli/Gotogate connector — its _do_search
# calls get_httpx_proxy_url() but never imports it from .browser, so every
# attempt fails with NameError ("name 'get_httpx_proxy_url' is not defined").
# Workflow logs show this happening on every leg. Inject the function into
# the etraveli module namespace at import time. Reported upstream.
def _patch_etraveli_connector():
    from letsfg.connectors import etraveli as _etr_mod
    from letsfg.connectors.browser import get_httpx_proxy_url
    _etr_mod.get_httpx_proxy_url = get_httpx_proxy_url


_patch_etraveli_connector()

from engine.constraints import (
    apply_constraints,
    prune_impossible_after_fetch,
    prune_short_gru_connections,
)
from engine.routes import Combo, deduplicate_legs, enumerate_routes
from engine.scorer import all_legs_found, score_combo
from fetchers.amadeus import merge_cash
from fetchers.common import empty_leg_result
from fetchers.letsfg import fetch_cash_offers
# seats.aero removed from the daily cron — its API requires a paid Partner
# subscription ($30+/mo) and the user's not paying for it. The Manual Award
# Check section of the digest still gives per-route deep-links to Aeroplan,
# LifeMiles, United, Flying Blue, Smiles, LATAM Pass, etc. Re-enable by
# importing fetch_award_fares from fetchers.seats_aero and adding back the
# call below.
# Smiles (GOL) award fetcher also removed — it's anti-bot blocked from cloud
# IPs (HTTP 406 on every leg from GitHub Actions). The Manual Award Check
# section's Smiles deep-link covers the same need.
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


async def build_trip_digest(trip_name: str, config: dict,
                            prev_day_data: Optional[dict] = None) -> dict:
    """Fetch + score a single trip, return text/html digest + scored combos.

    Does not send email or write to Sheets — those are orchestrated by the
    caller so multi-trip runs produce a single combined email.
    """
    log.info("Running flight search for: %s", trip_name)

    # When browsers are enabled (daily cron on GH Actions), real Chrome scrapes
    # take 60-120s per leg, so we need a longer per-leg timeout and more
    # browser slots than the API-only defaults. Web UI on Render keeps
    # LETSFG_BROWSERS=0 and these overrides don't apply.
    if os.environ.get("LETSFG_BROWSERS", "").strip() == "1":
        config = dict(config)  # don't mutate caller's dict
        fetchers_cfg = dict(config.get("fetchers") or {})
        letsfg_cfg = dict(fetchers_cfg.get("letsfg") or {})
        letsfg_cfg.setdefault("max_browsers", 4)
        letsfg_cfg.setdefault("timeout_sec", 120)
        fetchers_cfg["letsfg"] = letsfg_cfg
        config["fetchers"] = fetchers_cfg

    combos = enumerate_routes(config)
    log.info("[%s] Generated %d route combinations", trip_name, len(combos))

    combos = apply_constraints(combos, config.get("constraints", {}))
    log.info("[%s] %d combos after constraint filtering", trip_name, len(combos))

    unique_legs = deduplicate_legs(combos)
    log.info("[%s] Fetching fares for %d unique legs", trip_name, len(unique_legs))

    async with aiohttp.ClientSession() as session:
        offers_by_leg = await fetch_cash_offers(unique_legs, config, session=session)
        # Award data slots kept for back-compat with _assemble_legs (which
        # merges intl + domestic awards). Empty dicts = no automated award
        # alerts; the Manual Award Check section in the email still
        # generates per-route booking-search URLs.
        award_data = {}
        domestic_award_data = {}

    # Cheapest-per-leg view feeds the scorer (back-compat: same data shape as
    # the old fetch_cash_fares). Full per-leg lists go to the digest renderer
    # so it can show alternative options per route.
    letsfg_data = {}
    for leg in unique_legs:
        offers = offers_by_leg.get(leg.key) or []
        if offers:
            letsfg_data[leg.key] = dict(offers[0])  # already sorted cheapest-first
        else:
            row = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
            row["source"] = "letsfg"
            row["error"] = "no offers"
            letsfg_data[leg.key] = row

    _log_fetcher_summary(f"{trip_name}:letsfg", letsfg_data)

    cash_data = merge_cash(letsfg_data)
    priced = sum(1 for v in cash_data.values() if v and v.get("price_usd") is not None)
    log.info("[%s] Merged cash coverage: %d/%d legs priced", trip_name, priced, len(cash_data))

    scored = []
    skipped = 0
    for combo in combos:
        legs_data = _assemble_legs(combo, cash_data, award_data, domestic_award_data)
        if not all_legs_found(legs_data):
            skipped += 1
            continue
        scored.append(score_combo(combo, legs_data, config))

    log.info("[%s] Scored %d combos (%d skipped: missing leg fares)",
             trip_name, len(scored), skipped)

    constraints_cfg = config.get("constraints", {})
    max_travel = float(constraints_cfg.get("max_total_travel_hours", 36))
    min_gru_conn = float(constraints_cfg.get("gru_min_connection_hours", 3))

    before_travel = len(scored)
    scored = prune_impossible_after_fetch(scored, max_travel)
    before_conn = len(scored)
    scored = prune_short_gru_connections(scored, min_gru_conn)
    scored.sort(key=lambda x: x.score)
    log.info(
        "[%s] Post-fetch filtering: %d→%d (travel-time), %d→%d (GRU connection)",
        trip_name, before_travel, before_conn, before_conn, len(scored),
    )

    text = format_digest(scored, date.today(), config,
                         prev_day_data=prev_day_data,
                         per_leg_offers=offers_by_leg)
    html = format_digest_html(scored, date.today(), config,
                              prev_day_data=prev_day_data,
                              per_leg_offers=offers_by_leg)

    return {
        "name": trip_name,
        "display_name": config.get("name", trip_name),
        "text": text,
        "html": html,
        "scored": scored,
        "config": config,
        "combo_count": len(scored),
    }


def _combine_digests(digests: List[dict]) -> tuple:
    """Stitch per-trip text + HTML digests into one email body.

    For one trip, returns that digest unchanged. For multiple trips, prepends
    a short trip-count summary and separates each trip's block visually.
    """
    if len(digests) == 1:
        return digests[0]["text"], digests[0]["html"]

    # Text: horizontal rule + display name + body, repeated per trip
    divider = "\n\n" + ("=" * 60) + "\n\n"
    text_blocks = [
        f"### {d['display_name']} ({d['combo_count']} ranked combos) ###\n\n{d['text']}"
        for d in digests
    ]
    header = (f"Flight Digest — {date.today().isoformat()} · "
              f"{len(digests)} trips\n\n")
    text = header + divider.join(text_blocks)

    # HTML: each digest is a standalone <div>, stack them with a visible divider
    html_header = (
        f'<div style="max-width:720px;margin:20px auto;font-family:sans-serif;'
        f'color:#1f2328;padding:0 16px;">'
        f'<h1 style="font-size:22px;margin:0 0 12px 0;">✈ Flight Digest — '
        f'{date.today().strftime("%a %b %d, %Y")} · {len(digests)} trips</h1></div>'
    )
    html_blocks = [
        f'<div style="max-width:720px;margin:0 auto 24px;padding:0 16px;">'
        f'<h2 style="font-size:18px;color:#0969da;margin:24px 0 8px;'
        f'border-top:2px solid #d0d7de;padding-top:16px;">'
        f'▸ {d["display_name"]}</h2></div>{d["html"]}'
        for d in digests
    ]
    html = html_header + "".join(html_blocks)
    return text, html


def _select_trips(all_cfg: dict, specific: Optional[str]) -> List[tuple]:
    """Decide which trips to run. Returns [(name, cfg), ...].

    If `specific` is given, only that trip (regardless of enabled flag).
    Otherwise every trip where enabled != false. `enabled` defaults to True
    when the key is absent.
    """
    trips = all_cfg.get("trips") or {}
    if specific:
        if specific not in trips:
            raise KeyError(
                f"Unknown trip '{specific}'. Available: {list(trips.keys())}"
            )
        return [(specific, trips[specific])]
    return [(k, v) for k, v in trips.items() if v.get("enabled", True)]


async def run_all(trips: List[tuple], dry_run: bool) -> int:
    """Fetch every selected trip, combine their digests, send one email."""
    prev_day = load_previous_day()  # shared across trips (combo IDs don't collide)
    digests = []
    for name, cfg in trips:
        digests.append(await build_trip_digest(name, cfg, prev_day_data=prev_day))

    text, html = _combine_digests(digests)
    subject = (
        f"Flight Digest — {date.today().isoformat()}"
        if len(digests) == 1
        else f"Flight Digest — {len(digests)} trips · {date.today().isoformat()}"
    )

    if dry_run:
        print(text)
    else:
        # Recipient: env var wins; per-trip notify.email is a fallback using
        # the first digest's config (assumes same user gets all trips).
        recipient = (
            os.environ.get("RECIPIENT_EMAIL")
            or (digests[0]["config"].get("notify") or {}).get("email")
            or ""
        )
        sent = send_email(text, recipient, subject=subject, html=html)
        if not sent:
            print("\n[email not sent — falling back to stdout]\n")
            print(text)

    # Write price-history snapshot per trip (Google Sheets + local JSON).
    for d in digests:
        append_to_sheets(d["scored"], d["config"])

    total = sum(d["combo_count"] for d in digests)
    log.info("Done. %d trip(s), %d combos total ranked.", len(digests), total)
    return 0


def _load_all_trips(config_path: str) -> dict:
    """Load trips from the YAML file, then merge in any private trips from the
    TRIPS_CONFIG env var (a YAML string, typically a GitHub Secret).

    Trips defined in TRIPS_CONFIG override file-level trips with the same key.
    The file is intended to hold non-sensitive examples (sample/smoke trips);
    the secret holds the user's actual personal trip data so the repo can
    safely be public.
    """
    with open(config_path) as f:
        all_cfg = yaml.safe_load(f) or {}
    all_cfg.setdefault("trips", {})

    secret_yaml = (os.environ.get("TRIPS_CONFIG") or "").strip()
    if not secret_yaml:
        return all_cfg

    try:
        secret_cfg = yaml.safe_load(secret_yaml) or {}
    except yaml.YAMLError as e:
        log.error("TRIPS_CONFIG secret isn't valid YAML — ignoring it. "
                  "Parser error: %s", e)
        return all_cfg

    secret_trips = (secret_cfg or {}).get("trips") or {}
    if secret_trips:
        log.info("TRIPS_CONFIG secret merged %d trip(s): %s",
                 len(secret_trips), list(secret_trips.keys()))
        all_cfg["trips"].update(secret_trips)
    return all_cfg


def main():
    parser = argparse.ArgumentParser(description="Flight digest runner")
    parser.add_argument(
        "--trip",
        help="Run just one trip (key in config/trips.yaml or TRIPS_CONFIG "
        "secret). Omit to run every enabled trip.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print digest instead of sending email")
    parser.add_argument("--config", default="config/trips.yaml")
    args = parser.parse_args()

    all_cfg = _load_all_trips(args.config)

    try:
        trips = _select_trips(all_cfg, args.trip)
    except KeyError as e:
        log.error(str(e))
        sys.exit(1)

    if not trips:
        log.error("No enabled trips to run. Add 'enabled: true' to at least "
                  "one trip in %s or in the TRIPS_CONFIG secret, or pass "
                  "--trip explicitly.", args.config)
        sys.exit(1)

    log.info("Will run %d trip(s): %s", len(trips), [t[0] for t in trips])
    exit_code = asyncio.run(run_all(trips, dry_run=args.dry_run))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
