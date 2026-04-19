"""Smiles (GOL) Brazilian domestic award availability — anonymous requests only.

No logged-in session tokens. If Smiles changes the endpoint shape, the fetcher
returns empty awards with an `error` marker rather than crashing the pipeline.
"""

import logging
from typing import Dict, List

import aiohttp

from engine.routes import Leg
from fetchers.common import empty_leg_result, env, gather_limited, get_rates, to_usd

log = logging.getLogger(__name__)

SMILES_URL = "https://api-air-flightsearch-prd.smiles.com.br/v1/airlines/search"
BR_DOMESTIC_AIRPORTS = {"NVT", "JOI", "CWB", "GRU", "CGH", "VCP", "SDU", "GIG",
                        "BSB", "CNF", "REC", "SSA", "FLN", "POA"}


def _is_br_domestic(leg: Leg) -> bool:
    return leg.origin in BR_DOMESTIC_AIRPORTS and leg.destination in BR_DOMESTIC_AIRPORTS


async def _fetch_one(
    session: aiohttp.ClientSession, leg: Leg, rates: dict
) -> dict:
    result = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
    result["source"] = "smiles"

    if not _is_br_domestic(leg):
        return result  # not applicable, no awards, no error

    params = {
        "originAirportCode": leg.origin,
        "destinationAirportCode": leg.destination,
        "departureDate": leg.date.isoformat(),
        "adults": 1,
        "children": 0,
        "infants": 0,
        "tripType": 2,  # one-way
        "cabin": "ECONOMIC",
        "forceCongener": "false",
    }
    # Public anonymous headers — inspected from browser network traffic.
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://www.smiles.com.br",
        "referer": "https://www.smiles.com.br/",
        "user-agent": "Mozilla/5.0 (compatible; FlightDigest/1.0)",
        "x-api-key": env("SMILES_X_API_KEY", "aJqPU7xNHl9qN3NVZnPaJ208aPo2Bh2x2vF9SNgI"),
    }

    try:
        async with session.get(
            SMILES_URL,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                result["error"] = f"smiles http {r.status} — check manually"
                return result
            body = await r.json()
    except Exception as e:
        result["error"] = f"smiles exception: {e} — check manually"
        return result

    flights = (body.get("requestedFlightSegmentList") or [{}])[0].get("flightList") or []
    best_points = None
    best_fees_usd = 0.0
    for f in flights:
        fare_list = f.get("fareList") or []
        for fare in fare_list:
            fare_type = (fare.get("type") or "").upper()
            if fare_type not in {"SMILES", "SMILES_CLUB"}:
                continue
            miles = fare.get("miles")
            try:
                miles = int(miles)
            except (TypeError, ValueError):
                continue
            money = fare.get("money") or 0
            airport_tax = fare.get("airportTax", {}).get("money") or 0
            total_brl = float(money) + float(airport_tax)
            fees_usd = round(to_usd(total_brl, "BRL", rates), 2)
            if best_points is None or miles < best_points:
                best_points = miles
                best_fees_usd = fees_usd

    if best_points is not None:
        result["awards"] = {
            "smiles": {
                "points": best_points,
                "fees_usd": best_fees_usd,
                "cabin": "economy",
                "availability": "available",
            }
        }
    else:
        result["error"] = "no smiles award space"
    return result


async def fetch_smiles_domestic(
    legs: List[Leg], config: dict, session: aiohttp.ClientSession = None
) -> Dict[str, dict]:
    """Fetch Smiles award availability for BR domestic legs only."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        rates = await get_rates(session)
        coros = [_fetch_one(session, leg, rates) for leg in legs]
        results = await gather_limited(2, coros)
    finally:
        if own_session:
            await session.close()

    out: Dict[str, dict] = {}
    for leg, res in zip(legs, results):
        if isinstance(res, Exception):
            log.warning("smiles leg %s raised: %s", leg.key, res)
            data = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
            data["source"] = "smiles"
            data["error"] = f"{res} — check manually"
            out[leg.key] = data
        else:
            out[leg.key] = res
    return out
