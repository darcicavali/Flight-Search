"""Format and send the daily flight digest."""

import logging
import os
from datetime import date
from typing import List, Optional

from engine.scorer import ScoredCombo

log = logging.getLogger(__name__)


def _delta_str(current: float, prev: Optional[float]) -> str:
    if prev is None:
        return ""
    diff = current - prev
    if abs(diff) < 5:
        return "  —"
    arrow = "↓" if diff < 0 else "↑"
    return f"  {arrow}${abs(diff):.0f} vs yesterday"


def _format_combo_block(sc: ScoredCombo, prev_day_data: Optional[dict] = None) -> str:
    legs_str_lines = []
    for i, l in enumerate(sc.legs_data):
        price = l.get("price_usd")
        price_s = f"${price:.0f}" if price else "—"
        legs_str_lines.append(
            f"   Leg {i+1}: {l['origin']} → {l['destination']}  "
            f"{l['date']}  {l.get('airline') or ''}  "
            f"{price_s}  {l.get('duration_str') or ''}"
        )
    legs_str = "\n".join(legs_str_lines)

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

    return (
        f"{header}\n"
        "🥇 BEST CASH COMBO\n"
        f"{_format_combo_block(top_cash, prev_day_data)}\n"
        "🏆 BEST POINTS COMBO\n"
        f"{points_block}\n"
        "✈  BEST DIRECT (no stopover)\n"
        f"{direct_block}\n"
        "──────────────────────────────────────────────────────────────\n"
        f"FULL RANKING (top 10 of {len(ranked_combos)} combinations)\n\n"
        f"{top_rows}\n"
        "──────────────────────────────────────────────────────────────\n"
        "AWARD SPACE ALERTS\n"
        f"{_format_award_alerts(ranked_combos)}\n"
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
