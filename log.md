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

## 2026-07-20 — European coverage: Swiss + French delay data

- Delay stats now cover Switzerland and France next to Germany; journeys with legs in CH/FR (incl. cross-border Paris→Zürich) get badges, sorting, and tight-transfer warnings from the same unchanged lookup (`app/delays.py`/`app/main.py` untouched). Austria deferred (no per-stop open data; ÖBB HAFAS polling is the future path).
- Architecture: per-country producers write per-day parquets in the DE 17-column schema under `data/{ch,fr}/days/`; new `pipeline/merge_delays.py` UNION ALLs `data/de/delays.parquet` + day files into `data/delays.parquet` with a country partition on the padded-eva prefix (080/085/087 — drops IRIS's foreign border stops, prevents cross-source duplicates) and a global cut at last midnight on `time` AND `arrival_planned_time` so no source shifts the app's window anchor (`_max_day`) past what all countries cover. Cross-midnight rows stay in interior days (a first per-day midnight cut was found by review to permanently drop night-train arrivals and was replaced by this merge-level cut). `build_delay_db.py` gained `--output`, default `data/de/delays.parquet`.
- Switzerland (`pipeline/build_ch_days.py`): official istdaten v2 daily CSVs (opentransportdata.swiss; ~50-day rolling window scraped off the CKAN page — resource UUIDs rotate daily, the action API 403s anonymously). Filters: PRODUKT_ID=Zug, no DURCHFAHRT, BPUIC 85\*, arrival parseable, prognosis REAL/GESCHAETZT or cancelled. BPUIC == bahn.de extId directly. Key quirk found by probing: bahn.de sends the **line** number as fahrtNr for Swiss S-Bahn (S12 → "12") but the **run** number for everything else (IC 1519, RE 24 → 4720), while istdaten's LINIEN_ID is always the run number — so S/SN rows are keyed by the digits of LINIEN_TEXT, rest by LINIEN_ID (FAHRT_BEZEICHNER kept in `train_line_ride_id` as fallback). 31-day backfill ingested same-day (~128–145k train-stop rows/day). Cross-operator spot check (SBB/BLS/RhB/SOB): 20/22 bahn.de legs matched; misses were weekday-variant run numbers, same semantics as DE.
- France (`pipeline/fr_common.py`, `fr_poller.py`, `consolidate_fr.py`, `backfill_fr.py`): no official history exists, so a 24/7 poller (systemd, 120 s) ingests the official SNCF GTFS-RT trip-updates feed (transport.data.gouv.fr proxy, no auth, ODbL) into `data/fr/obs.sqlite` (WAL; PK (start_date, trip_id, stop_id); keep-last upsert with arrival/departure updated as units). Daily consolidation rewrites the last two start_dates (night trains) into day parquets; planned = feed `time − delay`, actual = last projection (accepted caveat), cancellations = trip CANCELED/stop SKIPPED — verified live: SNCF sends per-stop SKIPPED with times on canceled trips; bare no-STU trip cancellations are also propagated to stored stops at consolidation (review finding). One-time 35-day backfill decoded mirror.traines.eu daily tarballs (ODbL; 721 snapshots/day) through the same consolidate path — instant history 06-15..07-18; re-run `backfill_fr.py` in a few days to plug 07-19/20 once the mirror publishes them.
- Station IDs, France: bahn.de extIds for French stations are DB-assigned EVAs (Gare de Lyon extId 8700012 ≠ SNCF UIC 87686006), so `pipeline/build_fr_crosswalk.py` builds committed `config/fr_uic_to_eva.json` (3472/3534 GTFS stations): trainline-eu seed + bahn.de `orte` fill accepting only results whose location id carries the matching `i=U×00<uic7>` token; 20-sample seed verification, 0 mismatches. TGV/OGV/TER spot checks matched live feed rows incl. a +25 min delay at Avignon. Known gap: Trenitalia France absent from the SNCF feed.
- Deps: gtfs-realtime-bindings, brotli. Frontend: footer got a data-attribution line (DB IRIS · opentransportdata.swiss — contractually required · SNCF/ODbL), `footerData` i18n key, cache-busters app.js v=22 / style.css v=15.
- Verified E2E on :8001 against the merged parquet (DE 14.0M + CH 4.3M + FR 2.2M rows): Zürich→Bern IC/IR 7/7 days; Paris→Lyon TGVs matched (weekday-variant 5/7) incl. a med=25 red badge; Paris Est→Zürich shows stats on every leg (TGV + TER + IC). Window anchor confirmed 2026-07-19 23:59 across sources; app RSS 5.4 GB vs 3.4 GB before (123 GB box). Multi-agent review: 3 confirmed findings, all fixed (midnight cut, cancellation marker propagation, dead incremental_vacuum pragma).
- Deploy (systemd, needs sudo): replace `delaybahn-pipeline.service` with the 4-step DE→CH→FR→merge flow (`-` prefixes tolerate per-country failure) and install+enable new `delaybahn-fr-poller.service`.

## 2026-07-22 — "Unlikely transfer" variant of the tight-transfer warning

- `tight_transfers()` entries now carry an `unlikely` flag: true when the arriving leg's median delay exceeds the transfer time by more than `UNLIKELY_EXCESS_MIN` (30) minutes. The frontend then renders "⛔ Unwahrscheinlicher Umstieg:" / "⛔ Unlikely transfer:" instead of "⚠ Knapper Umstieg:" / "⚠ Tight transfer:"; detail text and strip styling unchanged.
- app.js cache-buster bumped to v=23.

## 2026-07-22 — Headline badge override + sort penalty for unlikely transfers

- The journey-card headline badge (previously always the final leg's median, so a doomed Tübingen→Zürich chain still showed "+0 min") is replaced by a red non-clickable "⛔ Anschluss wohl verpasst" / "⛔ Connection likely missed" pill whenever any `tightTransfers[]` entry has `unlikely: true`; the tooltip lists the affected station(s). New i18n keys `unlikelyBadge`/`unlikelyBadgeTooltip`.
- "Wenigste Verspätung" sort now ranks journeys with a likely-missed connection after all normal journeys (still by delayScore among themselves); no-data journeys stay last. Frontend-only change; verified headless (Playwright) on :8001 — badge, tooltip, DE/EN, sort order, per-leg day charts intact.
- app.js cache-buster bumped to v=24.

## 2026-07-22 — Rename headline pill to "Connection risk"

- The unlikely-transfer headline pill text changed from "Anschluss wohl verpasst" / "Connection likely missed" to "⛔ Anschlussrisiko" / "⛔ Connection risk" (`unlikelyBadge`); tooltip and behavior unchanged. app.js cache-buster bumped to v=25.

## 2026-07-23 — Past-journey compensation checker (mode=past)

- Home page gets a dashed-red CTA ("Über 1 Stunde Verspätung gehabt? / Hit by over 1 hour of delay?") that flips the search card into a past-journey mode: banner with the covered data range (new `/api/coverage`; date picker clamped to it), stats-window select hidden, search button "Reise prüfen / Check my journey", back link restores the normal checker. Mode is part of the shareable URL (`&mode=past`).
- Backend: `/api/journeys?mode=past` runs the same bahn.de search — probed to return past connections at least 60 days back, prices absent — but attaches `delayOnDate` per leg: the exact arrival delay + cancellation for that calendar day (`leg_delay_on_date` in app/delays.py; same train/EVA/±120-min matching as the median query, no aggregation; missing `arrival_change_time` (~1.2 % of rows) treated as on time per IRIS semantics).
- Results show per-leg exact delays (day charts disabled); the claim column replaces price/booking: "X % zurückholen → / Get X% back →" per DB Fahrgastrechte (25 % from 60 min, 50 % from 120 min delay at the destination), linking to bahn.de/buchung/reiseuebersicht/vergangene (digital claim flow in the customer account; verified reachable logged-out), with a fallback link to www.bahn.de/fahrgastrechte for tickets outside a DB account. Disclaimer: percentages apply to the ticket price, €4 minimum payout, recorded data is not authoritative. Umami events `refund-cta` / `claim-db`.

## 2026-07-23 — Missed-connection simulation for past journeys

- Past-mode journeys are now simulated leg by leg instead of statically flagged: `_simulate_walk()` (app/main.py) rides each leg with that day's actual delay; a transfer counts as made only when the connecting train's actual departure — its own delay included, via new `leg_departure_on_date()` in app/delays.py — leaves > `TRANSFER_TOLERANCE_MIN` after the passenger is ready (walking legs subtracted). On a miss or cancellation the itinerary is re-planned from that station to the final destination via bahn.de (minimal `A=1@O=<name>@L=<eva>@` tokens verified to work) and the walk continues over the replacement legs; up to `MAX_REPLANS=3` chained re-plans, responses cached per (origin, dest, minute).
- `_next_connection()` probes 45 min before the ready time and picks the catchable candidate with the earliest actual arrival, so delayed earlier-planned trains — typically the just-ridden train continuing onward — are considered, not only later timetable departures.
- Journey `arrivalDelay`/`compensationPct` derive from the simulated arrival vs the booked planned arrival. Frontend: header shows `dep → ~~planned~~ actual`, missed legs are struck out (gray "verpasst / missed", red "ausgefallen / cancelled" badges — no extra warning strip in past mode, the badges carry the information; the inline strip remains a future-mode tight-transfer feature), and a "↳ Tatsächliche Weiterfahrt mit der nächsten möglichen Verbindung:" section lists the replacement legs with their own exact-delay badges. If no replacement is found the card keeps the red "Missed connection" pill, a "check your claim" button, and a "no replacement found" note.
- Verified (curl + headless Playwright): Berlin→München 16.07. — ICE 707 +36 into Nürnberg, ICE 625 missed → sim rides ICE 587, arrival +42 → honestly "no compensation" (old logic would have flagged an unknown miss). Hannover→Hamburg night 15./16.07. — ICE 2512 cancelled → RE2 (+53) + RB31, arrival +238 → 50 %. Paris Est→Tübingen 19.07. — TGV 9577 +153, IC 2167 cancelled → IC 2169/RE14a/RB63, arrival +120 → 50 %. Future mode regression-checked (badges, day charts, tight warnings, booking button). Cache-busters app.js v=28, style.css v=17.
