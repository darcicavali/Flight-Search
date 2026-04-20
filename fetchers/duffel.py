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

        raw_segments = first_slice.get("segments") or []
        result["stops"] = max(len(raw_segments) - 1, 0)

        parsed = _parse_segments(raw_segments, leg.date.isoformat())
        result["segments"] = parsed["segments"]
        result["layovers"] = parsed["layovers"]
        result["depart_time"] = parsed["depart_time"]
        result["arrive_time"] = parsed["arrive_time"]
        result["operated_by"] = parsed["operated_by"]

    # Booking: link to Kayak search pre-filled for this leg. Duffel offer IDs
    # aren't publicly browsable; Kayak gives the user a real path to book.
    result["booking_url"] = _kayak_url(leg.origin, leg.destination, leg.date.isoformat())
    return result


def _parse_segments(raw_segments: list, leg_date_iso: str) -> dict:
    """Extract per-segment info and compute layover durations between them."""
    out_segments = []
    for s in raw_segments:
        origin = (s.get("origin") or {}).get("iata_code")
        destination = (s.get("destination") or {}).get("iata_code")
        marketing = (s.get("marketing_carrier") or {}).get("iata_code")
        operating = (s.get("operating_carrier") or {}).get("iata_code")
        carrier = marketing or operating
        flight_no = s.get("marketing_carrier_flight_number") \
            or s.get("operating_carrier_flight_number")
        depart = s.get("departing_at")   # ISO datetime
        arrive = s.get("arriving_at")
        out_segments.append({
            "carrier": carrier,
            "marketing_carrier": marketing,
            "operating_carrier": operating,
            "flight_no": flight_no,
            "origin": origin,
            "destination": destination,
            "depart": depart,
            "arrive": arrive,
        })

    layovers = []
    for i in range(len(raw_segments) - 1):
        arr = raw_segments[i].get("arriving_at")
        dep = raw_segments[i + 1].get("departing_at")
        airport = (raw_segments[i + 1].get("origin") or {}).get("iata_code")
        if arr and dep and airport:
            h = _duration_between(arr, dep)
            if h is not None:
                layovers.append(f"{airport} {_hours_str(h)}")

    depart_time = None
    arrive_time = None
    if raw_segments:
        first_depart = raw_segments[0].get("departing_at")
        last_arrive = raw_segments[-1].get("arriving_at")
        depart_time = _extract_hhmm(first_depart)
        arrive_time = _extract_hhmm(last_arrive, compare_date=leg_date_iso)

    # Codeshare detection: operating carriers that differ from marketing.
    operated_by = sorted({
        s["operating_carrier"] for s in out_segments
        if s.get("operating_carrier")
        and s.get("marketing_carrier")
        and s["operating_carrier"] != s["marketing_carrier"]
    })

    return {
        "segments": out_segments,
        "layovers": layovers,
        "depart_time": depart_time,
        "arrive_time": arrive_time,
        "operated_by": "/".join(operated_by) if operated_by else None,
    }


def _extract_hhmm(iso_dt: Optional[str], compare_date: Optional[str] = None) -> Optional[str]:
    """Pull HH:MM from an ISO-8601 datetime string. Appends '+1' if date differs."""
    if not iso_dt:
        return None
    date_part, _, rest = iso_dt.partition("T")
    hhmm = rest[:5] if rest else None
    if not hhmm:
        return None
    if compare_date and date_part and date_part != compare_date:
        # Only add the +1/+2 suffix if the arrival is on a later day than the
        # leg's scheduled departure date.
        try:
            from datetime import date as _date
            d0 = _date.fromisoformat(compare_date)
            d1 = _date.fromisoformat(date_part)
            delta = (d1 - d0).days
            if delta > 0:
                return f"{hhmm}+{delta}"
        except Exception:
            pass
    return hhmm


def _duration_between(start_iso: str, end_iso: str) -> Optional[float]:
    from datetime import datetime
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return round((e - s).total_seconds() / 3600, 2)
    except Exception:
        return None


def _kayak_url(origin: str, destination: str, iso_date: str) -> str:
    return f"https://www.kayak.com/flights/{origin}-{destination}/{iso_date}?sort=price_a"


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
