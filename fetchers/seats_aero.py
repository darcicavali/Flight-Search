"""Seats.aero Partner API — international award space.

Docs: https://seats.aero/api
Auth: Partner-Authorization header with API key.
"""

import logging
from typing import Dict, List

import aiohttp

from engine.routes import Leg
from fetchers.common import empty_leg_result, env, gather_limited, get_rates, to_usd

log = logging.getLogger(__name__)

SEATS_AERO_URL = "https://seats.aero/partnerapi/search"

# Map Seats.aero source → our CPP key in trips.yaml.
SOURCE_TO_PROGRAM = {
    "aeroplan": "aeroplan",
    "lifemiles": "lifemiles",
    "united": "united_mp",
    "unitedmileageplus": "united_mp",
    "american": "american_aa",
    "aeromexico": "aeromexico",
    "virginatlantic": "virgin_atlantic",
    "flyingblue": "flying_blue",
    "airfrance": "flying_blue",
    "klm": "flying_blue",
    "delta": "skymiles",
    "alaska": "alaska",
    "etihad": "etihad",
    "qantas": "qantas",
    "emirates": "skywards",
    "velocity": "velocity",
    "jetblue": "trueblue",
    "turkish": "miles_smiles",
    "avianca": "lifemiles",
}


async def _fetch_one(
    session: aiohttp.ClientSession, leg: Leg, api_key: str, rates: dict
) -> dict:
    result = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
    result["source"] = "seats_aero"

    if not api_key:
        result["error"] = "SEATS_AERO_API_KEY not set"
        return result

    params = {
        "origin_airport": leg.origin,
        "destination_airport": leg.destination,
        "start_date": leg.date.isoformat(),
        "end_date": leg.date.isoformat(),
        "cabin": "economy",
        "take": 50,
    }
    headers = {
        "Partner-Authorization": api_key,
        "accept": "application/json",
    }

    try:
        async with session.get(
            SEATS_AERO_URL,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                result["error"] = f"seats_aero http {r.status}"
                return result
            body = await r.json()
    except Exception as e:
        result["error"] = f"seats_aero exception: {e}"
        return result

    items = body.get("data") or []
    awards: Dict[str, dict] = {}
    for item in items:
        if not item.get("YAvailable"):
            continue
        source = (item.get("Source") or "").lower().replace(" ", "").replace("_", "")
        program = SOURCE_TO_PROGRAM.get(source, source)

        points = item.get("YMileageCost") or item.get("YMileageCostRaw")
        try:
            points = int(points)
        except (TypeError, ValueError):
            continue

        fee_amount = item.get("YTotalTaxes") or 0
        try:
            fee_amount = float(fee_amount) / 100.0  # Seats.aero returns cents in some fields
        except (TypeError, ValueError):
            fee_amount = 0.0
        fee_currency = item.get("TaxesCurrency") or "USD"
        fees_usd = round(to_usd(fee_amount, fee_currency, rates), 2)

        entry = {
            "points": points,
            "fees_usd": fees_usd,
            "cabin": "economy",
            "availability": "available",
            "source_raw": item.get("Source"),
        }
        existing = awards.get(program)
        if not existing or entry["points"] < existing["points"]:
            awards[program] = entry

    result["awards"] = awards
    if not awards:
        result["error"] = "no award space"
    return result


async def fetch_award_fares(
    legs: List[Leg], config: dict, session: aiohttp.ClientSession = None
) -> Dict[str, dict]:
    """Fetch award availability for every unique leg."""
    api_key = env("SEATS_AERO_API_KEY")
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        rates = await get_rates(session)
        coros = [_fetch_one(session, leg, api_key, rates) for leg in legs]
        results = await gather_limited(3, coros)
    finally:
        if own_session:
            await session.close()

    out: Dict[str, dict] = {}
    for leg, res in zip(legs, results):
        if isinstance(res, Exception):
            log.warning("seats_aero leg %s raised: %s", leg.key, res)
            data = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
            data["source"] = "seats_aero"
            data["error"] = str(res)
            out[leg.key] = data
        else:
            out[leg.key] = res
    return out
