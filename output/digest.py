"""Format and send the daily flight digest."""

import logging
import os
from datetime import date
from typing import List, Optional

from engine.scorer import ScoredCombo
from output.airlines import airline_name
from output.award_links import build_award_links

log = logging.getLogger(__name__)


def _delta_str(current: float, prev: Optional[float]) -> str:
    if prev is None:
        return ""
    diff = current - prev
    if abs(diff) < 5:
        return "  —"
    arrow = "↓" if diff < 0 else "↑"
    return f"  {arrow}${abs(diff):.0f} vs yesterday"


def _format_leg(i: int, l: dict) -> str:
    price = l.get("price_usd")
    price_s = f"${price:.0f}" if price else "—"
    carrier = airline_name(l.get("airline") or "") or "airline?"
    duration = l.get("duration_str") or ""

    timing = ""
    dep = l.get("depart_time")
    arr = l.get("arrive_time")
    if dep and arr:
        timing = f"  {dep}→{arr}"

    stops = l.get("stops")
    layovers = l.get("layovers") or []
    if stops and stops > 0:
        if layovers:
            stops_str = f"  via {', '.join(layovers)}"
        else:
            stops_str = f"  ({stops} stop{'s' if stops > 1 else ''})"
    else:
        stops_str = "  nonstop" if stops == 0 else ""

    flight_nos = ""
    segs = l.get("segments") or []
    if segs and all(s.get("flight_no") and s.get("carrier") for s in segs):
        flight_nos = "  [" + ", ".join(f"{s['carrier']}{s['flight_no']}" for s in segs) + "]"

    header = (f"   Leg {i+1}: {l['origin']} → {l['destination']}  {l['date']}"
              f"{timing}  {price_s}  {duration}")
    detail = f"          {carrier}{stops_str}{flight_nos}"
    return header + "\n" + detail


def _format_combo_block(sc: ScoredCombo, prev_day_data: Optional[dict] = None) -> str:
    legs_str = "\n".join(_format_leg(i, l) for i, l in enumerate(sc.legs_data))

    points_str = ""
    if sc.best_points_option:
        p = sc.best_points_option
        points_str = (
            f"\n   POINTS: {p['program'].upper()}  "
            f"{p['total_points']:,} pts + ${p['fees_usd']:.0f} fees"
            f"\n   = ${p['equiv_usd']:.0f} equiv @ {p['cpp_used']}cpp  →  {sc.verdict}"
        )

    flags_str = "\n".join(f"   {f}" for f in (sc.combo.flags or []))

    booking_str = "\n".join(
        f"   👉 Book leg {bl['leg']}: {bl['url']}" for bl in sc.booking_links
    )

    stopover_str = (
        f"Stopover: {sc.combo.stopover_city} {sc.combo.stopover_days} days"
        if sc.combo.stopover_city
        else "Direct route"
    )

    prev_price = None
    if prev_day_data and sc.combo.id in prev_day_data:
        prev_price = prev_day_data[sc.combo.id].get("total_cash_usd")

    return (
        f"{legs_str}\n"
        f"   ─────────────────────────────────────────────────\n"
        f"   TOTAL CASH:  ${sc.total_cash_usd:.0f}{_delta_str(sc.total_cash_usd, prev_price)}  |  "
        f"{sc.total_travel_hours:.0f}h flight time\n"
        f"   {stopover_str}"
        f"{points_str}\n"
        f"{flags_str}\n"
        f"{booking_str}\n"
    )


def _format_rank_row(rank: int, sc: ScoredCombo) -> str:
    route = " → ".join([sc.legs_data[0]["origin"]]
                       + [l["destination"] for l in sc.legs_data])
    return (
        f"  {rank:>2}. {route:<28}  "
        f"${sc.total_cash_usd:>6.0f}  "
        f"{sc.total_travel_hours:>4.0f}h  "
        f"{sc.verdict}\n"
    )


def _format_manual_award_check(
    ranked: List[ScoredCombo], cpp_valuations: dict, top_n_combos: int = 3
) -> str:
    """For the top N combos, list each unique leg with deep links to every
    relevant program's award search page. Deduplicated by (origin, dest, date).
    """
    seen = set()
    sections: List[str] = []

    for sc in ranked[:top_n_combos]:
        for leg in sc.legs_data:
            key = (leg["origin"], leg["destination"], leg["date"])
            if key in seen:
                continue
            seen.add(key)

            # Skip legs where the fetcher already returned award data — no need
            # to ask the user to check manually.
            if (leg.get("awards") or {}):
                continue

            links = build_award_links(
                leg["origin"], leg["destination"], leg["date"], cpp_valuations
            )
            if not links:
                continue

            header = f"  {leg['origin']} → {leg['destination']}  {leg['date']}"
            body = "\n".join(f"    • {l.label}: {l.url}" for l in links)
            sections.append(f"{header}\n{body}")

    if not sections:
        return "  (all legs have automated award data)"
    return "\n\n".join(sections)


def _format_award_alerts(ranked: List[ScoredCombo]) -> str:
    lines = []
    seen_programs = set()
    for sc in ranked:
        if sc.best_points_option and sc.best_points_option["program"] not in seen_programs:
            seen_programs.add(sc.best_points_option["program"])
            p = sc.best_points_option
            route = " → ".join([sc.legs_data[0]["origin"]]
                               + [l["destination"] for l in sc.legs_data])
            lines.append(
                f"  • {p['program'].upper()} available on {route}: "
                f"{p['total_points']:,} pts + ${p['fees_usd']:.0f}"
            )
    if not lines:
        return "  No award space found in current results."
    return "\n".join(lines)


def format_digest(
    ranked_combos: List[ScoredCombo],
    run_date: date,
    trip_config: dict,
    prev_day_data: Optional[dict] = None,
) -> str:
    if not ranked_combos:
        return "No complete flight combinations found. Check API credentials and retry."

    top_cash = next((c for c in ranked_combos if c.verdict == "CASH WINS"), ranked_combos[0])
    top_points = next((c for c in ranked_combos if "POINTS" in c.verdict), None)
    top_direct = next((c for c in ranked_combos if c.combo.combo_type == "direct"), None)

    # Best combo per stopover city — cheapest combo that routes through each
    # candidate Caribbean city, regardless of whether it tops the overall list.
    stopover_candidates = (trip_config.get("stopovers") or {}).get("candidates") or []
    best_per_stopover = {}
    for city in stopover_candidates:
        best = next(
            (c for c in ranked_combos if c.combo.stopover_city == city),
            None,
        )
        if best:
            best_per_stopover[city] = best

    window = trip_config["travel_window"]
    header = (
        "═══════════════════════════════════════════════════════════════\n"
        f"  ✈  FLIGHT DIGEST  |  {run_date.strftime('%A %B %d, %Y')}\n"
        f"  {trip_config['origin']} → Caribbean → GRU → SC/PR\n"
        f"  Window: {window['earliest_depart']} – {window['latest_depart']}\n"
        "═══════════════════════════════════════════════════════════════\n"
    )

    points_block = (
        _format_combo_block(top_points, prev_day_data)
        if top_points
        else "  No award space found for this date range\n"
    )
    direct_block = (
        _format_combo_block(top_direct, prev_day_data)
        if top_direct
        else "  No direct options in range\n"
    )

    top_rows = "".join(
        _format_rank_row(i + 1, c) for i, c in enumerate(ranked_combos[:10])
    )

    cpp = trip_config.get("cpp_valuations", {})
    manual_check = _format_manual_award_check(ranked_combos, cpp)

    if best_per_stopover:
        stopover_sections = []
        for city in stopover_candidates:
            if city in best_per_stopover:
                stopover_sections.append(
                    f"▸ Best via {city}:\n"
                    f"{_format_combo_block(best_per_stopover[city], prev_day_data)}"
                )
            else:
                stopover_sections.append(
                    f"▸ Best via {city}:\n  (no priced combo found)\n"
                )
        stopover_block = "\n".join(stopover_sections)
    else:
        stopover_block = "  (no stopover candidates configured)\n"

    return (
        f"{header}\n"
        "🥇 BEST CASH COMBO\n"
        f"{_format_combo_block(top_cash, prev_day_data)}\n"
        "🏆 BEST POINTS COMBO\n"
        f"{points_block}\n"
        "✈  BEST DIRECT (no stopover)\n"
        f"{direct_block}\n"
        "──────────────────────────────────────────────────────────────\n"
        "🏝  BEST STOPOVER OPTIONS  (cheapest combo via each Caribbean city)\n"
        f"{stopover_block}\n"
        "──────────────────────────────────────────────────────────────\n"
        f"FULL RANKING (top 10 of {len(ranked_combos)} combinations)\n\n"
        f"{top_rows}\n"
        "──────────────────────────────────────────────────────────────\n"
        "AWARD SPACE ALERTS\n"
        f"{_format_award_alerts(ranked_combos)}\n"
        "──────────────────────────────────────────────────────────────\n"
        "MANUAL AWARD CHECK (click each link, scan for availability)\n"
        f"{manual_check}\n"
        "═══════════════════════════════════════════════════════════════\n"
    )


def send_email(digest: str, recipient: str, subject: Optional[str] = None) -> bool:
    """Send digest via SendGrid. Returns True on success."""
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL")
    if not api_key or not from_email or not recipient:
        log.warning("SendGrid not configured (api_key/from/recipient). Skipping send.")
        return False

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
    except ImportError:
        log.error("sendgrid package not installed")
        return False

    subject = subject or f"Flight Digest — {date.today().isoformat()}"
    msg = Mail(
        from_email=from_email,
        to_emails=recipient,
        subject=subject,
        plain_text_content=digest,
    )
    try:
        client = SendGridAPIClient(api_key)
        resp = client.send(msg)
        log.info("SendGrid response status=%s", resp.status_code)
        return 200 <= resp.status_code < 300
    except Exception as e:
        log.error("SendGrid send failed: %s", e)
        return False
