# Progress

Snapshot of the current state. Update this file in place when the state changes; history lives in `log.md`.

## Current state (2026-07-22)

Live at delaybahn.com. Delay statistics cover Germany, Switzerland, and France (CH/FR added 2026-07-20): per-country pipelines write day parquets that `pipeline/merge_delays.py` unions into `data/delays.parquet`, so cross-border journeys (e.g. Paris→Zürich) get badges and tight-transfer warnings on every leg with zero app-code changes. CH comes from official istdaten daily files (31-day backfill done); FR from a 24/7 GTFS-RT poller plus a one-time mirror backfill. Deploy of the two updated/new systemd units is pending (needs sudo). Pipeline builds `data/delays.parquet` (30 full days, refreshed daily by systemd timer), FastAPI serves the enriched journey search, frontend renders and sorts correctly. Delay statistics use the median (since 2026-07-19), with a per-day chart behind the delay badges. The statistics window is user-selectable: 7, 15, or 30 days (default 7). Site-wide DE/EN toggle. Newest additions (2026-07-19/20, implemented but not yet browser-verified): tight-transfer warnings (since 2026-07-20 an inline red strip under the affected leg row: "⚠ Knapper Umstieg: X min Umstiegszeit – dieser Zug kommt typischerweise +Y min verspätet an"; since 2026-07-22 the strip says "⛔ Unwahrscheinlicher Umstieg" when the median delay exceeds the transfer time by > 30 min), exact-match resolution of typed-but-not-selected station names, shareable search URLs (query-string sync + restore), logo links home, recent-station suggestions on focusing an empty input (localStorage, last 6).

## Verified

- Pipeline: 8-day build parses ~955k XML responses → 3.1M merged stop rows; per-day avg delay 2.3–4.1 min (plausible network-wide values).
- API: `curl /api/journeys` Berlin Hbf → München Hbf returns journeys with `delayStats` at 7/7 days matched on all ICE legs.
- Cross-check: same train numbers (e.g. ICE 1505) found in both bahn.de journey results and the historical table at the arrival EVA — validates EVA padding + train-number matching.
- Browser (Playwright, headless Chromium): autocomplete → search → result cards with badges → "Wenigste Verspätung" sort reorders correctly → booking deep-link contains correct stations and departure time. No console errors.

## Key implementation decisions

- **bahn.de web API instead of v6.db.transport.rest**: transport.rest (planned upstream) was down (503 on v5+v6) during development. bahn.de's own web API is what db-vendo wraps anyway; it returns Berlin-local naive times (no tz conversion) and ticket prices. See log.md 2026-07-12.
- **Arrival delay, not the dataset's `delay_in_min`**: the dataset's column prefers departure delay; passengers care about arrival. We compute `date_diff('minute', arrival_planned_time, arrival_change_time)` ourselves.
- **Delay score = final leg's median arrival delay** (delay at the destination), with worst-leg median as tiebreaker/transfer-risk signal (avg → median switched 2026-07-19).
- **Tight-transfer warning**: `tight_transfers()` in app/main.py flags transfers where the arriving leg's median delay leaves ≤ 2 min of buffer (`TRANSFER_TOLERANCE_MIN`); walking legs between trains are subtracted from the planned gap. Entries carry an `unlikely` flag (median delay > transfer time + 30 min, `UNLIKELY_EXCESS_MIN`) that switches the warning to "⛔ Unwahrscheinlicher Umstieg / Unlikely transfer".
- **EVA normalization**: dataset EVAs are 8-char zero-padded strings; bahn.de extIds are unpadded → `pad_eva()` in app/delays.py.
- **Window ends yesterday** by default: today's raw uploads (every 6 h) are incomplete.
- **Selectable averaging window (7/15/30 days)**: pipeline default is `--days 31` (30 full days, raw mirror ~4.3 GB at ~140 MB/day); `/api/journeys` takes `window` (Literal 7/15/30, default 7); `leg_delay_stats` filters `CAST(arrival_planned_time AS DATE) >= _max_day - (window-1)` — anchored to the newest day in the parquet, not now(), so stale data still yields full windows; cache keyed by `(train, eva, window)`. If the parquet holds fewer days than requested, results degrade gracefully (badge shows e.g. 7/30 Tage).
- **Site-wide DE/EN toggle (frontend only)**: header pills replace the chart-only toggle. Static text carries `data-i18n`/`data-i18n-placeholder`/`data-i18n-title` attributes; dynamic strings (status, badges, journey cards, tooltips) go through the `I18N` dict + `t()` in static/app.js. Status messages are stored as key+params so they re-render on switch; `<html lang>`, `document.title`, and the chart SVG (de/en variant) follow. Choice persists in localStorage. Data values (station names, prices, times) stay as the API returns them.
- **Automated daily refresh**: systemd timer `delaybahn-pipeline.timer` on ps083 runs the pipeline daily at 05:30 Europe/Berlin (`Persistent=true`, up to 15 min randomized delay), then restarts `delaybahn.service` to reload the parquet. `prune_old_raw_days()` in pipeline/build_delay_db.py removes raw-mirror days outside the window after each build, keeping `data/raw_data/` a rolling ~31 days.
- **Multi-country delay data (2026-07-20)**: per-country producers emit the DE 17-column schema into `data/{ch,fr}/days/*.parquet`; `merge_delays.py` unions them with a partition on the padded-eva country prefix (080/085/087) and a global last-midnight cut on both `time` and `arrival_planned_time` so no source shifts `_max_day`. CH: istdaten v2 (BPUIC == bahn.de extId; S-Bahn keyed by line digits because bahn.de sends the line, not the run number, for Swiss S-Bahn only). FR: SNCF GTFS-RT poller → `data/fr/obs.sqlite` (keep-last upsert) → daily consolidation; station IDs crosswalked SNCF-UIC → DB-EVA via committed `config/fr_uic_to_eva.json`; cancellations = trip CANCELED / stop SKIPPED, bare trip-cancellation markers propagated at consolidation. Austria deferred: no per-stop open data, ÖBB HAFAS polling is the future path. See log.md 2026-07-20.

## Not done / next candidates

- Deploy the 2026-07-20 systemd changes (updated `delaybahn-pipeline.service`, new `delaybahn-fr-poller.service`) — unit contents in log.md entry / scratchpad; needs sudo.
- Re-run `pipeline/backfill_fr.py` in a few days to plug the FR seam days (2026-07-19/20) once mirror.traines.eu publishes them.
- Austria via ÖBB HAFAS board polling; other countries follow the same producer pattern (day parquets + one merge line + eva prefix).
- Passenger/class options in search.
- Browser-verify the 2026-07-19 additions (tight-transfer flag, typed-station resolution, shareable URLs, recent-station suggestions).
- delays.py in-process cache never invalidates; fine while the server restarts after each pipeline run.

## How to resume work

1. `uv sync`, then `uv run python pipeline/build_delay_db.py` (refreshes the window; skips existing downloads).
2. `uv run uvicorn app.main:app --port 8000`, open http://localhost:8000.
3. Read `feature_list.md` for scope, `log.md` for history, and `deutsche-bahn-data/AGENTS.md` for DuckDB patterns over the dataset.
