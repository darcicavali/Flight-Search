"""Web UI for ad-hoc flight search — runs locally or on a host like Render.

Local:
    python -m tools.web
    # opens http://127.0.0.1:8000 in your browser

Hosted (Render etc.):
    gunicorn tools.web:app --bind 0.0.0.0:$PORT
    # see render.yaml for the full deploy config

Add legs (origin, destination, date) and submit. Calls the same LetsFG
fetcher the daily digest uses and renders the cheapest offer per leg.

Env vars honored at startup:
    LETSFG_BROWSERS       "0" to skip slow browser connectors (default).
    WEB_USERNAME          basic-auth username (default "user").
    WEB_PASSWORD          when set, every request requires basic auth.
    SEATS_AERO_API_KEY    enables the optional award-search checkbox.
"""

from __future__ import annotations

import os

# Skip LetsFG's browser-based connectors by default — they take 20–60s each,
# depend on a working Chrome/Xvfb, and rarely add unique offers. Users who
# want them can set LETSFG_BROWSERS=1 before launching.
os.environ.setdefault("LETSFG_BROWSERS", "0")

import argparse
import asyncio
import logging
import secrets
import threading
import time
import webbrowser
from datetime import date
from typing import List

import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, Response, render_template_string, request

from engine.routes import Leg
from fetchers.letsfg import fetch_cash_offers
from fetchers.seats_aero import fetch_award_fares
from output.airlines import airline_name

log = logging.getLogger("tools.web")

app = Flask(__name__)


# ── HTTP Basic auth for hosted deploys ────────────────────────────────────
# When WEB_PASSWORD env var is set (only on Render etc.), every request
# except /healthz prompts the browser for credentials. Username defaults
# to "user" and can be overridden with WEB_USERNAME.
def _auth_ok() -> bool:
    expected_pw = os.environ.get("WEB_PASSWORD") or ""
    if not expected_pw:
        return True  # no password configured = open access (local default)
    expected_user = os.environ.get("WEB_USERNAME", "user")
    auth = request.authorization
    if not auth or not auth.password:
        return False
    return (
        secrets.compare_digest(auth.username or "", expected_user)
        and secrets.compare_digest(auth.password, expected_pw)
    )


@app.before_request
def _require_auth():
    if request.path == "/healthz":
        return None
    if not _auth_ok():
        return Response(
            "Authentication required.\n",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Flight Search"'},
        )
    return None


@app.route("/healthz")
def healthz():
    """Render's load-balancer pings this to confirm the app is alive."""
    return "ok", 200, {"Content-Type": "text/plain"}


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Flight Search</title>
  <style>
    :root { --bd: #d0d7de; --bg: #f6f8fa; --ink: #1f2328; --accent: #0969da; }
    * { box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
           max-width: 780px; margin: 2rem auto; padding: 0 1rem; color: var(--ink); }
    h1 { margin: 0 0 .25rem; }
    p.sub { margin: 0 0 1.5rem; color: #656d76; }
    .leg-row { display: grid; grid-template-columns: 90px 20px 90px 1fr 36px;
               gap: .5rem; align-items: center; margin: .4rem 0; }
    .leg-row input { padding: .45rem .6rem; border: 1px solid var(--bd);
                     border-radius: 6px; font: inherit; width: 100%; }
    .leg-row input[name="origin"], .leg-row input[name="destination"] {
      text-transform: uppercase; letter-spacing: .08em; font-weight: 600;
    }
    .arrow { text-align: center; color: #656d76; }
    .remove-btn { background: none; border: 1px solid var(--bd); border-radius: 6px;
                  cursor: pointer; padding: .3rem; color: #cf222e; font-size: 1rem; }
    .remove-btn:hover { background: #ffebe9; }
    .controls { display: flex; gap: .5rem; margin-top: 1rem; align-items: center; }
    .add-btn { background: var(--bg); border: 1px solid var(--bd); border-radius: 6px;
               padding: .5rem .9rem; cursor: pointer; font: inherit; }
    .add-btn:hover { background: #eaeef2; }
    .submit-btn { background: var(--accent); color: white; border: 0; border-radius: 6px;
                  padding: .55rem 1.2rem; font: inherit; font-weight: 600; cursor: pointer;
                  margin-left: auto; }
    .submit-btn:hover { background: #0860c7; }
    .submit-btn:disabled { opacity: .55; cursor: wait; }
    label.opts { display: flex; align-items: center; gap: .4rem; font-size: .9rem;
                 color: #656d76; margin-top: .9rem; }
    label.opts input { margin: 0; }
    .hint { margin-top: 2rem; padding: .9rem 1rem; background: var(--bg);
            border-left: 4px solid var(--accent); font-size: .9rem; }
    .hint code { background: #eaeef2; padding: 1px 5px; border-radius: 3px; }
  </style>
</head>
<body>
  <h1>✈ Flight Search</h1>
  <p class="sub">Ad-hoc lookup. Uses the same price sources as the daily digest.</p>

  <form method="POST" action="/search" id="search-form">
    <div id="legs">
      <div class="leg-row">
        <input name="origin" placeholder="ORD" maxlength="3" required pattern="[A-Za-z]{3}">
        <span class="arrow">→</span>
        <input name="destination" placeholder="GRU" maxlength="3" required pattern="[A-Za-z]{3}">
        <input type="date" name="date" required>
        <span></span>
      </div>
    </div>

    <div class="controls">
      <button type="button" class="add-btn" onclick="addLeg()">+ Add leg</button>
      <label class="opts">
        <input type="checkbox" name="award" value="1"> Include award search (seats.aero)
      </label>
      <button type="submit" class="submit-btn" id="submit-btn">Search</button>
    </div>
  </form>

  <div class="hint">
    <strong>Tip:</strong> enter airport codes in IATA format (e.g. <code>ORD</code>, <code>GRU</code>, <code>FLN</code>).
    Each leg is searched independently — pick real dates for each. Lookup usually takes 5–15s per leg.
  </div>

  <script>
    function addLeg() {
      const div = document.createElement('div');
      div.className = 'leg-row';
      div.innerHTML = `
        <input name="origin" placeholder="XXX" maxlength="3" required pattern="[A-Za-z]{3}">
        <span class="arrow">→</span>
        <input name="destination" placeholder="YYY" maxlength="3" required pattern="[A-Za-z]{3}">
        <input type="date" name="date" required>
        <button type="button" class="remove-btn" onclick="this.parentElement.remove()">×</button>
      `;
      document.getElementById('legs').appendChild(div);
      div.querySelector('input[name="origin"]').focus();
    }
    document.getElementById('search-form').addEventListener('submit', () => {
      const btn = document.getElementById('submit-btn');
      btn.disabled = true;
      btn.textContent = 'Searching…';
    });
  </script>
</body>
</html>
"""


RESULTS_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Flight Search — Results</title>
  <style>
    :root { --bd: #d0d7de; --bg: #f6f8fa; --ink: #1f2328; --accent: #0969da;
            --green: #1a7f37; --green-bg: #dafbe1; --red: #cf222e; --amber: #fff8c5; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
           max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: var(--ink); }
    h1 { margin: 0 0 .25rem; }
    a.back { color: var(--accent); text-decoration: none; font-size: .9rem; }
    a.back:hover { text-decoration: underline; }

    .leg { margin: 1.6rem 0 1.2rem; border: 1px solid var(--bd); border-radius: 8px;
           overflow: hidden; }
    .leg-head { padding: .9rem 1.1rem; background: var(--bg); border-bottom: 1px solid var(--bd);
                display: flex; flex-wrap: wrap; align-items: baseline; gap: .5rem 1rem; }
    .leg-head .route { font-size: 1.15rem; font-weight: 600; }
    .leg-head .date { color: #656d76; }
    .leg-head .cheapest-badge { margin-left: auto; background: var(--green-bg);
           color: var(--green); padding: .2rem .6rem; border-radius: 4px;
           font-size: .85rem; font-weight: 600; white-space: nowrap; }
    .leg-head .count { color: #656d76; font-size: .85rem; }

    table { border-collapse: collapse; width: 100%; }
    th, td { padding: .5rem .75rem; text-align: left; border-bottom: 1px solid var(--bd);
             vertical-align: middle; font-size: .92rem; }
    thead th { background: white; font-weight: 600; font-size: .8rem;
               color: #656d76; text-transform: uppercase; letter-spacing: .04em;
               border-bottom: 2px solid var(--bd); }
    tr.cheapest td { background: color-mix(in srgb, var(--green-bg) 60%, white); }
    tr.cheapest td:first-child::before { content: "⭐ "; }
    tbody tr:last-child td { border-bottom: 0; }

    td.price { font-weight: 600; text-align: right; white-space: nowrap; }
    td .muted { color: #656d76; font-size: .82rem; display: block; }
    .nonstop { color: var(--green); font-weight: 500; }
    .book { background: var(--accent); color: white; padding: .32rem .7rem;
            border-radius: 5px; text-decoration: none; font-size: .85rem;
            white-space: nowrap; display: inline-block; }
    .book:hover { background: #0860c7; }

    .no-offers { padding: 1rem 1.2rem; color: var(--red); font-style: italic;
                 background: white; }
    .awards-bar { padding: .55rem 1.1rem; background: #f0fff6;
                  border-bottom: 1px solid var(--bd); }
    .award { display: inline-block; background: var(--green-bg); color: var(--green);
             border-radius: 4px; padding: 2px 7px; margin: 2px 5px 2px 0;
             font-size: .8rem; font-weight: 500; }
    .note { display: inline-block; background: var(--amber); color: #6f5500;
            border-radius: 4px; padding: 1px 7px; margin-left: .3rem;
            font-size: .72rem; font-weight: 600; }

    .totals { background: var(--bg); border-radius: 8px; padding: 1rem 1.25rem;
              margin: 1.5rem 0; display: grid; grid-template-columns: 1fr 1fr 1fr;
              gap: 1rem; }
    .totals .label { color: #656d76; font-size: .85rem; }
    .totals .value { font-size: 1.3rem; font-weight: 600; }
  </style>
</head>
<body>
  <a class="back" href="/">← New search</a>
  <h1>Results</h1>

  <div class="totals">
    <div>
      <div class="label">Cheapest-combo total</div>
      <div class="value">${{ "{:,.2f}".format(total_cash) }}</div>
    </div>
    <div>
      <div class="label">Legs priced</div>
      <div class="value">{{ priced_count }}/{{ total_count }}</div>
    </div>
    <div>
      <div class="label">Total flight time</div>
      <div class="value">{{ total_duration }}</div>
    </div>
  </div>

  {% for leg in legs %}
  <div class="leg">
    <div class="leg-head">
      <span class="route">{{ leg.origin }} → {{ leg.destination }}</span>
      <span class="date">{{ leg.date }}</span>
      {% if leg.source_note %}<span class="note">{{ leg.source_note }}</span>{% endif %}
      <span class="count">{{ leg.offer_count }} option{{ '' if leg.offer_count == 1 else 's' }}</span>
      {% if leg.cheapest_price is not none %}
        <span class="cheapest-badge">from ${{ "{:,.2f}".format(leg.cheapest_price) }}</span>
      {% endif %}
    </div>

    {% if leg.awards %}
    <div class="awards-bar">
      {% for prog, a in leg.awards.items() %}
        <span class="award">{{ prog }}: {{ "{:,}".format(a.miles) }} mi + ${{ "%.0f"|format(a.taxes_usd) }}</span>
      {% endfor %}
    </div>
    {% endif %}

    {% if leg.offers %}
      <table>
        <thead>
          <tr>
            <th>Airline</th>
            <th>Flight</th>
            <th>Stops</th>
            <th>Times</th>
            <th>Duration</th>
            <th class="price">Price</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {% for o in leg.offers %}
          <tr {% if loop.first %}class="cheapest"{% endif %}>
            <td>{{ o.airline_name or o.airline_code or '—' }}</td>
            <td>{{ o.flight_nos or '—' }}</td>
            <td>
              {% if o.stops == 0 %}<span class="nonstop">nonstop</span>
              {% else %}{{ o.stops }} stop{{ '' if o.stops == 1 else 's' }}
                {% if o.layovers %}<span class="muted">via {{ o.layovers|join(', ') }}</span>{% endif %}
              {% endif %}
            </td>
            <td>
              {% if o.depart_time or o.arrive_time %}{{ o.depart_time or '?' }} → {{ o.arrive_time or '?' }}
              {% else %}—{% endif %}
            </td>
            <td>{{ o.duration_str or '—' }}</td>
            <td class="price">${{ "{:,.2f}".format(o.price_usd) }}</td>
            <td>{% if o.booking_url %}<a class="book" href="{{ o.booking_url }}" target="_blank" rel="noopener">Book</a>{% endif %}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <div class="no-offers">No offers found. Try a nearby date or different airport pair.</div>
    {% endif %}
  </div>
  {% endfor %}

  <a class="back" href="/">← New search</a>
</body>
</html>
"""


def _segments_summary(segments: list) -> str:
    """One-line flight-number summary, e.g. 'UA823 · AV9445'.

    LetsFG sometimes stores flight_no already prefixed with the carrier code
    (e.g. 'CM822'); other connectors return a bare number. Prefer flight_no
    when it already starts with letters, otherwise glue carrier+flight_no.
    """
    out = []
    for s in (segments or []):
        carrier = (s.get("carrier") or "").strip()
        fn = (s.get("flight_no") or "").strip()
        if not fn and not carrier:
            continue
        if fn and fn[:1].isalpha():
            out.append(fn)
        else:
            out.append(f"{carrier}{fn}")
    return " · ".join(out)


def _format_duration_total(hours_sum: float) -> str:
    if hours_sum <= 0:
        return "—"
    h = int(hours_sum)
    m = int(round((hours_sum - h) * 60))
    return f"{h}h{m:02d}m"


def _empty_keys(offers_by_leg: dict, legs: List[Leg]) -> List[Leg]:
    """Legs whose current offer list is empty."""
    return [leg for leg in legs if not offers_by_leg.get(leg.key)]


async def _retry_empty(legs: List[Leg], offers_by_leg: dict,
                       source_notes: dict, session: aiohttp.ClientSession,
                       delay_sec: float = 3.0) -> int:
    """Retry empty legs once after a short delay. Mutates dicts in place."""
    empty = _empty_keys(offers_by_leg, legs)
    if not empty:
        return 0
    log.info("retry: %d empty legs after %.1fs delay", len(empty), delay_sec)
    await asyncio.sleep(delay_sec)
    retry = await fetch_cash_offers(empty, {}, session=session)
    recovered = 0
    for leg in empty:
        new_offers = retry.get(leg.key) or []
        if new_offers:
            offers_by_leg[leg.key] = new_offers
            source_notes[leg.key] = "retry"
            recovered += 1
    return recovered


async def _browser_fallback(legs: List[Leg], offers_by_leg: dict,
                            source_notes: dict,
                            session: aiohttp.ClientSession) -> int:
    """Re-run still-empty legs with browser connectors enabled.

    Patches letsfg's cached _BROWSERS_AVAILABLE on for the call duration,
    then restores. Covers carriers/OTAs the API-only pool misses.
    """
    empty = _empty_keys(offers_by_leg, legs)
    if not empty:
        return 0
    from letsfg.connectors import engine as _eng
    if _eng._BROWSERS_AVAILABLE:
        return 0
    log.info("browser-fallback: %d legs still empty, escalating to full mode", len(empty))
    fallback_cfg = {
        "fetchers": {"letsfg": {
            "mode": None, "timeout_sec": 90, "concurrency": 1, "max_browsers": 2,
        }}
    }
    _eng._BROWSERS_AVAILABLE = True
    try:
        result = await fetch_cash_offers(empty, fallback_cfg, session=session)
    finally:
        _eng._BROWSERS_AVAILABLE = False
    recovered = 0
    for leg in empty:
        new_offers = result.get(leg.key) or []
        if new_offers:
            offers_by_leg[leg.key] = new_offers
            source_notes[leg.key] = "full mode"
            recovered += 1
    return recovered


async def _run_search(legs: List[Leg], include_award: bool) -> dict:
    """Fetch all cash offers per leg (+ optional award), with recovery.

    Returns {'offers_by_leg': {key: [offer, ...]}, 'awards': {key: {...}},
             'source_notes': {key: 'retry'|'full mode'}}.
    """
    source_notes: dict = {}
    async with aiohttp.ClientSession() as session:
        cash_task = fetch_cash_offers(legs, {}, session=session)
        if include_award:
            award_task = fetch_award_fares(legs, {}, session=session)
            offers_by_leg, award = await asyncio.gather(cash_task, award_task)
        else:
            offers_by_leg = await cash_task
            award = {}

        await _retry_empty(legs, offers_by_leg, source_notes, session)
        await _browser_fallback(legs, offers_by_leg, source_notes, session)

    return {
        "offers_by_leg": offers_by_leg,
        "awards": {k: (v or {}).get("awards") or {} for k, v in (award or {}).items()},
        "source_notes": source_notes,
    }


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/search", methods=["POST"])
def search():
    origins = [x.strip().upper() for x in request.form.getlist("origin")]
    destinations = [x.strip().upper() for x in request.form.getlist("destination")]
    dates = [x.strip() for x in request.form.getlist("date")]
    include_award = bool(request.form.get("award"))

    legs: List[Leg] = []
    for i, (o, d, dt) in enumerate(zip(origins, destinations, dates), start=1):
        if not (o and d and dt):
            continue
        try:
            legs.append(Leg(o, d, date.fromisoformat(dt), i))
        except ValueError:
            continue

    if not legs:
        return "No valid legs submitted. <a href='/'>Back</a>", 400

    t0 = time.perf_counter()
    results = asyncio.run(_run_search(legs, include_award))
    log.info("search: %d legs in %.1fs (award=%s)",
             len(legs), time.perf_counter() - t0, include_award)

    offers_by_leg = results["offers_by_leg"]
    awards_by_leg = results["awards"]
    source_notes = results["source_notes"]

    leg_rows = []
    total_cash = 0.0
    total_hours = 0.0
    priced = 0
    for leg in legs:
        offers = offers_by_leg.get(leg.key) or []
        shaped = [{
            "airline_name": airline_name(o.get("airline") or ""),
            "airline_code": o.get("airline") or "",
            "duration_str": o.get("duration_str"),
            "depart_time": o.get("depart_time"),
            "arrive_time": o.get("arrive_time"),
            "layovers": o.get("layovers") or [],
            "flight_nos": _segments_summary(o.get("segments") or []),
            "booking_url": o.get("booking_url"),
            "price_usd": o.get("price_usd"),
            "stops": o.get("stops") or 0,
        } for o in offers]

        cheapest = shaped[0] if shaped else None
        leg_rows.append({
            "origin": leg.origin,
            "destination": leg.destination,
            "date": leg.date.isoformat(),
            "offers": shaped,
            "awards": awards_by_leg.get(leg.key) or {},
            "source_note": source_notes.get(leg.key),
            "cheapest_price": cheapest["price_usd"] if cheapest else None,
            "offer_count": len(shaped),
        })
        if cheapest:
            total_cash += cheapest["price_usd"]
            priced += 1
            if offers[0].get("duration_hours"):
                total_hours += offers[0]["duration_hours"]

    return render_template_string(
        RESULTS_HTML,
        legs=leg_rows,
        total_cash=total_cash,
        total_duration=_format_duration_total(total_hours),
        priced_count=priced,
        total_count=len(legs),
    )


def main():
    parser = argparse.ArgumentParser(description="Local flight search web UI")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't auto-open the browser")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    url = f"http://{args.host}:{args.port}"
    print(f"\n  ✈  Flight Search UI running at {url}\n")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
