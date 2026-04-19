from fetchers.amadeus import merge_cash


def _leg(price, source):
    return {"price_usd": price, "source": source, "awards": {}}


def test_merge_picks_lowest_price_across_three_sources():
    kiwi = {"L1": _leg(500, "kiwi"), "L2": _leg(999, "kiwi")}
    amadeus = {"L1": _leg(450, "amadeus"), "L2": _leg(800, "amadeus")}
    duffel = {"L1": _leg(520, "duffel"), "L2": _leg(750, "duffel")}
    merged = merge_cash(kiwi, amadeus, duffel)
    assert merged["L1"]["source"] == "amadeus"  # cheapest
    assert merged["L2"]["source"] == "duffel"


def test_merge_falls_back_when_one_source_has_no_price():
    kiwi = {"L1": {"price_usd": None, "source": "kiwi", "awards": {},
                   "error": "no offers"}}
    amadeus = {"L1": _leg(450, "amadeus")}
    merged = merge_cash(kiwi, amadeus)
    assert merged["L1"]["source"] == "amadeus"


def test_merge_handles_missing_leg_in_one_source():
    kiwi = {"L1": _leg(300, "kiwi")}
    amadeus = {"L2": _leg(400, "amadeus")}
    merged = merge_cash(kiwi, amadeus)
    assert merged["L1"]["source"] == "kiwi"
    assert merged["L2"]["source"] == "amadeus"


def test_merge_handles_empty_sources():
    merged = merge_cash({}, {}, {"L1": _leg(100, "duffel")})
    assert merged["L1"]["source"] == "duffel"
