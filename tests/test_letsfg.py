"""Tests for the LetsFG fetcher adapter — no network calls.

We monkeypatch `_search_async` (the only thing that touches the library) with
async fakes that mimic LetsFG's `search_local` dict return shape.
"""

import asyncio
from datetime import date

import pytest

from engine.routes import Leg
from fetchers import letsfg


def _seg(airline, flight_no, origin, destination, departure, arrival,
         duration_seconds=0):
    return {
        "airline": airline,
        "flight_no": flight_no,
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "arrival": arrival,
        "duration_seconds": duration_seconds,
    }


def _route(segments, total_duration_seconds, stopovers=0):
    return {
        "segments": segments,
        "total_duration_seconds": total_duration_seconds,
        "stopovers": stopovers,
    }


def _offer(price, currency, outbound, airlines, owner_airline, booking_url):
    return {
        "id": "fake_id",
        "price": price,
        "currency": currency,
        "outbound": outbound,
        "inbound": None,
        "airlines": airlines,
        "owner_airline": owner_airline,
        "booking_url": booking_url,
    }


def _direct_offer():
    seg = _seg(
        "UA", "UA823", "ORD", "GRU",
        "2026-07-26T19:21:00-05:00", "2026-07-27T09:10:00-03:00",
        duration_seconds=42540,
    )
    return _offer(
        987.50, "USD",
        _route([seg], total_duration_seconds=42540, stopovers=0),
        ["UA"], "UA",
        "https://example.com/book/UA823",
    )


def _two_stop_offer():
    s1 = _seg("NK", "NK756", "ORD", "MIA",
              "2026-07-26T07:10:00-05:00", "2026-07-26T11:23:00-04:00")
    s2 = _seg("DM", "DM5103", "MIA", "PUJ",
              "2026-07-26T16:02:00-04:00", "2026-07-26T18:38:00-04:00")
    s3 = _seg("DM", "DM6088", "PUJ", "GRU",
              "2026-07-26T20:10:00-04:00", "2026-07-27T04:20:00-03:00")
    return _offer(
        457.0, "USD",
        _route([s1, s2, s3], total_duration_seconds=46740, stopovers=2),
        ["NK", "DM"], "NK",
        "https://skiplagged.com/flights/ORD/GRU/2026-07-26",
    )


def _empty_search():
    return {"offers": []}


def _patch_search(monkeypatch, by_route):
    """by_route maps (origin,dest,iso_date) → dict result (or callable raising)."""
    async def fake(origin, destination, iso_date, currency, limit, max_browsers, mode):
        v = by_route.get((origin, destination, iso_date))
        if callable(v):
            return v()
        return v if v is not None else _empty_search()
    monkeypatch.setattr(letsfg, "_search_async", fake)


def _run(legs, config=None):
    return asyncio.run(letsfg.fetch_cash_fares(legs, config or {}))


def test_direct_offer_maps_to_leg_result(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): {"offers": [_direct_offer()]},
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
        ("ORD", "GRU", "2026-07-26"): {"offers": [_two_stop_offer()]},
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
    eur_offer["price"] = 1000.0
    eur_offer["currency"] = "EUR"
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): {"offers": [eur_offer]},
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
    nvt_seg = _seg("G3", "G31234", "GRU", "NVT",
                   "2026-07-28T10:00:00-03:00", "2026-07-28T11:15:00-03:00")
    nvt_offer = _offer(
        89.0, "USD",
        _route([nvt_seg], total_duration_seconds=4500, stopovers=0),
        ["G3"], "G3",
        "https://voegol.com.br/x",
    )
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): {"offers": [_direct_offer()]},
        ("GRU", "NVT", "2026-07-28"): {"offers": [nvt_offer]},
    })
    out = _run([leg1, leg2])
    assert out[leg1.key]["price_usd"] == 987.50
    assert out[leg2.key]["price_usd"] == 89.0
    assert out[leg2.key]["airline"] == "G3"


def test_timeout_returns_error(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    async def slow(origin, destination, iso_date, currency, limit, max_browsers, mode):
        await asyncio.sleep(2)
        return {"offers": [_direct_offer()]}
    monkeypatch.setattr(letsfg, "_search_async", slow)
    cfg = {"fetchers": {"letsfg": {"timeout_sec": 0.1}}}
    res = asyncio.run(letsfg.fetch_cash_fares([leg], cfg))[leg.key]
    assert res["price_usd"] is None
    assert res["error"] and "timeout" in res["error"]


def test_outbound_none_returns_no_outbound_segments(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    offer = _direct_offer()
    offer["outbound"] = None
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): {"offers": [offer]},
    })
    res = _run([leg])[leg.key]
    assert res["price_usd"] is None
    assert res["error"] == "no outbound segments"


def test_empty_outbound_segments_returns_no_outbound_segments(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    offer = _direct_offer()
    offer["outbound"] = _route([], total_duration_seconds=0)
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): {"offers": [offer]},
    })
    res = _run([leg])[leg.key]
    assert res["price_usd"] is None
    assert res["error"] == "no outbound segments"


def test_airline_falls_back_to_airlines_list_when_owner_missing(monkeypatch):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    offer = _direct_offer()
    offer["owner_airline"] = ""
    offer["airlines"] = ["DL", "AF"]
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): {"offers": [offer]},
    })
    res = _run([leg])[leg.key]
    assert res["airline"] == "DL"
    assert res["error"] is None


def test_unknown_currency_treated_as_usd_with_warning(monkeypatch, caplog):
    leg = Leg("ORD", "GRU", date(2026, 7, 26), 1)
    offer = _direct_offer()
    offer["price"] = 321.0
    offer["currency"] = "XYZ"
    _patch_search(monkeypatch, {
        ("ORD", "GRU", "2026-07-26"): {"offers": [offer]},
    })
    import fetchers.common as common
    common._rates_cache = {"USD": 1.0}
    try:
        with caplog.at_level("WARNING"):
            res = _run([leg])[leg.key]
    finally:
        common._rates_cache = None
    assert res["price_usd"] == 321.0
    assert "No FX rate for XYZ" in caplog.text


def test_concurrency_cap_limits_in_flight_search_calls(monkeypatch):
    legs = [Leg("ORD", "GRU", date(2026, 7, 26 + i), i + 1) for i in range(4)]
    state = {"in_flight": 0, "max_in_flight": 0}

    async def fake(origin, destination, iso_date, currency, limit, max_browsers, mode):
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        try:
            await asyncio.sleep(0.1)
        finally:
            state["in_flight"] -= 1
        return {"offers": [_direct_offer()]}

    monkeypatch.setattr(letsfg, "_search_async", fake)
    cfg = {"fetchers": {"letsfg": {"concurrency": 2}}}
    out = _run(legs, cfg)
    assert len(out) == 4
    assert state["max_in_flight"] <= 2
