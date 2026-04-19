"""Price history storage — Google Sheets primary, JSON file fallback."""

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import List, Optional

from engine.scorer import ScoredCombo

log = logging.getLogger(__name__)

HEADER = [
    "date", "combo_id", "route", "combo_type", "stopover_city", "stopover_days",
    "cash_total_usd", "best_points_program", "points_total", "fees_usd",
    "equiv_usd", "verdict", "rank",
]

LOCAL_HISTORY = Path("data/price_history.json")


def _route_str(sc: ScoredCombo) -> str:
    return " → ".join([sc.legs_data[0]["origin"]]
                      + [l["destination"] for l in sc.legs_data])


def _rows(scored: List[ScoredCombo], run_date: date) -> List[list]:
    rows = []
    for i, sc in enumerate(scored):
        p = sc.best_points_option or {}
        rows.append([
            run_date.isoformat(),
            sc.combo.id,
            _route_str(sc),
            sc.combo.combo_type,
            sc.combo.stopover_city or "",
            sc.combo.stopover_days or "",
            sc.total_cash_usd,
            p.get("program", ""),
            p.get("total_points", ""),
            p.get("fees_usd", ""),
            p.get("equiv_usd", ""),
            sc.verdict,
            i + 1,
        ])
    return rows


def _append_to_local_json(scored: List[ScoredCombo], run_date: date) -> None:
    LOCAL_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if LOCAL_HISTORY.exists():
        try:
            existing = json.loads(LOCAL_HISTORY.read_text())
        except Exception:
            existing = []
    snapshot = {
        "date": run_date.isoformat(),
        "combos": [
            {
                "combo_id": sc.combo.id,
                "route": _route_str(sc),
                "total_cash_usd": sc.total_cash_usd,
                "best_points_option": sc.best_points_option,
                "total_travel_hours": sc.total_travel_hours,
                "verdict": sc.verdict,
                "rank": i + 1,
            }
            for i, sc in enumerate(scored)
        ],
    }
    existing.append(snapshot)
    LOCAL_HISTORY.write_text(json.dumps(existing, indent=2))


def append_to_sheets(scored: List[ScoredCombo], config: dict, run_date: Optional[date] = None) -> bool:
    """Append today's rankings to Google Sheets, fall back to local JSON on failure."""
    run_date = run_date or date.today()
    # Always write local JSON — cheap, reliable, versioned by git.
    _append_to_local_json(scored, run_date)

    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    sheet_id = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")
    if not creds_json or not sheet_id:
        log.info("Google Sheets not configured; local JSON history written.")
        return False

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        info = json.loads(creds_json)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_key(sheet_id)
        try:
            ws = sh.worksheet("price_history")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="price_history", rows=1000, cols=len(HEADER))
            ws.append_row(HEADER)

        rows = _rows(scored, run_date)
        if rows:
            ws.append_rows(rows, value_input_option="RAW")
        return True
    except Exception as e:
        log.warning("Google Sheets append failed: %s (local JSON still written)", e)
        return False


def load_previous_day() -> Optional[dict]:
    """Return the most recent prior snapshot as {combo_id: combo_dict} or None."""
    if not LOCAL_HISTORY.exists():
        return None
    try:
        snapshots = json.loads(LOCAL_HISTORY.read_text())
    except Exception:
        return None
    if len(snapshots) < 2:
        return None
    prev = snapshots[-2]  # the snapshot before today's run
    return {c["combo_id"]: c for c in prev.get("combos", [])}
