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


def _carrier_display(l: dict) -> str:
    """Carrier string with codeshare annotation. 'British Airways (op. by United)'."""
    marketing = airline_name(l.get("airline") or "") or "airline?"
    op = l.get("operated_by")
    if op and op != (l.get("airline") or ""):
        return f"{marketing} (op. by {airline_name(op)})"
    return marketing


def _stops_str(l: dict) -> str:
    stops = l.get("stops")
    layovers = l.get("layovers") or []
    if stops and stops > 0:
        if layovers:
            return f"via {', '.join(layovers)}"
        return f"{stops} stop{'s' if stops > 1 else ''}"
    return "nonstop" if stops == 0 else ""


def _flight_numbers_str(l: dict) -> str:
    segs = l.get("segments") or []
    if segs and all(s.get("flight_no") and s.get("carrier") for s in segs):
        return ", ".join(f"{s['carrier']}{s['flight_no']}" for s in segs)
    return ""


def _format_leg(i: int, l: dict) -> str:
    price = l.get("price_usd")
    price_s = f"${price:.0f}" if price else "—"
    carrier = _carrier_display(l)
    duration = l.get("duration_str") or ""

    dep = l.get("depart_time")
    arr = l.get("arrive_time")
    timing = f"  {dep}→{arr}" if dep and arr else ""

    stops_str = _stops_str(l)
    stops_part = f"  {stops_str}" if stops_str else ""

    nums = _flight_numbers_str(l)
    nums_part = f"  [{nums}]" if nums else ""

    header = (f"   Leg {i+1}: {l['origin']} → {l['destination']}  {l['date']}"
              f"{timing}  {price_s}  {duration}")
    detail = f"          {carrier}{stops_part}{nums_part}"
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


# ---------------------------------------------------------------------------
# HTML digest
# ---------------------------------------------------------------------------
#
# Email clients (especially Gmail) strip <style> blocks, so all styling is
# inline. We keep the markup simple — tables for per-leg data, button-styled
# anchors for bookings, deduplicated CSS through a small set of helpers.

_CSS = {
    "body": "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
            "color:#222;background:#fafafa;margin:0;padding:16px;",
    "wrap": "max-width:720px;margin:0 auto;background:#fff;border:1px solid #e4e4e7;"
            "border-radius:8px;padding:20px;",
    "h1":   "font-size:18px;margin:0 0 4px 0;",
    "sub":  "color:#666;font-size:13px;margin:0 0 16px 0;",
    "h2":   "font-size:15px;margin:20px 0 8px 0;border-bottom:1px solid #eee;"
            "padding-bottom:6px;",
    "card": "border:1px solid #e4e4e7;border-radius:6px;padding:12px;"
            "margin-bottom:12px;background:#fcfcfd;",
    "leg_tbl": "width:100%;border-collapse:collapse;font-size:13px;",
    "leg_th":  "text-align:left;font-weight:600;color:#555;padding:4px 6px;"
               "border-bottom:1px solid #eee;",
    "leg_td":  "padding:4px 6px;vertical-align:top;",
    "totals": "font-size:14px;font-weight:600;margin-top:10px;",
    "sub_meta": "color:#666;font-size:12px;margin-top:2px;",
    "btn":   "display:inline-block;padding:6px 12px;margin:6px 6px 0 0;"
             "background:#0066cc;color:#fff;text-decoration:none;"
             "border-radius:4px;font-size:12px;",
    "btn_alt": "display:inline-block;padding:4px 9px;margin:3px 6px 3px 0;"
               "background:#f0f3f8;color:#0066cc;text-decoration:none;"
               "border-radius:4px;font-size:12px;border:1px solid #d4dce9;",
    "flag":  "color:#a54b00;font-size:12px;margin:4px 0;",
    "rank_tbl": "width:100%;border-collapse:collapse;font-size:13px;",
    "rank_th":  "text-align:left;font-weight:600;color:#555;padding:6px 8px;"
                "border-bottom:1px solid #eee;",
    "rank_td":  "padding:6px 8px;border-bottom:1px solid #f3f3f5;",
    "pts_box":  "background:#fff7e6;border:1px solid #f2d08b;border-radius:4px;"
                "padding:8px 10px;margin-top:10px;font-size:13px;",
}


def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _html_leg_row(i: int, l: dict) -> str:
    price = l.get("price_usd")
    price_s = f"${price:.0f}" if price else "—"
    timing = ""
    dep, arr = l.get("depart_time"), l.get("arrive_time")
    if dep and arr:
        timing = f"{_esc(dep)}→{_esc(arr)}"

    duration = l.get("duration_str") or ""
    stops_str = _stops_str(l)
    nums = _flight_numbers_str(l)
    carrier = _carrier_display(l)

    meta_parts = [p for p in [stops_str, nums] if p]
    meta = " · ".join(_esc(p) for p in meta_parts)

    return (
        f'<tr>'
        f'<td style="{_CSS["leg_td"]}"><strong>{i+1}</strong></td>'
        f'<td style="{_CSS["leg_td"]}">{_esc(l["origin"])} → {_esc(l["destination"])}'
        f'<div style="{_CSS["sub_meta"]}">{_esc(l["date"])} · {timing}</div></td>'
        f'<td style="{_CSS["leg_td"]}">{_esc(carrier)}'
        f'<div style="{_CSS["sub_meta"]}">{meta}</div></td>'
        f'<td style="{_CSS["leg_td"]};text-align:right;white-space:nowrap;">'
        f'<strong>{_esc(price_s)}</strong>'
        f'<div style="{_CSS["sub_meta"]}">{_esc(duration)}</div></td>'
        f'</tr>'
    )


def _html_combo_card(sc: ScoredCombo, prev_day_data: Optional[dict] = None) -> str:
    legs_rows = "".join(_html_leg_row(i, l) for i, l in enumerate(sc.legs_data))

    prev_price = None
    if prev_day_data and sc.combo.id in prev_day_data:
        prev_price = prev_day_data[sc.combo.id].get("total_cash_usd")
    delta = _delta_str(sc.total_cash_usd, prev_price)

    totals = (
        f'<div style="{_CSS["totals"]}">Total: ${sc.total_cash_usd:.0f}'
        f'{_esc(delta)} · {sc.total_travel_hours:.0f}h flight time'
        f'</div>'
    )

    stopover = (
        f'<div style="{_CSS["sub_meta"]}">'
        f'Stopover: {_esc(sc.combo.stopover_city)} · {sc.combo.stopover_days} days'
        f'</div>' if sc.combo.stopover_city else
        f'<div style="{_CSS["sub_meta"]}">Direct route</div>'
    )

    points_html = ""
    if sc.best_points_option:
        p = sc.best_points_option
        points_html = (
            f'<div style="{_CSS["pts_box"]}">'
            f'<strong>{_esc(p["program"].upper())}</strong>: '
            f'{p["total_points"]:,} pts + ${p["fees_usd"]:.0f} fees '
            f'= ${p["equiv_usd"]:.0f} equiv @ {p["cpp_used"]}¢/pt → '
            f'<strong>{_esc(sc.verdict)}</strong>'
            f'</div>'
        )

    flags_html = "".join(
        f'<div style="{_CSS["flag"]}">{_esc(f)}</div>' for f in (sc.combo.flags or [])
    )

    buttons = "".join(
        f'<a href="{_esc(bl["url"])}" style="{_CSS["btn"]}">Book leg {bl["leg"]}</a>'
        for bl in sc.booking_links
    )

    return (
        f'<div style="{_CSS["card"]}">'
        f'<table style="{_CSS["leg_tbl"]}"><thead><tr>'
        f'<th style="{_CSS["leg_th"]}">#</th>'
        f'<th style="{_CSS["leg_th"]}">Route</th>'
        f'<th style="{_CSS["leg_th"]}">Flight</th>'
        f'<th style="{_CSS["leg_th"]};text-align:right;">Price</th>'
        f'</tr></thead><tbody>{legs_rows}</tbody></table>'
        f'{totals}{stopover}{points_html}{flags_html}'
        f'<div>{buttons}</div>'
        f'</div>'
    )


def _html_rank_row(rank: int, sc: ScoredCombo) -> str:
    route = " → ".join([sc.legs_data[0]["origin"]]
                       + [l["destination"] for l in sc.legs_data])
    return (
        f'<tr>'
        f'<td style="{_CSS["rank_td"]};width:30px;">{rank}</td>'
        f'<td style="{_CSS["rank_td"]}">{_esc(route)}</td>'
        f'<td style="{_CSS["rank_td"]};text-align:right;">${sc.total_cash_usd:.0f}</td>'
        f'<td style="{_CSS["rank_td"]};text-align:right;">{sc.total_travel_hours:.0f}h</td>'
        f'<td style="{_CSS["rank_td"]}">{_esc(sc.verdict)}</td>'
        f'</tr>'
    )


def _html_manual_award_check(
    ranked: List[ScoredCombo], cpp_valuations: dict, top_n_combos: int = 3
) -> str:
    seen = set()
    sections: List[str] = []
    for sc in ranked[:top_n_combos]:
        for leg in sc.legs_data:
            key = (leg["origin"], leg["destination"], leg["date"])
            if key in seen:
                continue
            seen.add(key)
            if (leg.get("awards") or {}):
                continue
            links = build_award_links(
                leg["origin"], leg["destination"], leg["date"], cpp_valuations
            )
            if not links:
                continue
            buttons = "".join(
                f'<a href="{_esc(lk.url)}" style="{_CSS["btn_alt"]}">{_esc(lk.label)}</a>'
                for lk in links
            )
            sections.append(
                f'<div style="margin:10px 0;">'
                f'<strong>{_esc(leg["origin"])} → {_esc(leg["destination"])}</strong> · '
                f'{_esc(leg["date"])}<br>{buttons}</div>'
            )
    if not sections:
        return '<p style="color:#666;">(all legs have automated award data)</p>'
    return "".join(sections)


def _html_award_alerts(ranked: List[ScoredCombo]) -> str:
    items = []
    seen_programs = set()
    for sc in ranked:
        if sc.best_points_option and sc.best_points_option["program"] not in seen_programs:
            seen_programs.add(sc.best_points_option["program"])
            p = sc.best_points_option
            route = " → ".join([sc.legs_data[0]["origin"]]
                               + [l["destination"] for l in sc.legs_data])
            items.append(
                f'<li><strong>{_esc(p["program"].upper())}</strong> '
                f'on {_esc(route)}: '
                f'{p["total_points"]:,} pts + ${p["fees_usd"]:.0f}</li>'
            )
    if not items:
        return '<p style="color:#666;">No award space found in current results.</p>'
    return f'<ul style="padding-left:20px;">{"".join(items)}</ul>'


def format_digest_html(
    ranked_combos: List[ScoredCombo],
    run_date: date,
    trip_config: dict,
    prev_day_data: Optional[dict] = None,
) -> str:
    if not ranked_combos:
        return (
            f'<div style="{_CSS["body"]}"><div style="{_CSS["wrap"]}">'
            f'<p>No complete flight combinations found. '
            f'Check API credentials and retry.</p></div></div>'
        )

    top_cash = next((c for c in ranked_combos if c.verdict == "CASH WINS"), ranked_combos[0])
    top_points = next((c for c in ranked_combos if "POINTS" in c.verdict), None)
    top_direct = next((c for c in ranked_combos if c.combo.combo_type == "direct"), None)

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
        f'<h1 style="{_CSS["h1"]}">✈ Flight Digest — '
        f'{_esc(run_date.strftime("%a %b %d, %Y"))}</h1>'
        f'<p style="{_CSS["sub"]}">'
        f'{_esc(trip_config["origin"])} → Caribbean → GRU → SC/PR · '
        f'Window: {_esc(window["earliest_depart"])} – {_esc(window["latest_depart"])}'
        f'</p>'
    )

    def _section(title: str, body: str) -> str:
        return f'<h2 style="{_CSS["h2"]}">{_esc(title)}</h2>{body}'

    cash_html = _html_combo_card(top_cash, prev_day_data)
    points_html = (_html_combo_card(top_points, prev_day_data) if top_points
                   else '<p style="color:#666;">No award space found for this date range.</p>')
    direct_html = (_html_combo_card(top_direct, prev_day_data) if top_direct
                   else '<p style="color:#666;">No direct options in range.</p>')

    if best_per_stopover:
        stopover_html = "".join(
            f'<h3 style="font-size:14px;margin:14px 0 6px 0;">▸ Via {_esc(city)}</h3>'
            f'{_html_combo_card(best_per_stopover[city], prev_day_data)}'
            for city in stopover_candidates if city in best_per_stopover
        )
    else:
        stopover_html = '<p style="color:#666;">(no stopover candidates configured)</p>'

    rank_rows = "".join(
        _html_rank_row(i + 1, c) for i, c in enumerate(ranked_combos[:10])
    )
    ranking_html = (
        f'<table style="{_CSS["rank_tbl"]}"><thead><tr>'
        f'<th style="{_CSS["rank_th"]}">#</th>'
        f'<th style="{_CSS["rank_th"]}">Route</th>'
        f'<th style="{_CSS["rank_th"]};text-align:right;">Cash</th>'
        f'<th style="{_CSS["rank_th"]};text-align:right;">Time</th>'
        f'<th style="{_CSS["rank_th"]}">Verdict</th>'
        f'</tr></thead><tbody>{rank_rows}</tbody></table>'
        f'<p style="{_CSS["sub_meta"]}">Top 10 of {len(ranked_combos)} ranked combinations</p>'
    )

    cpp = trip_config.get("cpp_valuations", {})
    manual_html = _html_manual_award_check(ranked_combos, cpp)
    alerts_html = _html_award_alerts(ranked_combos)

    return (
        f'<div style="{_CSS["body"]}"><div style="{_CSS["wrap"]}">'
        f'{header}'
        f'{_section("🥇 Best Cash Combo", cash_html)}'
        f'{_section("🏆 Best Points Combo", points_html)}'
        f'{_section("✈ Best Direct (no stopover)", direct_html)}'
        f'{_section("🏝 Best Stopover Options", stopover_html)}'
        f'{_section("Full Ranking", ranking_html)}'
        f'{_section("Award Space Alerts", alerts_html)}'
        f'{_section("Manual Award Check", manual_html)}'
        f'</div></div>'
    )


def send_email(
    digest: str,
    recipient: str,
    subject: Optional[str] = None,
    html: Optional[str] = None,
) -> bool:
    """Send digest via SendGrid. Plain-text `digest` is the required fallback;
    `html` is rendered when the client supports it. Returns True on success.
    """
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
    kwargs = dict(
        from_email=from_email,
        to_emails=recipient,
        subject=subject,
        plain_text_content=digest,
    )
    if html:
        kwargs["html_content"] = html
    msg = Mail(**kwargs)
    try:
        client = SendGridAPIClient(api_key)
        resp = client.send(msg)
        log.info("SendGrid response status=%s", resp.status_code)
        return 200 <= resp.status_code < 300
    except Exception as e:
        log.error("SendGrid send failed: %s", e)
        return False
