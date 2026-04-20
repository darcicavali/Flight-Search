"""Shared helpers for fetchers: USD conversion, retry, leg shape."""

import asyncio
import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

EXCHANGE_URL = "https://api.exchangerate-api.com/v4/latest/USD"
_rates_cache: Optional[dict] = None


async def get_rates(session: aiohttp.ClientSession) -> dict:
    """Fetch USD-base FX rates once per process."""
    global _rates_cache
    if _rates_cache is not None:
        return _rates_cache
    try:
        async with session.get(EXCHANGE_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            data = await r.json()
            _rates_cache = data.get("rates", {})
    except Exception as e:
        log.warning("FX rate fetch failed, defaulting to USD=1 only: %s", e)
        _rates_cache = {"USD": 1.0}
    return _rates_cache


def to_usd(amount: float, currency: str, rates: dict) -> float:
    """Convert amount in `currency` to USD using rates (USD-base)."""
    if not amount:
        return 0.0
    if currency == "USD":
        return float(amount)
    rate = rates.get(currency)
    if not rate:
        log.warning("No FX rate for %s, treating as USD", currency)
        return float(amount)
    return float(amount) / rate


def empty_leg_result(origin: str, destination: str, iso_date: str) -> dict:
    return {
        "origin": origin,
        "destination": destination,
        "date": iso_date,
        "price_usd": None,
        "airline": None,
        "airline_name": None,
        "duration_hours": None,
        "duration_str": None,
        "stops": None,
        "segments": [],            # [{carrier, flight_no, origin, destination, depart, arrive}]
        "layovers": [],            # ["LIM 2h15m"]
        "depart_time": None,       # "HH:MM"
        "arrive_time": None,       # "HH:MM" (plus "+1" suffix if next-day)
        "booking_url": None,
        "awards": {},
        "source": None,
        "error": None,
    }


async def gather_limited(limit: int, coros):
    """asyncio.gather with a semaphore to bound concurrency."""
    sem = asyncio.Semaphore(limit)

    async def _wrap(c):
        async with sem:
            return await c

    return await asyncio.gather(*(_wrap(c) for c in coros), return_exceptions=True)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)
