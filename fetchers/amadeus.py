"""Amadeus Self-Service cash fare fetcher — gap filler for legacy carriers.

Docs: https://developers.amadeus.com/self-service/category/flights
Auth: OAuth2 client credentials -> bearer token.
"""

import logging
import time
from typing import Dict, List, Optional

import aiohttp

from engine.routes import Leg
from fetchers.common import empty_leg_result, env, gather_limited, get_rates, to_usd

log = logging.getLogger(__name__)

AMADEUS_BASE = "https://test.api.amadeus.com"
TOKEN_URL = f"{AMADEUS_BASE}/v1/security/oauth2/token"
OFFERS_URL = f"{AMADEUS_BASE}/v2/shopping/flight-offers"

_token_cache: Optional[dict] = None  # {"token": str, "expires_at": epoch}


async def _get_token(
    session: aiohttp.ClientSession, client_id: str, client_secret: str
) -> Optional[str]:
    global _token_cache
    now = time.time()
    if _token_cache and _token_cache["expires_at"] > now + 30:
        return _token_cache["token"]
    try:
        async with session.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                log.warning("amadeus token http %s", r.status)
                return None
            body = await r.json()
    except Exception as e:
        log.warning("amadeus token exception: %s", e)
        return None
    _token_cache = {
        "token": body["access_token"],
        "expires_at": now + body.get("expires_in", 1700),
    }
    return _token_cache["token"]


def _duration_iso_to_hours(iso: str) -> Optional[float]:
    # PT13H45M → 13.75
    if not iso or not iso.startswith("PT"):
        return None
    iso = iso[2:]
    hours = 0.0
    num = ""
    for ch in iso:
        if ch.isdigit():
            num += ch
        elif ch == "H":
            hours += float(num or 0)
            num = ""
        elif ch == "M":
            hours += float(num or 0) / 60
            num = ""
        else:
            num = ""
    return round(hours, 2)


async def _fetch_one(
    session: aiohttp.ClientSession, leg: Leg, token: Optional[str], rates: dict
) -> dict:
    result = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
    result["source"] = "amadeus"
    if not token:
        result["error"] = "amadeus no token"
        return result

    params = {
        "originLocationCode": leg.origin,
        "destinationLocationCode": leg.destination,
        "departureDate": leg.date.isoformat(),
        "adults": 1,
        "currencyCode": "USD",
        "max": 5,
        "nonStop": "false",
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with session.get(
            OFFERS_URL,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                result["error"] = f"amadeus http {r.status}"
                return result
            body = await r.json()
    except Exception as e:
        result["error"] = f"amadeus exception: {e}"
        return result

    offers = body.get("data") or []
    if not offers:
        result["error"] = "no offers"
        return result

    offer = offers[0]
    price = offer.get("price", {})
    currency = price.get("currency", "USD")
    result["price_usd"] = round(to_usd(float(price.get("total", 0)), currency, rates), 2)

    itineraries = offer.get("itineraries") or []
    if itineraries:
        it = itineraries[0]
        result["duration_hours"] = _duration_iso_to_hours(it.get("duration"))
        if result["duration_hours"]:
            h = int(result["duration_hours"])
            m = int(round((result["duration_hours"] - h) * 60))
            result["duration_str"] = f"{h}h{m:02d}m"
        segs = it.get("segments") or []
        result["stops"] = max(len(segs) - 1, 0)
        carriers = {s.get("carrierCode") for s in segs if s.get("carrierCode")}
        result["airline"] = "/".join(sorted(carriers)) if carriers else None
    return result


async def fetch_cash_fares(
    legs: List[Leg], config: dict, session: aiohttp.ClientSession = None
) -> Dict[str, dict]:
    """Fetch cash fares via Amadeus. Returns {leg.key: leg_data}."""
    client_id = env("AMADEUS_CLIENT_ID")
    client_secret = env("AMADEUS_CLIENT_SECRET")

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        rates = await get_rates(session)
        token = None
        if client_id and client_secret:
            token = await _get_token(session, client_id, client_secret)
        coros = [_fetch_one(session, leg, token, rates) for leg in legs]
        results = await gather_limited(3, coros)
    finally:
        if own_session:
            await session.close()

    out: Dict[str, dict] = {}
    for leg, res in zip(legs, results):
        if isinstance(res, Exception):
            log.warning("amadeus leg %s raised: %s", leg.key, res)
            data = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
            data["source"] = "amadeus"
            data["error"] = str(res)
            out[leg.key] = data
        else:
            out[leg.key] = res
    return out


def merge_cash(primary: Dict[str, dict], secondary: Dict[str, dict]) -> Dict[str, dict]:
    """Pick lower-price offer per leg between Kiwi and Amadeus."""
    out: Dict[str, dict] = {}
    keys = set(primary) | set(secondary)
    for k in keys:
        a = primary.get(k)
        b = secondary.get(k)
        if a and a.get("price_usd") and (not b or not b.get("price_usd") or a["price_usd"] <= b["price_usd"]):
            out[k] = a
        elif b and b.get("price_usd"):
            out[k] = b
        else:
            out[k] = a or b
    return out
