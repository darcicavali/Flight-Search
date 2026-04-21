"""LetsFG cash-fare fetcher — primary price source.

LetsFG (https://pypi.org/project/letsfg/) runs ~100 airline-site scrapers
locally via Playwright + httpx. `search()` is completely free; only `unlock()`
and `book()` require an API key. We just want prices and booking URLs, so we
use `search()` only.

Trade-off vs Duffel: real bookable prices from real airline sites (no synthetic
test data), but each call spins up browsers and takes ~10–20s. We wrap the
synchronous library in `run_in_executor` and cap concurrency.
"""

import asyncio
import logging
from datetime import date as _date
from datetime import datetime
from typing import Dict, List, Optional

import aiohttp

from engine.routes import Leg
from fetchers.common import empty_leg_result, get_rates, to_usd

log = logging.getLogger(__name__)

# Playwright browser spinup is heavy — keep this conservative. Each search
# already parallelises ~100 site scrapers internally via `max_browsers`.
DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_BROWSERS = 3
DEFAULT_LIMIT = 10
DEFAULT_TIMEOUT_SECONDS = 90


def _hours_str(hours: Optional[float]) -> Optional[str]:
    if hours is None:
        return None
    h = int(hours)
    m = int(round((hours - h) * 60))
    return f"{h}h{m:02d}m"


def _hhmm(iso_dt: Optional[str], compare_date: Optional[str] = None) -> Optional[str]:
    """Pull HH:MM from an ISO datetime. Appends '+N' if it falls on a later day."""
    if not iso_dt:
        return None
    date_part, _, rest = iso_dt.partition("T")
    if not rest:
        return None
    hhmm = rest[:5]
    if compare_date and date_part and date_part != compare_date:
        try:
            d0 = _date.fromisoformat(compare_date)
            d1 = _date.fromisoformat(date_part)
            delta = (d1 - d0).days
            if delta > 0:
                return f"{hhmm}+{delta}"
        except ValueError:
            pass
    return hhmm


def _layover_hours(arrive_iso: str, depart_iso: str) -> Optional[float]:
    try:
        a = datetime.fromisoformat(arrive_iso)
        d = datetime.fromisoformat(depart_iso)
        return round((d - a).total_seconds() / 3600, 2)
    except (TypeError, ValueError):
        return None


def _offer_to_segments(route, leg_date_iso: str) -> dict:
    """Translate a LetsFG FlightRoute into our segments/layovers shape."""
    out_segments = []
    for s in route.segments:
        out_segments.append({
            "carrier": s.airline,
            "marketing_carrier": s.airline,
            "operating_carrier": None,
            "flight_no": s.flight_no,
            "origin": s.origin,
            "destination": s.destination,
            "depart": s.departure,
            "arrive": s.arrival,
        })

    layovers: List[str] = []
    raw = route.segments
    for i in range(len(raw) - 1):
        h = _layover_hours(raw[i].arrival, raw[i + 1].departure)
        if h is not None:
            layovers.append(f"{raw[i + 1].origin} {_hours_str(h)}")

    depart_time = _hhmm(raw[0].departure) if raw else None
    arrive_time = _hhmm(raw[-1].arrival, compare_date=leg_date_iso) if raw else None

    return {
        "segments": out_segments,
        "layovers": layovers,
        "depart_time": depart_time,
        "arrive_time": arrive_time,
    }


def _offer_to_leg_result(offer, leg: Leg, rates: dict) -> dict:
    """Translate a single FlightOffer into the empty_leg_result shape."""
    result = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
    result["source"] = "letsfg"

    try:
        result["price_usd"] = round(to_usd(float(offer.price), offer.currency, rates), 2)
    except (TypeError, ValueError):
        result["error"] = "could not parse price"
        return result

    result["airline"] = offer.owner_airline or (offer.airlines[0] if offer.airlines else None)

    route = offer.outbound
    if route is None or not route.segments:
        result["error"] = "no outbound segments"
        return result

    result["duration_hours"] = round((route.total_duration_seconds or 0) / 3600, 2)
    result["duration_str"] = _hours_str(result["duration_hours"])
    result["stops"] = max(len(route.segments) - 1, 0)

    parsed = _offer_to_segments(route, leg.date.isoformat())
    result["segments"] = parsed["segments"]
    result["layovers"] = parsed["layovers"]
    result["depart_time"] = parsed["depart_time"]
    result["arrive_time"] = parsed["arrive_time"]

    result["booking_url"] = offer.booking_url
    return result


def _search_sync(origin: str, destination: str, iso_date: str,
                 currency: str, limit: int, max_browsers: int):
    """Blocking LetsFG call — must run in a worker thread."""
    from letsfg import LetsFG  # local import: heavy module, optional dep
    return LetsFG().search(
        origin, destination, iso_date,
        currency=currency, limit=limit, max_browsers=max_browsers,
    )


async def _fetch_one(loop, sem: asyncio.Semaphore, leg: Leg, rates: dict,
                     currency: str, limit: int, max_browsers: int,
                     timeout: float) -> dict:
    async with sem:
        try:
            search = await asyncio.wait_for(
                loop.run_in_executor(
                    None, _search_sync,
                    leg.origin, leg.destination, leg.date.isoformat(),
                    currency, limit, max_browsers,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            res = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
            res["source"] = "letsfg"
            res["error"] = f"timeout after {timeout}s"
            return res
        except Exception as e:
            log.warning("letsfg leg %s raised: %s", leg.key, e)
            res = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
            res["source"] = "letsfg"
            res["error"] = f"letsfg exception: {e}"
            return res

    offers = getattr(search, "offers", None) or []
    if not offers:
        res = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
        res["source"] = "letsfg"
        res["error"] = "no offers"
        return res

    return _offer_to_leg_result(offers[0], leg, rates)


async def fetch_cash_fares(
    legs: List[Leg], config: dict, session: aiohttp.ClientSession = None
) -> Dict[str, dict]:
    """Fetch cash fares for every unique leg via LetsFG. Returns {leg.key: leg_data}.

    Honours these optional config keys under `fetchers.letsfg`:
      currency      (default "USD")
      limit         (default 10)
      max_browsers  (default 3)
      concurrency   (default 2)
      timeout_sec   (default 90)
    """
    cfg = (config.get("fetchers") or {}).get("letsfg") or {}
    currency = cfg.get("currency", "USD")
    limit = int(cfg.get("limit", DEFAULT_LIMIT))
    max_browsers = int(cfg.get("max_browsers", DEFAULT_MAX_BROWSERS))
    concurrency = int(cfg.get("concurrency", DEFAULT_CONCURRENCY))
    timeout = float(cfg.get("timeout_sec", DEFAULT_TIMEOUT_SECONDS))

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        rates = await get_rates(session)
    finally:
        if own_session:
            await session.close()

    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_event_loop()
    coros = [
        _fetch_one(loop, sem, leg, rates, currency, limit, max_browsers, timeout)
        for leg in legs
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    out: Dict[str, dict] = {}
    for leg, res in zip(legs, results):
        if isinstance(res, Exception):
            data = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
            data["source"] = "letsfg"
            data["error"] = str(res)
            out[leg.key] = data
        else:
            out[leg.key] = res
    return out
