"""LetsFG cash-fare fetcher — primary price source.

Calls LetsFG's internal async API (`letsfg.local.search_local`) with
`mode='fast'` so we only hit ~25 OTA/airline-direct connectors instead of
the 100+ the library runs by default. Fast mode typically returns in
~5–10s per leg (vs 60s+ in full mode), and the browser-heavy connectors
that don't work in GitHub Actions runners are excluded.

search_local returns a dict (no pydantic models), so we consume it as-is
and avoid the sync-wrapper boilerplate.
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

DEFAULT_CONCURRENCY = 3
DEFAULT_MAX_BROWSERS = 2
DEFAULT_LIMIT = 10
DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_MODE = "fast"


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


def _route_to_segments(route: dict, leg_date_iso: str) -> dict:
    """Translate a LetsFG outbound-route dict into our segments/layovers shape."""
    raw = route.get("segments") or []
    out_segments = []
    for s in raw:
        out_segments.append({
            "carrier": s.get("airline"),
            "marketing_carrier": s.get("airline"),
            "operating_carrier": None,
            "flight_no": s.get("flight_no"),
            "origin": s.get("origin"),
            "destination": s.get("destination"),
            "depart": s.get("departure"),
            "arrive": s.get("arrival"),
        })

    layovers: List[str] = []
    for i in range(len(raw) - 1):
        h = _layover_hours(raw[i].get("arrival"), raw[i + 1].get("departure"))
        if h is not None:
            layovers.append(f"{raw[i + 1].get('origin')} {_hours_str(h)}")

    depart_time = _hhmm(raw[0].get("departure")) if raw else None
    arrive_time = _hhmm(raw[-1].get("arrival"), compare_date=leg_date_iso) if raw else None

    return {
        "segments": out_segments,
        "layovers": layovers,
        "depart_time": depart_time,
        "arrive_time": arrive_time,
    }


def _offer_to_leg_result(offer: dict, leg: Leg, rates: dict) -> dict:
    """Translate a single LetsFG offer dict into the empty_leg_result shape."""
    result = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
    result["source"] = "letsfg"

    route = offer.get("outbound")
    if not route or not (route.get("segments") or []):
        result["error"] = "no outbound segments"
        return result

    try:
        result["price_usd"] = round(
            to_usd(float(offer.get("price") or 0), offer.get("currency") or "USD", rates), 2
        )
    except (TypeError, ValueError):
        result["error"] = "could not parse price"
        return result

    airlines = offer.get("airlines") or []
    result["airline"] = offer.get("owner_airline") or (airlines[0] if airlines else None)

    segments = route.get("segments") or []
    result["duration_hours"] = round((route.get("total_duration_seconds") or 0) / 3600, 2)
    result["duration_str"] = _hours_str(result["duration_hours"])
    result["stops"] = max(len(segments) - 1, 0)

    parsed = _route_to_segments(route, leg.date.isoformat())
    result["segments"] = parsed["segments"]
    result["layovers"] = parsed["layovers"]
    result["depart_time"] = parsed["depart_time"]
    result["arrive_time"] = parsed["arrive_time"]

    result["booking_url"] = offer.get("booking_url")
    return result


async def _search_async(origin: str, destination: str, iso_date: str,
                        currency: str, limit: int, max_browsers: int,
                        mode: Optional[str]) -> dict:
    """Call LetsFG's async core. Returns a dict with 'offers' list."""
    from letsfg.local import search_local  # local import: heavy optional dep
    return await search_local(
        origin, destination, iso_date,
        currency=currency, limit=limit,
        max_browsers=max_browsers, mode=mode,
    )


async def _fetch_one(sem: asyncio.Semaphore, leg: Leg, rates: dict,
                     currency: str, limit: int, max_browsers: int,
                     mode: Optional[str], timeout: float) -> dict:
    async with sem:
        try:
            search = await asyncio.wait_for(
                _search_async(
                    leg.origin, leg.destination, leg.date.isoformat(),
                    currency, limit, max_browsers, mode,
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

    offers = (search or {}).get("offers") or []
    if not offers:
        res = empty_leg_result(leg.origin, leg.destination, leg.date.isoformat())
        res["source"] = "letsfg"
        res["error"] = "no offers"
        return res

    return _offer_to_leg_result(offers[0], leg, rates)


async def _fetch_offers_one(sem: asyncio.Semaphore, leg: Leg, rates: dict,
                            currency: str, limit: int, max_browsers: int,
                            mode: Optional[str], timeout: float) -> List[dict]:
    """Like _fetch_one but returns every priced offer (sorted cheapest-first)."""
    async with sem:
        try:
            search = await asyncio.wait_for(
                _search_async(
                    leg.origin, leg.destination, leg.date.isoformat(),
                    currency, limit, max_browsers, mode,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.info("letsfg leg %s timeout after %ss", leg.key, timeout)
            return []
        except Exception as e:
            log.warning("letsfg leg %s raised: %s", leg.key, e)
            return []

    offers = (search or {}).get("offers") or []
    rows: List[dict] = []
    for offer in offers:
        row = _offer_to_leg_result(offer, leg, rates)
        if row.get("price_usd") is not None:
            rows.append(row)
    rows.sort(key=lambda r: r.get("price_usd") or float("inf"))
    return rows


async def fetch_cash_fares(
    legs: List[Leg], config: dict, session: aiohttp.ClientSession = None
) -> Dict[str, dict]:
    """Fetch cash fares for every unique leg via LetsFG. Returns {leg.key: leg_data}.

    Optional config keys under `fetchers.letsfg`:
      mode          (default "fast" — ~25 connectors; None = all ~100)
      currency      (default "USD")
      limit         (default 10)
      max_browsers  (default 2)
      concurrency   (default 2)
      timeout_sec   (default 45)
    """
    cfg = (config.get("fetchers") or {}).get("letsfg") or {}
    mode = cfg.get("mode", DEFAULT_MODE)
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
    coros = [
        _fetch_one(sem, leg, rates, currency, limit, max_browsers, mode, timeout)
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


async def fetch_cash_offers(
    legs: List[Leg], config: dict, session: aiohttp.ClientSession = None
) -> Dict[str, List[dict]]:
    """Like fetch_cash_fares but returns *all* priced offers per leg, not just
    the cheapest. Each leg value is a List[dict] sorted by price ascending.

    Useful for the ad-hoc search UI where the user wants to compare carriers
    and booking sources side-by-side. Daily digest still uses fetch_cash_fares.
    """
    cfg = (config.get("fetchers") or {}).get("letsfg") or {}
    mode = cfg.get("mode", DEFAULT_MODE)
    currency = cfg.get("currency", "USD")
    # Bump the default limit so we actually collect alternatives, not just 10
    # total (LetsFG aggregates across connectors before applying limit).
    limit = int(cfg.get("limit", 25))
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
    coros = [
        _fetch_offers_one(sem, leg, rates, currency, limit, max_browsers, mode, timeout)
        for leg in legs
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    out: Dict[str, List[dict]] = {}
    for leg, res in zip(legs, results):
        if isinstance(res, Exception):
            log.warning("letsfg leg %s exception: %s", leg.key, res)
            out[leg.key] = []
        else:
            out[leg.key] = res
    return out
