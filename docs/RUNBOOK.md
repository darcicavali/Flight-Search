# Operations Runbook

## I didn't get an email today

Check in this order:

1. **GitHub Actions run exists**
   - Open **Actions** tab in GitHub.
   - Open workflow **Daily Flight Digest**.
   - Confirm today's scheduled run exists and started.

2. **Run log completed the search**
   - In the run logs, open job `run-digest`.
   - Confirm `Run flight search` executed `python main.py --trip chicago_sao_paulo_jul2026`.

3. **LetsFG leg coverage is non-zero**
   - In logs, find lines from `main`:
     - `[letsfg] X/Y legs priced`
     - `Merged cash coverage: X/Y legs priced across all sources`
   - If `X=0`, email may still send but digest will have no ranked combos.

4. **SendGrid delivery step**
   - In logs, find either:
     - `SendGrid response status=...` (delivery attempted), or
     - `SendGrid not configured ... Skipping send.`
   - If delivery attempted but no inbox email, check SendGrid Activity for the recipient.

## Prices look wrong or missing

1. Rerun locally in dry-run mode:

```bash
python main.py --trip chicago_sao_paulo_jul2026 --dry-run
```

2. Inspect coverage and errors in local logs:
   - `[letsfg] X/Y legs priced`
   - `[letsfg] sample error -> ...`
   - `Scored N combos (M skipped: missing leg fares)`

3. Inspect one leg via targeted test run (fastest path):

```bash
pytest tests/test_letsfg.py -q
```

4. If only one route/date looks wrong, re-run dry-run and search stdout for that leg key format:
   - `ORG-DST-YYYY-MM-DD`

## Playwright install failed on CI

Install happens in workflow step:

```bash
python -m playwright install --with-deps chromium
```

Common causes:

- GitHub transient apt/network failure while downloading browser dependencies.
- Upstream Playwright/CDN outage.
- Version skew between `playwright` in `requirements.txt` and cached environment.

Actions:

1. Re-run the failed job once from Actions UI.
2. If repeated, clear/update dependency pins and rerun.
3. Confirm the failing step is **Install Playwright browsers (required by LetsFG)**, not app code.

## How to trigger a manual run

1. Open **Actions** tab.
2. Select workflow **Daily Flight Digest**.
3. Click **Run workflow**.
4. Choose the branch and run.

This uses `workflow_dispatch` defined in `.github/workflows/daily_run.yml`.

## Secrets list (names only)

From `.github/workflows/daily_run.yml`:

- `SEATS_AERO_API_KEY`
- `SENDGRID_API_KEY`
- `SENDGRID_FROM_EMAIL`
- `RECIPIENT_EMAIL`
- `GOOGLE_SHEETS_CREDENTIALS`
- `GOOGLE_SHEETS_SPREADSHEET_ID`
