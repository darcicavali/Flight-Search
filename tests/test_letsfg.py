"""Tests for the LetsFG fetcher adapter — no network calls.

We monkeypatch `_search_sync` (the only thing that touches the library) with
fakes that mimic LetsFG's FlightOffer/FlightRoute/FlightSegment dataclasses.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

import pytest

from engine.routes import Leg
from fetchers import letsfg


@dataclass
class _FakeSeg:
    airline: str
    flight_no: str
    origin: str
    destination: str
    departure: str
    arrival: str
    airline_name: str = ""
    duration_seconds: int = 0
    cabin_class: str = "economy"
    aircraft: str = ""
    origin_city: str = ""
    destination_city: str = ""


@dataclass
class _FakeRoute:
    segments: List[_FakeSeg]
    total_duration_seconds: int
    stopovers: int = 0


@dataclass
class _FakeOffer:
    price: float
    currency: str
    outbound: Optional[_FakeRoute]
    airlines: List[str]
    owner_airline: str
    booking_url: str
    inbound: Optional[_FakeRoute] = None
    id: str = "fake_id"
    price_formatted: str = ""
    bags_price: dict = field(default_factory=dict)
    availability_seats: Optional[int] = None
    conditions: dict = field(default_factory=dict)
    is_locked: bool = False
    fetched_at: str = ""


@dataclass
class _FakeSearch:
    offers: List[_FakeOffer]
    passenger_ids: list = field(default_factory=list)


def _direct_offer():
    seg = _FakeSeg(
        airline="UA", flight_no="UA823",
        origin="ORD", destination="GRU",
        departure="2026-07-26T19:21:00-05:00",
        arrival="2026-07-27T09:10:00-03:00",
        duration_seconds=42540,
    )
    return _FakeOffer(
        price=987.50, currency="USD",
        outbound=_FakeRoute(segments=[seg], total_duration_seconds=42540, stopovers=0),
        airlines=["UA"], owner_airline="UA",
        booking_url="https://example.com/book/UA823",
    )


def _two_stop_offer():
    s1 = _FakeSeg("NK", "NK756", "ORD", "MIA",
                  "2026-07-26T07:10:00-05:00", "2026-07-26T11:23:00-04:00")
    s2 = _FakeSeg("DM", "DM5103", "MIA", "PUJ",
                  "2026-07-26T16:02:00-04:00", "2026-07-26T18:38:00-04:00")
    s3 = _FakeSeg("DM", "DM6088", "PUJ", "GRU",
                  "2026-07-26T20:10:00-04:00", "2026-07-27T04:20:00-03:00")
    return _FakeOffer(
        price=457.0, currency="USD",
        outbound=_FakeRoute(segments=[s1, s2, s3],
                            total_duration_seconds=46740, stopovers=2),
        airlines=["NK", "DM"], owner_airline="NK",
        booking_url="https://skiplagged.com/flights/ORD/GRU/2026-07-26",
    )


def _empty_search():
    return _FakeSearch(offers=[])


def _patch_search(monkeypatch, by_route):
    """by_route maps (origin,dest,iso_date) → _FakeSearch (or callable raising)."""
    def fake(origin, destination, iso_date, currency, limit, max_browsers):
        v = by_route.get((origin, destination, iso_date))
        if callable(v):
            return v()
        return v if v is not None else _empty_search()
    monkeypatch.setattr(letsfg, "_search_sync", fake)


def _run(legs, config=None):
    return asyncio.run(letsfg.fetch_cash_fares(legs, config or {}))


def test_direct_offer_maps_to_leg_result(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): _FakeSearch(offers=[_direct_offer()]),
    })
    out = _run([leg])
    res = out[leg.key]
    assert res["source"] == "letsfg"
    assert res["price_usd"] == 987.50
    assert res["airline"] == "UA"
    assert res["stops"] == 0
    assert res["depart_time"] == "19:21"
    assert res["arrive_time"] == "09:10+1"
    assert res["booking_url"].endswith("UA823")
    assert res["error"] is None
    assert res["duration_str"] == "11h49m"


def test_multistop_offer_produces_layovers(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): _FakeSearch(offers=[_two_stop_offer()]),
    })
    res = _run([leg])[leg.key]
    assert res["stops"] == 2
    assert any(lo.startswith("MIA ") for lo in res["layovers"])
    assert any(lo.startswith("PUJ ") for lo in res["layovers"])
    assert res["arrive_time"] == "04:20+1"


def test_no_offers_returns_error_not_crash(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    _patch_search(monkeypatch, {})  # empty search for everything
    res = _run([leg])[leg.key]
    assert res["price_usd"] is None
    assert res["error"] == "no offers"


def test_exception_in_search_is_captured(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    def boom():
        raise RuntimeError("playwright crashed")
    _patch_search(monkeypatch, {("ORD", "GRU", "2026-07-26"): boom})
    res = _run([leg])[leg.key]
    assert res["price_usd"] is None
    assert res["error"] and "playwright crashed" in res["error"]


def test_currency_conversion_applied(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    eur_offer = _direct_offer()
    eur_offer.price = 1000.0
    eur_offer.currency = "EUR"
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): _FakeSearch(offers=[eur_offer]),
    })
    # Stub FX so we don't depend on the network
    import fetchers.common as common
    common._rates_cache = {"USD": 1.0, "EUR": 0.8}  # 1 USD = 0.8 EUR → 1000 EUR = 1250 USD
    try:
        res = _run([leg])[leg.key]
    finally:
        common._rates_cache = None
    assert res["price_usd"] == 1250.0


def test_multiple_legs_each_get_their_own_search(monkeypatch):
    leg1 = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    leg2 = Leg("GRU", "NVT", date(2026, 7, 28), 2)
    nvt_seg = _FakeSeg("G3", "G31234", "GRU", "NVT",
                       "2026-07-28T10:00:00-03:00", "2026-07-28T11:15:00-03:00")
    nvt_offer = _FakeOffer(
        price=89.0, currency="USD",
        outbound=_FakeRoute(segments=[nvt_seg], total_duration_seconds=4500, stopovers=0),
        airlines=["G3"], owner_airline="G3",
        booking_url="https://voegol.com.br/x",
    )
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): _FakeSearch(offers=[_direct_offer()]),
        ("GRU", "NVT", "2026-07-28"): _FakeSearch(offers=[nvt_offer]),
    })
    out = _run([leg1, leg2])
    assert out[leg1.key]["price_usd"] == 987.50
    assert out[leg2.key]["price_usd"] == 89.0
    assert out[leg2.key]["airline"] == "G3"


def test_timeout_returns_error(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    import time
    def slow():
        time.sleep(2)
        return _FakeSearch(offers=[_direct_offer()])
    _patch_search(monkeypatch, {("ORD", "GRU", "2026-07-26"): slow})
    cfg = {"fetchers": {"letsfg": {"timeout_sec": 0.1}}}
    res = asyncio.run(letsfg.fetch_cash_fares([leg], cfg))[leg.key]
    assert res["price_usd"] is None
    assert res["error"] and "timeout" in res["error"]
