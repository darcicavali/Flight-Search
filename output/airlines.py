"""IATA carrier code → human-readable airline name.

Covers carriers relevant to ORD ↔ Caribbean ↔ GRU ↔ Brazilian domestic, plus
common European and Middle Eastern carriers that sometimes route through SA.

Duffel test mode uses 'ZZ' (Duffel Airways) as a mock carrier; we flag it
explicitly so the digest makes clear the result is fake inventory.
"""

IATA_TO_NAME = {
    # US legacy
    "AA": "American Airlines",
    "UA": "United Airlines",
    "DL": "Delta Air Lines",
    "AS": "Alaska Airlines",
    "B6": "JetBlue",
    # US low-cost
    "NK": "Spirit Airlines",
    "F9": "Frontier Airlines",
    "WN": "Southwest Airlines",
    "SY": "Sun Country",
    # Latin America — international
    "LA": "LATAM Airlines",
    "JJ": "LATAM Brasil",
    "CM": "Copa Airlines",
    "AV": "Avianca",
    "AR": "Aerolíneas Argentinas",
    "4M": "LATAM Argentina",
    "H2": "Sky Airline",
    "JA": "JetSMART",
    "Z8": "Amaszonas",
    # Brazil — domestic
    "G3": "GOL",
    "AD": "Azul",
    "2Z": "Voepass (Passaredo)",
    # Caribbean / Central America
    "AM": "Aeroméxico",
    "Y4": "Volaris",
    "VB": "VivaAerobus",
    "3M": "Silver Airways",
    "9K": "Cayman Airways",
    "BW": "Caribbean Airlines",
    "WG": "Sunwing",
    "P6": "Wingo",
    "DM": "Arajet",
    # Canada
    "AC": "Air Canada",
    "WS": "WestJet",
    # Europe
    "BA": "British Airways",
    "IB": "Iberia",
    "AF": "Air France",
    "KL": "KLM",
    "LH": "Lufthansa",
    "LX": "Swiss",
    "TP": "TAP Air Portugal",
    "VS": "Virgin Atlantic",
    "EI": "Aer Lingus",
    # Middle East
    "TK": "Turkish Airlines",
    "QR": "Qatar Airways",
    "EK": "Emirates",
    "EY": "Etihad",
    # African / other relevant long-haul
    "ET": "Ethiopian Airlines",
    "SA": "South African Airways",
    # Duffel test mode — call it out as fake
    "ZZ": "(Duffel test carrier — mock data)",
    "DU": "(Duffel test carrier — mock data)",
}


def airline_name(code: str) -> str:
    """Return a readable airline name for an IATA code, falling back to the code."""
    if not code:
        return ""
    # Handle combined codes like "AA/AV" from the Kiwi fetcher
    parts = [p.strip() for p in code.replace("-", "/").split("/") if p.strip()]
    if len(parts) > 1:
        return " + ".join(airline_name(p) for p in parts)
    return IATA_TO_NAME.get(code.upper(), code)
