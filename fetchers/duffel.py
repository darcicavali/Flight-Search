"""Duffel cash fare fetcher — LCC + legacy carrier coverage.

Docs: https://duffel.com/docs/api
Auth: Bearer token + Duffel-Version header.

Duffel is a two-step API (create offer request → get offers) but setting
`return_offers=true` collapses it into a single call that returns the top offers
inline.

Rate limiting: Duffel test env caps at a few requests/sec. We serialize to 2
concurrent and retry HTTP 429 responses with exponential backoff.
"""

import asyncio
import logging
from typing import Dict, List, Optional

import aiohttp

from engine.routes import Leg
from fetchers.common import empty_leg_result, env, gather_limited, get_rates, to_usd

log = logging.getLogger(__name__)

DUFFEL_API = "https://api.duffel.com"
DUFFEL_OFFER_REQUESTS = f"{DUFFEL_API}/air/offer_requests"
DUFFEL_VERSION = "v2"

RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF_SECONDS = [2, 4, 8, 16]


def _duration_iso_to_hours(iso: Optional[str]) -> Optional[float]:
    # PT13H45M → 13.75
    if not iso or not iso.startswith("PT"):
        return None
    iso = iso[2:]
    hours = 0.0
    num = ""
    for ch in iso:
        if ch.isdigit() or ch == ".":
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


def _hours_str(hours: Optional[float]) -> Optional[str]:
    if hours is None:
        return None
    h = int(hours)
    m = int(round((hours - h) * 60))
    return f"{h}h{m:02d}m"


async def _fetch_one(
    session: aiohttp.ClientSession, leg: Leg, token: str, rates: dict
) -> dict:
    result = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
    result["source"] = "duffel"

    if not token:
        result["error"] = "DUFFEL_ACCESS_TOKEN not set"
        return result

    payload = {
        "data": {
            "slices": [
                {
                    "origin": leg.origin,
                    "destination": leg.destination,
                    "departure_date": leg.date.isoformat(),
                }
            ],
            "passengers": [{"type": "adult"}],
            "cabin_class": "economy",
        }
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Duffel-Version": DUFFEL_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    body = None
    last_error = None
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            async with session.post(
                f"{DUFFEL_OFFER_REQUESTS}?return_offers=true&sort=total_amount",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as r:
                if r.status == 429 and attempt < RATE_LIMIT_RETRIES:
                    retry_after = RATE_LIMIT_BACKOFF_SECONDS[attempt]
                    header_val = r.headers.get("Retry-After")
                    if header_val and header_val.isdigit():
                        retry_after = max(retry_after, int(header_val))
                    await asyncio.sleep(retry_after)
                    continue
                if r.status not in (200, 201):
                    body_text = await r.text()
                    last_error = f"duffel http {r.status}: {body_text[:200]}"
                    break
                body = await r.json()
                break
        except Exception as e:
            last_error = f"duffel exception: {e}"
            break

    if body is None:
        result["error"] = last_error or "duffel failed after retries"
        return result

    offers = ((body.get("data") or {}).get("offers")) or []
    if not offers:
        result["error"] = "no offers"
        return result

    best = offers[0]
    total_amount = best.get("total_amount")
    currency = best.get("total_currency", "USD")
    try:
        result["price_usd"] = round(to_usd(float(total_amount), currency, rates), 2)
    except (TypeError, ValueError):
        result["error"] = "could not parse price"
        return result

    owner = best.get("owner") or {}
    result["airline"] = owner.get("iata_code") or owner.get("name")

    slices = best.get("slices") or []
    if slices:
        first_slice = slices[0]
        result["duration_hours"] = _duration_iso_to_hours(first_slice.get("duration"))
        result["duration_str"] = _hours_str(result["duration_hours"])
        segments = first_slice.get("segments") or []
        result["stops"] = max(len(segments) - 1, 0)

    # Duffel does not expose a deep link; booking happens via the Orders API.
    # We surface the offer id so the user can look it up if needed.
    offer_id = best.get("id")
    if offer_id:
        result["booking_url"] = f"https://duffel.com/offers/{offer_id}"
    return result


async def fetch_cash_fares(
    legs: List[Leg], config: dict, session: aiohttp.ClientSession = None
) -> Dict[str, dict]:
    """Fetch cash fares for every unique leg via Duffel. Returns {leg.key: leg_data}."""
    token = env("DUFFEL_ACCESS_TOKEN")
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        rates = await get_rates(session)
        coros = [_fetch_one(session, leg, token, rates) for leg in legs]
        # Lower concurrency (2) — stay under Duffel test-env rate limits.
        results = await gather_limited(2, coros)
    finally:
        if own_session:
            await session.close()

    out: Dict[str, dict] = {}
    for leg, res in zip(legs, results):
        if isinstance(res, Exception):
            log.warning("duffel leg %s raised: %s", leg.key, res)
            data = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
            data["source"] = "duffel"
            data["error"] = str(res)
            out[leg.key] = data
        else:
            out[leg.key] = res
    return out
