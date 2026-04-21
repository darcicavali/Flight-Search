from output.airlines import airline_name


def test_common_iata_codes_resolve_to_human_names():
    for code in ["UA", "AA", "DL", "G3", "AD", "LA", "DM", "BA", "TP", "AZ", "9R", "4C"]:
        name = airline_name(code)
        assert name
        assert name != code


def test_g3_resolves_to_gol():
    assert "GOL" in airline_name("G3")
