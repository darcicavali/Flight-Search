"""Local web UI for ad-hoc flight search.

Run with:
    python -m tools.web

Opens http://127.0.0.1:8000 in your browser. Add legs (origin, destination,
date) and submit. Calls the same LetsFG fetcher the daily digest uses and
renders the cheapest offer per leg, plus totals.

Optional: tick "Include award search" to also call seats.aero (requires
SEATS_AERO_API_KEY in env or .env file).
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

from flask import Flask, render_template_string, request

from engine.routes import Leg
from fetchers.letsfg import fetch_cash_fares
from fetchers.seats_aero import fetch_award_fares
from output.airlines import airline_name

log = logging.getLogger("tools.web")

app = Flask(__name__)


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
            --green: #1a7f37; --red: #cf222e; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
           max-width: 980px; margin: 2rem auto; padding: 0 1rem; color: var(--ink); }
    h1 { margin: 0 0 .25rem; } h2 { margin-top: 2rem; }
    a.back { color: var(--accent); text-decoration: none; font-size: .9rem; }
    a.back:hover { text-decoration: underline; }
    table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
    th, td { padding: .55rem .7rem; text-align: left; border-bottom: 1px solid var(--bd);
             vertical-align: top; }
    th { background: var(--bg); font-weight: 600; font-size: .9rem; color: #656d76; }
    td .muted { color: #656d76; font-size: .85rem; display: block; }
    td.price { font-weight: 600; text-align: right; white-space: nowrap; }
    td.miss { color: var(--red); font-style: italic; }
    .totals { background: var(--bg); border-radius: 8px; padding: 1rem 1.25rem;
              margin: 1.5rem 0; display: grid; grid-template-columns: 1fr 1fr 1fr;
              gap: 1rem; }
    .totals div { }
    .totals .label { color: #656d76; font-size: .85rem; }
    .totals .value { font-size: 1.3rem; font-weight: 600; }
    .book { background: var(--accent); color: white; padding: .35rem .7rem;
            border-radius: 5px; text-decoration: none; font-size: .85rem; white-space: nowrap; }
    .book:hover { background: #0860c7; }
    .awards { margin-top: .4rem; }
    .award { display: inline-block; background: #dafbe1; color: var(--green);
             border-radius: 4px; padding: 2px 6px; margin: 2px 4px 2px 0; font-size: .8rem; }
    .note { display: inline-block; background: #fff8c5; color: #6f5500;
            border-radius: 4px; padding: 1px 5px; margin-left: .35rem; font-size: .72rem; }
    .segs { color: #656d76; font-size: .82rem; margin-top: .25rem; }
  </style>
</head>
<body>
  <a class="back" href="/">← New search</a>
  <h1>Results</h1>

  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Route</th>
        <th>Date</th>
        <th>Airline</th>
        <th>Flight</th>
        <th>Duration</th>
        <th class="price">Cash (USD)</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr>
        <td>{{ loop.index }}</td>
        <td><strong>{{ row.origin }} → {{ row.destination }}</strong>
            {% if row.source_note %}<span class="note">{{ row.source_note }}</span>{% endif %}
            {% if row.layovers %}<span class="muted">via {{ row.layovers|join(', ') }}</span>{% endif %}
            {% if row.awards %}
              <div class="awards">
                {% for prog, a in row.awards.items() %}
                  <span class="award">{{ prog }}: {{ "{:,}".format(a.miles) }} mi + ${{ "%.0f"|format(a.taxes_usd) }}</span>
                {% endfor %}
              </div>
            {% endif %}
        </td>
        <td>{{ row.date }}
          {% if row.depart_time or row.arrive_time %}
            <span class="muted">{{ row.depart_time or '?' }} → {{ row.arrive_time or '?' }}</span>
          {% endif %}
        </td>
        <td>{{ row.airline_name or '—' }}</td>
        <td>{{ row.flight_nos or '—' }}</td>
        <td>{{ row.duration_str or '—' }}</td>
        {% if row.price_usd is not none %}
          <td class="price">${{ "{:,.2f}".format(row.price_usd) }}</td>
          <td>{% if row.booking_url %}<a class="book" href="{{ row.booking_url }}" target="_blank" rel="noopener">Book</a>{% endif %}</td>
        {% else %}
          <td class="price miss" colspan="2">{{ row.error or 'no offer' }}</td>
        {% endif %}
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <div class="totals">
    <div>
      <div class="label">Total cash</div>
      <div class="value">${{ "{:,.2f}".format(total_cash) }}</div>
    </div>
    <div>
      <div class="label">Total flight time</div>
      <div class="value">{{ total_duration }}</div>
    </div>
    <div>
      <div class="label">Legs priced</div>
      <div class="value">{{ priced_count }}/{{ total_count }}</div>
    </div>
  </div>

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


def _failed_legs(cash: dict, legs: List[Leg]) -> List[Leg]:
    """Legs that returned no priced offer in the most recent fetch."""
    return [
        leg for leg in legs
        if (cash.get(leg.key) or {}).get("price_usd") is None
    ]


async def _retry_failed(legs: List[Leg], cash: dict, session: aiohttp.ClientSession,
                        delay_sec: float = 3.0) -> int:
    """Retry empty legs once after a short delay. Mutates `cash` in place."""
    failed = _failed_legs(cash, legs)
    if not failed:
        return 0
    log.info("retry: %d empty legs after %.1fs delay", len(failed), delay_sec)
    await asyncio.sleep(delay_sec)
    retry = await fetch_cash_fares(failed, {}, session=session)
    recovered = 0
    for leg in failed:
        new_row = retry.get(leg.key) or {}
        if new_row.get("price_usd") is not None:
            new_row["source_note"] = "retry"
            cash[leg.key] = new_row
            recovered += 1
    return recovered


async def _browser_fallback(legs: List[Leg], cash: dict,
                            session: aiohttp.ClientSession) -> int:
    """For legs still without a price, re-run with browser connectors enabled.

    Patches letsfg's cached _BROWSERS_AVAILABLE flag on for the duration of
    the call, then restores it. ~3–5x slower per leg but covers routes the
    API-only pool misses (smaller domestic carriers, specific OTAs).
    """
    failed = _failed_legs(cash, legs)
    if not failed:
        return 0
    from letsfg.connectors import engine as _eng
    if _eng._BROWSERS_AVAILABLE:
        return 0  # browsers already on, nothing to escalate to
    log.info("browser-fallback: %d legs still empty, escalating to full mode", len(failed))
    fallback_cfg = {
        "fetchers": {"letsfg": {
            "mode": None,            # full connector set
            "timeout_sec": 90,       # browsers need more time
            "concurrency": 1,        # one at a time to avoid resource thrash
            "max_browsers": 2,
        }}
    }
    _eng._BROWSERS_AVAILABLE = True
    try:
        result = await fetch_cash_fares(failed, fallback_cfg, session=session)
    finally:
        _eng._BROWSERS_AVAILABLE = False
    recovered = 0
    for leg in failed:
        new_row = result.get(leg.key) or {}
        if new_row.get("price_usd") is not None:
            new_row["source_note"] = "full mode"
            cash[leg.key] = new_row
            recovered += 1
    return recovered


async def _run_search(legs: List[Leg], include_award: bool) -> dict:
    """Fetch cash (+ optional award) for each leg, with retry + browser fallback."""
    async with aiohttp.ClientSession() as session:
        cash_task = fetch_cash_fares(legs, {}, session=session)
        if include_award:
            award_task = fetch_award_fares(legs, {}, session=session)
            cash, award = await asyncio.gather(cash_task, award_task)
        else:
            cash = await cash_task
            award = {}

        # Empty-result recovery: cheap retry, then expensive browser fallback.
        await _retry_failed(legs, cash, session)
        await _browser_fallback(legs, cash, session)

    merged = {}
    for leg in legs:
        c = cash.get(leg.key) or {}
        a = (award.get(leg.key) or {}).get("awards") or {}
        row = dict(c)
        row["awards"] = a
        merged[leg.key] = row
    return merged


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

    rows = []
    total_cash = 0.0
    total_hours = 0.0
    priced = 0
    for leg in legs:
        r = results.get(leg.key) or {}
        row = {
            "origin": leg.origin,
            "destination": leg.destination,
            "date": leg.date.isoformat(),
            "price_usd": r.get("price_usd"),
            "airline_name": airline_name(r.get("airline") or ""),
            "duration_str": r.get("duration_str"),
            "depart_time": r.get("depart_time"),
            "arrive_time": r.get("arrive_time"),
            "layovers": r.get("layovers") or [],
            "segments_summary": _segments_summary(r.get("segments") or []),
            "flight_nos": _segments_summary(r.get("segments") or []),
            "booking_url": r.get("booking_url"),
            "awards": r.get("awards") or {},
            "source_note": r.get("source_note"),
            "error": r.get("error"),
        }
        rows.append(row)
        if row["price_usd"] is not None:
            total_cash += row["price_usd"]
            priced += 1
        if r.get("duration_hours"):
            total_hours += r["duration_hours"]

    return render_template_string(
        RESULTS_HTML,
        rows=rows,
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
