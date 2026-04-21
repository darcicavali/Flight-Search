import json
import sys
from datetime import date
from types import ModuleType, SimpleNamespace

from engine.routes import Combo, Leg
from engine.scorer import score_combo
from output import sheets


CONFIG = {
    "scoring_weights": {"total_cost": 0.5, "total_time": 0.3, "connection_quality": 0.2},
    "cpp_valuations": {"smiles": 1.3},
    "constraints": {"baggage_checked_bags": 0},
}


def _scored_combo():
    combo = Combo(legs=[Leg("ORD", "GRU", date(2026, 7, 26), 1)], combo_type="through_direct")
    legs_data = [{
        "origin": "ORD",
        "destination": "GRU",
        "price_usd": 500,
        "duration_hours": 10,
        "awards": {"smiles": {"points": 20000, "fees_usd": 30}},
        "booking_url": "https://example.com/book",
    }]
    return score_combo(combo, legs_data, CONFIG)


def test_load_previous_day_without_local_history_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sheets, "LOCAL_HISTORY", tmp_path / "missing.json")
    assert sheets.load_previous_day() is None


def test_append_to_sheets_without_credentials_returns_false_and_no_raise(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sheets, "LOCAL_HISTORY", tmp_path / "price_history.json")
    monkeypatch.delenv("GOOGLE_SHEETS_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)

    with caplog.at_level("INFO"):
        out = sheets.append_to_sheets([_scored_combo()], {}, run_date=date(2026, 7, 26))

    assert out is False
    assert "Google Sheets not configured" in caplog.text


def test_append_to_sheets_writes_expected_columns_with_fake_gspread(tmp_path, monkeypatch):
    monkeypatch.setattr(sheets, "LOCAL_HISTORY", tmp_path / "price_history.json")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS", json.dumps({"type": "service_account"}))
    monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "sheet-123")

    captured = {"rows": None}

    class FakeWorksheet:
        def append_row(self, row):
            pass

        def append_rows(self, rows, value_input_option="RAW"):
            captured["rows"] = rows

    class FakeSpreadsheet:
        def worksheet(self, name):
            return FakeWorksheet()

    class FakeClient:
        def open_by_key(self, key):
            assert key == "sheet-123"
            return FakeSpreadsheet()

    fake_gspread = ModuleType("gspread")
    fake_gspread.WorksheetNotFound = Exception
    fake_gspread.authorize = lambda creds: FakeClient()

    class FakeCreds:
        @classmethod
        def from_service_account_info(cls, info, scopes):
            return object()

    fake_service_account = ModuleType("google.oauth2.service_account")
    fake_service_account.Credentials = FakeCreds

    monkeypatch.setitem(sys.modules, "gspread", fake_gspread)
    monkeypatch.setitem(sys.modules, "google", ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", ModuleType("google.oauth2"))
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", fake_service_account)

    scored = [_scored_combo()]
    ok = sheets.append_to_sheets(scored, {}, run_date=date(2026, 7, 26))

    assert ok is True
    row = captured["rows"][0]
    assert row[1] == scored[0].combo.id
    assert row[2] == "ORD → GRU"
    assert row[6] == scored[0].total_cash_usd
    assert row[8] == scored[0].best_points_option["total_points"]
    assert row[11] == scored[0].verdict
    assert row[12] == 1
