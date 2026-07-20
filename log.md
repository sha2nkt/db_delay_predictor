# Log

Append-only. Add new entries at the bottom with a date heading; never edit or delete existing entries.

---

## 2026-07-12 — Initial build

- Explored `deutsche-bahn-data` submodule: collects per-station-stop IRIS timetable data (plan + fchg) every 6 h into HuggingFace dataset `piebro/deutsche-bahn-data`; monthly processed releases; no journey/routing concept.
- Decisions (with user): journeys via public transport.rest wrapper; historical delays from HF raw data (no DB API key); FastAPI + DuckDB + vanilla JS; booking = bahn.de deep link.
- Scaffolded uv project: `pyproject.toml`, `pipeline/`, `app/`, `static/`.
- `pipeline/build_delay_db.py`: downloads last 8 days of raw parquet via `snapshot_download` (skips existing), reuses submodule's `process_files_to_temp` XML parser, adapts the monthly-release merge SQL to a rolling window, writes `data/delays.parquet`. Window ends yesterday (today's uploads incomplete); oldest day is boundary-only for cross-midnight trains.
- **Pivot**: v6.db.transport.rest returned 503 on all endpoints (v5 too). Switched to bahn.de web API directly (`/web/api/reiseloesung/orte` for locations, POST `/web/api/angebote/fahrplan` for journeys) — same upstream that db-vendo wraps. Removed `app/transport_rest.py`, added `app/bahn_api.py`. Bonus: Berlin-local naive times (no tz conversion) and ticket prices in the response.
- `app/delays.py`: DuckDB query matching train legs to history by `train_number` (zero-strip both sides) + padded arrival EVA + time-of-day proximity (±120 min, closest per day); arrival delay computed from `arrival_planned_time`/`arrival_change_time`; cancelled days excluded from avg but counted.
- `app/main.py`: normalizes bahn.de `verbindungen`/`verbindungsAbschnitte` into frontend-friendly journeys; `delayScore` = final leg avg arrival delay; `maxLegAvgDelay` = worst leg.
- Frontend: bahn.de-style UI, autocomplete, color-coded delay badges (green <3 / yellow 3–9 / red ≥10 / gray no-data), "n/7 Tage" coverage, cancellation note, sort by departure or least delay, bahn.de booking deep link.
- Smoke run `--days 3`: 2 days, ~440k stops/day, avg 3.6–4.1 min. Full `--days 8`: 7 days (2026-07-05..11), 3.1M rows.
- Verified end-to-end: curl (7/7 days matched on Berlin→München ICEs) + Playwright browser flow (search, badges, sort reorder, booking URL, no console errors).
- Docs added: README.md, feature_list.md, progress.md, this log. Repo initialized as git with `deutsche-bahn-data` as a proper submodule.

## 2026-07-12 — Prominent prices + price sorting

- Price moved out of the meta line into a dedicated bold element next to the delay badge; missing price renders "Preis auf bahn.de" instead of nothing (frontend only; `journey.price` already existed in the API).
- Added "Günstigster Preis" sort toggle: ascending by price, no-price journeys last, stable sort keeps departure order on ties.
- Verified via Playwright: prices render on all cards, price sort orders 135,99 € before 149,99 €, no console errors.

## 2026-07-12 — Selectable statistics window + earlier/later paging

- Averaging window now user-selectable (7/15/30 days, default 7): `/api/journeys` takes `window`; `leg_delay_stats` filters to the last N days anchored at the newest day in the parquet (not now(), so stale data still yields full windows); cache keyed by `(train, eva, window)`. Pipeline default raised to `--days 31` (30 full days).
- Earlier/later connection paging: bahn.de `verbindungReference.earlier/later` tokens exposed as `earlierRef`/`laterRef`; frontend buttons prepend/append the adjacent page, deduplicating by (planned departure, planned arrival, train names).
- Homepage scatter chart got a DE/EN toggle swapping delay-correlation.svg / delay-correlation-en.svg.

## 2026-07-13 — Site-wide DE/EN language toggle

- Moved the DE/EN toggle from the chart into the header; it now switches the whole UI layout language, not just the chart SVG (which still swaps de/en variants).
- Static HTML tagged with `data-i18n` (+ `-placeholder`/`-title` variants); all dynamic strings routed through an `I18N` dict + `t()` in app.js, including parameterized ones (transfer count, badge tooltip, cancellation note). Status messages stored as key+params so on-screen text re-renders on switch; `<html lang>` and `document.title` follow.
- Language choice persists in localStorage and is applied on load. Data values (station names, train names, prices, times) untouched; only surrounding label text changes.

## 2026-07-14 — Full 30-day data window

- Diagnosed "15/30-day windows only match 7 days": `delays.parquet` was still the July-12 build from when the pipeline default was `--days 8`; nobody re-ran it after the default was raised to 31. Not a code bug — upstream HF repo retains full raw history (~140 MB/day), so nothing was ever missing upstream.
- Interim workaround (same day, later superseded): merged June 13–30 from `monthly_processed_data/data-2026-06.parquet` into the parquet by `id`-dedup.
- Proper fix: re-ran `build_delay_db.py` with the default 31 days → 30 full days (2026-06-14..07-13), 14.97M plan rows, 5 GB raw mirror. Verified end-to-end: `leg_delay_stats` and live `/api/journeys` both match 7/15/30 days for windows 7/15/30.
- `build_delay_db.py` now prunes raw-mirror day dirs outside the current window after each build, so the local mirror stays a rolling ~31 days instead of growing 140 MB/day.
- The daily `delaybahn-pipeline.timer` (05:39, set up earlier today) keeps the window full from here on; it runs with the 31-day default.

## 2026-07-19 — Tight-transfer warnings, typed-station fallback, logo home link

- `tight_transfers()` in app/main.py: for each pair of consecutive train legs, computes the transfer buffer (planned gap minus intermediate walking legs) and flags it when the arriving leg's median delay leaves ≤ `TRANSFER_TOLERANCE_MIN` (2) minutes; exposed per journey as `tightTransfers`. Frontend renders a red `.tight-flag` chip above the leg list (DE/EN strings).
- `resolveTyped()` in app.js: a station typed but never picked from the dropdown now resolves via an exact case-insensitive name match against `/api/locations` before search, instead of failing with "pick stations".
- Header logo is now a link back to `/` (clears results/params).
- Cache-busters bumped: style.css v=6, app.js v=9 (Cloudflare edge caches static assets ~4 h).
- Shareable searches: `syncUrl()` writes fromId/from/toId/to/date/time/window into the query string via `history.replaceState` on every search; on load, matching params restore the form state and re-run the search (refresh, bookmark, shared link). app.js bumped to v=15.

## 2026-07-19 — Recent-station suggestions + tight-transfer flag redesign

- Focusing an empty Von/Nach input now suggests the last 6 searched stations under a "Letzte Suchen"/"Recent searches" label; stations are saved to localStorage (`recentStations`) on each search, deduped by id, newest first. Dropdown rendering unified into `showItems()` shared by recents and live autocomplete results.
- Tight-transfer warning redesigned: the floated inline sentence chip is replaced by a `.tight-col` card column beside the leg list with a "Knapper Umstieg!" title and the transit time; the median-delay figure was dropped from the flag text (the per-leg badges already carry it). `.tight-flag` restyled as a left-red-border card.
- style.css cache-buster bumped to v=12.

## 2026-07-19 — Tight-transfer card: delay line + warning glyph

- The tight-transfer card beside the leg list got a third line, "previous train delay: Y mins" (`tightDelay` in both languages), restoring the median-delay figure dropped in the previous redesign.
- Card title now carries the ⚠ glyph ("⚠ Knapper Umstieg!" / "⚠ Tight transfer!"), matching the cancellation note. A brief station-name addition to the title was reverted same-session.
- app.js cache-buster bumped to v=20.

## 2026-07-20 — Tight-transfer warning moved inline under the leg row

- The tight-transfer card column beside the leg list (`.legs-wrap`/`.tight-col`/`.tight-flag`) is replaced by an inline `.leg-tight` strip rendered directly under the affected leg row, keyed by `tightTransfers[].legIndex`. Single-sentence text: "⚠ Knapper Umstieg: X min Umstiegszeit – dieser Zug kommt typischerweise +Y min verspätet an" (DE/EN); the separate `tightTransit`/`tightDelay` strings collapsed into one `tightDetail`.
- Styling: left-red-border strip matching the previous flag's palette; the mobile `.legs-wrap` column stacking rule is gone with the wrapper.
- Cache-busters bumped: style.css v=14, app.js v=21.
- `.claude/settings.json` (pre-commit docs hook) now committed; `.claude/settings.local.json` gitignored.
