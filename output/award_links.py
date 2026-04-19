"""Generate deep links to each loyalty program's own award search page.

Used as a fallback when Seats.aero isn't configured — the user clicks through
to each program, which loads a pre-populated search for the exact leg.

URL formats are best-effort: airline sites change them occasionally. If a link
stops working, update the template here and the digest picks it up.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

# Which programs are relevant per region pair. Keeps noise out of the digest
# (no point linking Aeroplan for a GRU→NVT domestic leg).
INTL_PROGRAMS = ["aeroplan", "lifemiles", "united_mp", "flying_blue", "american_aa"]
BR_DOMESTIC_PROGRAMS = ["smiles", "latam_pass", "tudoazul"]

BR_AIRPORTS = {"NVT", "JOI", "CWB", "GRU", "CGH", "VCP", "SDU", "GIG",
               "BSB", "CNF", "REC", "SSA", "FLN", "POA"}


@dataclass
class AwardLink:
    program: str
    label: str
    url: str


def _aeroplan(origin: str, destination: str, iso_date: str) -> str:
    return (
        "https://www.aircanada.com/aeroplan/redeem/availability/outbound"
        f"?org0={origin}&dest0={destination}&departureDate0={iso_date}"
        "&lang=en-CA&tripType=O&ADT=1&CHD=0&INF=0&INS=0&marketCode=INT"
    )


def _lifemiles(origin: str, destination: str, iso_date: str) -> str:
    return (
        "https://www.lifemiles.com/fly/redeem/search-results"
        f"?origin={origin}&destination={destination}"
        f"&departureDate={iso_date}&adults=1&children=0&infants=0"
        "&cabin=COACH&tripType=ONEWAY"
    )


def _united_mp(origin: str, destination: str, iso_date: str) -> str:
    return (
        "https://www.united.com/en/us/fsr/choose-flights"
        f"?f={origin}&t={destination}&d={iso_date}"
        "&tt=1&at=1&sc=7&px=1&taxng=1&clm=7&st=bestmatches"
        "&act=1&mm=award&fl=MP"
    )


def _flying_blue(origin: str, destination: str, iso_date: str) -> str:
    return (
        "https://www.airfrance.com/search/advanced"
        f"?bookingFlow=REWARD&connections={origin},{destination}"
        f"&departureDates={iso_date}"
        "&pax=1-0-0-0-0-0-0-0&cabinClass=ECONOMY"
    )


def _american_aa(origin: str, destination: str, iso_date: str) -> str:
    return (
        "https://www.aa.com/booking/choose-flights/1"
        f"?tripType=oneWay&searchType=Award&origin={origin}"
        f"&destination={destination}&departDate={iso_date}"
        "&passengerCount=1&cabin=COACH"
    )


def _smiles(origin: str, destination: str, iso_date: str) -> str:
    return (
        "https://www.smiles.com.br/mfe/emissao-passagens"
        f"?adults=1&cabin=ECONOMIC&origin={origin}&destination={destination}"
        f"&departureDate={iso_date}&tripType=2"
    )


def _latam_pass(origin: str, destination: str, iso_date: str) -> str:
    return (
        "https://www.latamairlines.com/br/pt/oferta-voos"
        f"?origin={origin}&destination={destination}&outbound={iso_date}"
        "&adt=1&chd=0&inf=0&trip=OW&redemption=true&sort=RECOMMENDED"
    )


def _tudoazul(origin: str, destination: str, iso_date: str) -> str:
    return (
        "https://www.voeazul.com.br/br/en/home/flight-selection"
        f"?origin={origin}&destination={destination}"
        f"&departureDate={iso_date}&adultPassengers=1&useRewardPoints=true"
    )


_BUILDERS = {
    "aeroplan": ("Aeroplan", _aeroplan),
    "lifemiles": ("LifeMiles", _lifemiles),
    "united_mp": ("United MP", _united_mp),
    "flying_blue": ("Flying Blue", _flying_blue),
    "american_aa": ("American AA", _american_aa),
    "smiles": ("Smiles (GOL)", _smiles),
    "latam_pass": ("LATAM Pass", _latam_pass),
    "tudoazul": ("TudoAzul", _tudoazul),
}


def _is_br_domestic(origin: str, destination: str) -> bool:
    return origin in BR_AIRPORTS and destination in BR_AIRPORTS


def build_award_links(
    origin: str,
    destination: str,
    iso_date: str,
    cpp_valuations: Optional[dict] = None,
) -> List[AwardLink]:
    """Return a list of AwardLink for the programs that make sense for this leg.

    If `cpp_valuations` is passed, only programs the user has a CPP defined for
    are included (so irrelevant programs don't clutter the digest).
    """
    if _is_br_domestic(origin, destination):
        candidates = BR_DOMESTIC_PROGRAMS
    else:
        candidates = INTL_PROGRAMS

    if cpp_valuations:
        candidates = [p for p in candidates if p in cpp_valuations]

    links: List[AwardLink] = []
    for program in candidates:
        entry = _BUILDERS.get(program)
        if not entry:
            continue
        label, builder = entry
        links.append(AwardLink(program=program, label=label,
                               url=builder(origin, destination, iso_date)))
    return links
