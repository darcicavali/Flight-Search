"""Kiwi Tequila cash fare fetcher.

API docs: https://tequila.kiwi.com/portal/docs/tequila_api/search_api
Auth: apikey header.
"""

import logging
from typing import Dict, List

import aiohttp

from engine.routes import Leg
from fetchers.common import empty_leg_result, env, gather_limited, get_rates, to_usd

log = logging.getLogger(__name__)

KIWI_SEARCH_URL = "https://api.tequila.kiwi.com/v2/search"


def _format_kiwi_date(iso: str) -> str:
    # Kiwi expects DD/MM/YYYY
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def _duration_str(seconds: int) -> str:
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}m"


async def _fetch_one(
    session: aiohttp.ClientSession, leg: Leg, api_key: str, rates: dict
) -> dict:
    result = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
    result["source"] = "kiwi"

    if not api_key:
        result["error"] = "KIWI_API_KEY not set"
        return result

    d = _format_kiwi_date(leg.date.isoformat())
    params = {
        "fly_from": leg.origin,
        "fly_to": leg.destination,
        "date_from": d,
        "date_to": d,
        "adults": 1,
        "curr": "USD",
        "limit": 5,
        "sort": "price",
        "max_stopovers": 2,
    }
    headers = {"apikey": api_key, "accept": "application/json"}

    try:
        async with session.get(
            KIWI_SEARCH_URL,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                result["error"] = f"kiwi http {r.status}"
                return result
            body = await r.json()
    except Exception as e:
        result["error"] = f"kiwi exception: {e}"
        return result

    offers = body.get("data") or []
    if not offers:
        result["error"] = "no offers"
        return result

    best = offers[0]
    price = best.get("price", 0)
    currency = body.get("currency", "USD")
    result["price_usd"] = round(to_usd(price, currency, rates), 2)
    airlines = best.get("airlines") or []
    result["airline"] = "/".join(airlines) if airlines else None
    duration_total = (best.get("duration") or {}).get("total", 0)
    result["duration_hours"] = round(duration_total / 3600, 2) if duration_total else None
    result["duration_str"] = _duration_str(duration_total) if duration_total else None
    route = best.get("route") or []
    result["stops"] = max(len(route) - 1, 0)
    # Kiwi does give a real deep_link that actually books — prefer it over Kayak.
    result["booking_url"] = best.get("deep_link") or (
        f"https://www.kayak.com/flights/{leg.origin}-{leg.destination}/"
        f"{leg.date.isoformat()}?sort=price_a"
    )
    # Extract segment/time info from Kiwi's route list.
    segs = []
    for seg in route:
        segs.append({
            "carrier": seg.get("airline"),
            "flight_no": seg.get("flight_no"),
            "origin": seg.get("flyFrom"),
            "destination": seg.get("flyTo"),
            "depart": seg.get("local_departure"),
            "arrive": seg.get("local_arrival"),
        })
    result["segments"] = segs
    if segs:
        result["depart_time"] = (segs[0].get("depart") or "")[11:16] or None
        result["arrive_time"] = (segs[-1].get("arrive") or "")[11:16] or None
    return result


async def fetch_cash_fares(
    legs: List[Leg], config: dict, session: aiohttp.ClientSession = None
) -> Dict[str, dict]:
    """Fetch cash fares for every unique leg via Kiwi. Returns {leg.key: leg_data}."""
    api_key = env("KIWI_API_KEY")
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        rates = await get_rates(session)
        coros = [_fetch_one(session, leg, api_key, rates) for leg in legs]
        results = await gather_limited(5, coros)
    finally:
        if own_session:
            await session.close()

    out: Dict[str, dict] = {}
    for leg, res in zip(legs, results):
        if isinstance(res, Exception):
            log.warning("kiwi leg %s raised: %s", leg.key, res)
            data = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
            data["source"] = "kiwi"
            data["error"] = str(res)
            out[leg.key] = data
        else:
            out[leg.key] = res
    return out
