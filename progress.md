# Progress

Snapshot of the current state. Update this file in place when the state changes; history lives in `log.md`.

## Current state (2026-07-12)

MVP complete and verified end-to-end. The app runs locally: pipeline builds `data/delays.parquet` (30 full days by default, ~450k stops/day), FastAPI serves the enriched journey search, frontend renders and sorts correctly. The averaging window is user-selectable: 7, 15, or 30 days (default 7).

## Verified

- Pipeline: 8-day build parses ~955k XML responses → 3.1M merged stop rows; per-day avg delay 2.3–4.1 min (plausible network-wide values).
- API: `curl /api/journeys` Berlin Hbf → München Hbf returns journeys with `delayStats` at 7/7 days matched on all ICE legs.
- Cross-check: same train numbers (e.g. ICE 1505) found in both bahn.de journey results and the historical table at the arrival EVA — validates EVA padding + train-number matching.
- Browser (Playwright, headless Chromium): autocomplete → search → result cards with badges → "Wenigste Verspätung" sort reorders correctly → booking deep-link contains correct stations and departure time. No console errors.

## Key implementation decisions

- **bahn.de web API instead of v6.db.transport.rest**: transport.rest (planned upstream) was down (503 on v5+v6) during development. bahn.de's own web API is what db-vendo wraps anyway; it returns Berlin-local naive times (no tz conversion) and ticket prices. See log.md 2026-07-12.
- **Arrival delay, not the dataset's `delay_in_min`**: the dataset's column prefers departure delay; passengers care about arrival. We compute `date_diff('minute', arrival_planned_time, arrival_change_time)` ourselves.
- **Delay score = final leg's avg arrival delay** (delay at the destination), with worst-leg avg as tiebreaker/transfer-risk signal.
- **EVA normalization**: dataset EVAs are 8-char zero-padded strings; bahn.de extIds are unpadded → `pad_eva()` in app/delays.py.
- **Window ends yesterday** by default: today's raw uploads (every 6 h) are incomplete.
- **Selectable averaging window (7/15/30 days)**: pipeline default is `--days 31` (30 full days, raw mirror ~4.3 GB at ~140 MB/day); `/api/journeys` takes `window` (Literal 7/15/30, default 7); `leg_delay_stats` filters `CAST(arrival_planned_time AS DATE) >= _max_day - (window-1)` — anchored to the newest day in the parquet, not now(), so stale data still yields full windows; cache keyed by `(train, eva, window)`. If the parquet holds fewer days than requested, results degrade gracefully (badge shows e.g. 7/30 Tage).

## Not done / next candidates

- Scheduled daily pipeline refresh (cron/launchd); currently manual.
- Old raw days accumulate in `data/raw_data/` (~140 MB/day); no pruning yet.
- Passenger/class options in search.
- Per-day delay breakdown in the UI (the data is there, only aggregates shown).
- delays.py in-process cache never invalidates; fine while the server restarts after each pipeline run.

## How to resume work

1. `uv sync`, then `uv run python pipeline/build_delay_db.py` (refreshes the window; skips existing downloads).
2. `uv run uvicorn app.main:app --port 8000`, open http://localhost:8000.
3. Read `feature_list.md` for scope, `log.md` for history, and `deutsche-bahn-data/AGENTS.md` for DuckDB patterns over the dataset.
