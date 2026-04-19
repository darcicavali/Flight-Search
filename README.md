# Flight Search & Ranking System

A daily automated flight comparison system that runs every morning and delivers a ranked digest
of all flight combinations for a given trip — comparing cash fares vs award/points fares across
multiple legs, including international and Brazilian domestic segments.

## Phase 1

Hardcoded for a single trip: **ORD → Caribbean stopover → GRU → SC/PR domestic** (late July 2026).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys
python main.py --trip chicago_sao_paulo_jul2026 --dry-run
```

`--dry-run` prints the digest to stdout instead of sending email.

## Architecture

```
config/trips.yaml        # trip definitions, point balances, weights
fetchers/                # API clients (Kiwi, Amadeus, Seats.aero, Smiles)
engine/                  # route enumeration, constraints, scoring
output/                  # email digest + Google Sheets price history
main.py                  # async orchestrator
.github/workflows/       # daily 7am CT GitHub Actions run
```

## Data sources

| Source       | Purpose                              | Cost        |
| ------------ | ------------------------------------ | ----------- |
| Kiwi Tequila | LCC cash fares (affiliate-only now)  | Free tier (if granted) |
| Amadeus      | Legacy carrier cash fares            | Free tier   |
| Duffel       | LCC + legacy cash fares (NDC)        | Free test env |
| Seats.aero   | International award availability     | ~$10/mo     |
| Smiles       | Brazilian domestic awards (GOL)      | Free (anon) |
| SendGrid     | Email delivery                       | Free tier   |
| Google Sheets| Price history                        | Free        |

Kiwi's Tequila API is affiliate-gated as of late 2025 — if you have a key set it;
otherwise Amadeus + Duffel together cover the cash-fare layer.

See `config/trips.yaml` for how to configure a trip.

## Build order (Phase 1)

1. Scaffolding → 2. Config → 3. Routes → 4. Kiwi → 5. Seats.aero →
6. Smiles → 7. Constraints → 8. Scorer → 9. Digest → 10. Email →
11. Sheets → 12. Main → 13. GitHub Actions → 14. E2E.

## Safety notes

- All API calls are anonymous. No loyalty account credentials are ever used.
- GRU 3-hour minimum connection is a hard filter, not a warning.
- Separate-ticket itineraries are flagged explicitly.
- The system is honest about gaps — it prefers flagging uncertainty over silently missing data.
