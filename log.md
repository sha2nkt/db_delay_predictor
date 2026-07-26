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

## 2026-07-23 — Past-mode UI polish

- Claim hint gains the missing step: "einloggen → Reise auswählen → Reisedetails anzeigen → Entschädigung beantragen" / "log in → select your trip → click Trip Details → request compensation" (matches the actual bahn.de flow, where the compensation button sits behind Reisedetails).
- The refund CTA is now home-page-only: hidden when future-mode search results render (like the hero chart), restored on returning to the home state via the past-mode exit link (`.refund-cta.hidden`).
- Searching a too-recent past date now says when to check back: normally "Neue Daten kommen jeden Morgen dazu – schau ab dem <D+1> wieder vorbei" / "New data arrives every morning – check back on <D+1>"; when that morning has already passed (pipeline behind), an honest "die Daten hängen gerade etwas hinterher – schau in den nächsten Tagen wieder vorbei" / "running a bit behind – check back in the next few days" instead (`dateNotYet`/`dateNotYetLag`; local-date comparison, not UTC). Too-old dates keep the existing range message.
- Past-mode search button renamed "Reise prüfen / Check my journey" → "Entschädigung prüfen / Find my compensation".
- Cache-busters app.js v=33, style.css v=18. All message branches and CTA visibility states verified headless (DE/EN).

## 2026-07-25 — bahn.de fingerprint rotation + upstream request cache

- On 2026-07-23 Akamai Bot Manager started answering every `impersonate="chrome"` profile with 403 OPS_BLOCKED (fresh sessions blocked too, so it was the TLS/HTTP2 fingerprint, not cookies or rate); firefox and safari profiles passed. `app/bahn_api.py` now keeps a `PROFILES` list (`firefox135`, `safari17_0`, `chrome`) with one lazily-built `AsyncSession` each and rotates to the next profile on a 403, retrying the same request (`_request()`); the rotation is guarded by a lock so concurrent 403s advance the index once. `client` is gone — `app/main.py` shuts down via the new `bahn_api.close()`.
- Both upstream calls go through `_cached()`: an LRU (`CACHE_MAX=512`) of asyncio tasks keyed by the request arguments, TTL 120 s for journeys and 600 s for locations. Callers await the shared task, so a spike on the same search rides one upstream request; a still-running task is handed out even past its TTL. Failed or cancelled tasks are dropped from the cache in a done-callback, so a block clears as soon as the next request rotates.

## 2026-07-25 — Say what the compensation check actually shows

- The refund CTA promised money back without saying the tool reconstructs the journey as it really ran, so it gained a middle line: "Sieh die Reise, die du tatsächlich hattest – mit Verspätungen und verpassten Anschlüssen." / "See the journey you actually took, including delays and missed connections." (`refundCtaLead`, rendered in normal text colour between the red headline and the muted "3 Klicks" line; `.refund-cta span` is now `display: block`).
- The past-mode banner carries the same promise as an instruction now that the user is at the form: "Gib deine Reise ein, um zu sehen, wie sie tatsächlich verlief – mit Verspätungen, verpassten Anschlüssen und deinem Entschädigungsanspruch." / "Enter your journey to see the trip you actually took – including delays, missed connections and what you can claim back." (`pastLead`, full-width row in the banner flexbox).
- Cache-busters app.js v=34, style.css v=19.

## 2026-07-25 — Same-day past journeys answered live from the DB Timetables API

- Past mode could only answer days already in `data/delays.parquet`, so a journey stayed uncheckable until the next morning's build. Two causes, found by tracing the lag: the batch floor (HF publishes raw IRIS XML every 6 h, `build_delay_db.py` ends its window at yesterday, timer fires 05:30 and takes ~39 min → answerable ~06:20 on D+1), and a deploy gap — the running `delaybahn-pipeline.service` still executes only `build_delay_db.py`, which since the multi-country refactor writes `data/de/delays.parquet`, so **nothing had written `data/delays.parquet` since 2026-07-20 15:23** while the DE build succeeded daily. `/api/coverage` was serving `maxDay: 2026-07-19`, six days stale.
- Data caught up by hand: `build_ch_days.py` (CH through 07-24), `consolidate_fr.py`, `backfill_fr.py --days 6 --end-date 2026-07-22` to plug the FR seam, then `merge_delays.py`. All three countries now continuous through 07-24 (DE 408k, CH 138k, FR 60k arrivals on 07-24); FR 07-18/19 are thin (40k/35k vs ~59k) where the poller gap meets the mirror archive's start-of-day.
- New `app/live_delays.py`: resolves a stop through `timetables/v1/plan/{eva}/{yymmdd}/{HH}` (train number + planned time → IRIS stop id) and reads `ar/@ct` / `dp/@ct` from `timetables/v1/fchg/{eva}` — the same field the nightly build stores, so a delay seen minutes after arrival equals what the parquet will hold. `warm()` is the only await: it prefetches every (eva, hour) an itinerary touches concurrently (semaphore 10, `MAX_CALLS_PER_WARM=150`), after which `leg_delay_on_date`/`leg_departure_on_date` are sync and cache-only, mirroring the `app/delays.py` signatures so `normalize_leg` stays synchronous. Shared-task LRU as in `bahn_api` (plan 6 h, fchg 60 s); 404 → empty, not an error; credentials from env or a gitignored `.env`.
- `app/main.py` threads a `live` flag through `normalize_leg`, `_departure_info`, `_simulate_walk` and `_next_connection`; it is on only when the requested day is past `delays.coverage()[1]`, and each lookup falls back to the parquet, so the parquet stays authoritative wherever it has the day. `_warm_live()` runs before the main normalize loop and again inside `_next_connection`, so re-planned replacement legs resolve live too. No credentials → `live` is never on and behaviour is unchanged.
- **Stops still ahead report unknown, not on time.** IRIS emits no change message for a stop that hasn't happened, which first read as "+0 min": a journey arriving 22:00, checked at 18:00, would have been told it was punctual with no claim. `_lookup` now returns `None` when `(ct or pt)` is still in the future. Cancellations still report, being known ahead and factual.
- Measured `fchg` lookback with real credentials at 22:14: Hamburg Hbf oldest change 05:31 (16.7 h), München 06:42 (15.5 h), Köln 08:00 (14.2 h) — far more than the ">6 hours" the upstream README implies, and enough to meet the 06:20 build. **The intraday-refresh idea (rebuild after each 6-hourly HF upload) is therefore not needed.**
- Cross-checked live IRIS against bahn.de's own `echtzeit` on eight Berlin↔Hamburg legs that had just run: six exact, two off by 1 min (IRIS is the side that matches tomorrow's parquet). bahn.de `echtzeit` was also measured as a fallback source and rejected: present only while a train runs and ~3 h after it finishes, and a prognosis rather than the recorded value.
- Frontend: `/api/coverage` gains `liveMaxDay` (today, or null without credentials); `latestPastDay()` extends the picker to it; a leg with no observation yet renders as "noch offen / pending" with a tooltip instead of "keine Daten", and the claim column says "Ankunft noch nicht bestätigt – morgen früh prüfen / Arrival not confirmed yet – check tomorrow morning" (`journey.pending` from the backend). Days beyond `liveMaxDay` keep the existing `dateNotYet`/`dateNotYetLag` messages.
- Verified end-to-end at 22:15 for a 17:00 Hamburg→Berlin departure the same evening: ICE 2547 +34, cancelled FLX 1247 → re-planned to 58 min late at the destination, ICE 607 +22, FLX 1349 +5, ICE 873 +17; journeys later that night correctly `pending` with no verdict. Cold search 1.36 s (13 IRIS calls for a 6-result page), warm 0.00 s. Stubbed-IRIS checks cover delayed/cancelled/on-time/±2-min-skew/leading-zero/unknown-train cases, plus a forced +95 min miss that re-plans over live data.
- Investigated and dismissed: bahn.de reports `ankunftsOrtExtId` `08098160` for legs arriving at Berlin Hbf — the same EVA IRIS and the parquet use. The `08011160` that `/api/locations` returns is autocomplete-only and never reaches a delay lookup, so there is no EVA mismatch.
- Still open: `delaybahn-pipeline.service` must be replaced with the 4-step DE→CH→FR→merge flow and `delaybahn-fr-poller.service` installed (needs sudo); until `delaybahn.service` is restarted the live site keeps serving the 07-19 table from memory.

## 2026-07-25 — Local station autocomplete, cache headers, hero chart leads with the claim

- `/api/locations` now answers from the delay data itself: `_build_station_index()` in app/delays.py turns every station in the parquet into an autocomplete entry at startup, deduped by a folded name (umlauts/ß/accents stripped, "hauptbahnhof" → "hbf", separators normalised) so the multi-level Hbf EVAs collapse to one entry, keeping the busiest EVA and summing volume across levels. `station_search()` ranks prefix > word-start > substring, main stations first, then by observation volume; bahn.de is called only when the local index has no match (rural stops, POIs, addresses).
- `Cache-Control` added to both API endpoints: `public, max-age=600` on `/api/locations`, `public, max-age=120` on `/api/journeys` (matching the in-process TTLs).
- Hero chart reworked: the headline moved out of the SVG into the page as a two-line claim ("Verspätete Züge bleiben verspätet. / Pünktliche Züge bleiben pünktlich.") with a scope+finding sub-line, and the plot now sits behind a "Daten ansehen" toggle, fading in via `allow-discrete`/`@starting-style`. `make_delay_scatter.py`/`make_delay_violin.py` drop the in-chart title and shift everything below the subtitle up by `TOP_SHIFT=34`, shrinking the viewBox; all four SVGs regenerated.
- Cache-busters app.js v=38, style.css v=22.

## 2026-07-25 — Header button for the compensation check

- The compensation check was reachable only through the mid-page CTA; the header now carries a solid white pill button ("Entschädigung beantragen / Apply delay compensation") between the title and the language toggle — the only filled element on the red bar, so it reads as the primary action. In past mode it shows a pressed state (red-tinted fill + inset shadow) keyed off `body.past-mode`, so it lights up regardless of entry path (header button, CTA, or `?mode=past` deep link). Clicking tracks `refund-nav` in Umami (separate from `refund-cta`, so the two entry points can be compared) and focuses the From field; a second click is a no-op (`setMode` returns early on the same mode).
- `.header-inner` wraps on ≤700 px screens; the button carries `margin-left: auto` so it and the DE/EN pills drop to a right-aligned second row instead of overflowing narrow phones.
- Cache-busters app.js v=40, style.css v=24.

## 2026-07-26 — Delay reasons in the per-day charts and past-mode badges

- Every delay tooltip can now say *why*: IRIS delay-cause messages (`<m t="d" c="43" ts="…"/>`) are extracted end-to-end and rendered as the official German cause texts (with EN translations) in the per-day chart tooltips (future mode) and the exact-delay badge tooltips (past mode, live legs included).
- New `pipeline/fchg_parse.py` replaces the submodule's fchg path in `process_files_to_temp` (plan parsing still imported unchanged): per stop it keeps the latest cause message by `ts` (yymmddhhmm, so string comparison orders chronologically) as `reason_code`/`reason_ts`; columns are typed `Int64`/`string` even in all-None batches so parquet schemas stay unionable. `build_delay_db.py` adds a `reasons` CTE using `arg_max(reason_code, reason_ts)` over **all** fchg responses per stop id — deliberately independent of the newest-response dedup, because a cause message often drops off later fchg responses while the change time survives.
- `merge_delays.py` grew an `OPTIONAL_COLUMNS` mechanism: `reason_code` is selected where present and `CAST(NULL AS INTEGER)` where not (CH istdaten and FR GTFS-RT carry no cause data — the feature is DE-only by data availability). `app/delays.py` guards with `ALTER TABLE … ADD COLUMN IF NOT EXISTS reason_code`, so parquets built before the feature keep working (verified: same response shapes, `reason: null`).
- `leg_delay_stats` days and `leg_delay_on_date` now carry `reason`; `app/live_delays.py` applies the same latest-`ts` selection to live fchg responses, so a same-day journey shows its cause minutes after arrival. `leg_departure_on_date` stays reason-less (feeds only the catchability simulation, never rendered).
- Frontend: `DELAY_REASONS` DE/EN table in app.js (official DB cause texts; codes 70–98 are quality messages that never appear as delay causes and are omitted), `reasonText()` appends " – <cause>" to the day-chart `<title>` tooltips and the past-mode badge `title`s, in the active language. Cache-buster app.js v=41.
- Data note: the nightly timer runs from the main checkout and had rebuilt the shared `data/` (worktree symlinks to it) with the old parser at 05:37, so the parquets initially lacked the column. Forced 31-day rebuild with the new parser (~50 min, 4.1M XML responses): 3.7M of 21.5M merged rows carry a reason; **79.3 % of DE arrivals delayed ≥ 10 min have one**. Top codes: 43 (Verspätung eines vorausfahrenden Zuges), 48 (Verspätung aus vorheriger Fahrt), 45 (Vorfahrt eines anderen Zuges). All codes observed in the data (1–69, 99) are covered by the frontend map.
- Verified end-to-end: API future mode (per-day reasons, e.g. ICE 801 07-24 +122 → 47) and past mode (ICE 1103 +19 → 34, cancelled ICE 723 → 36); live same-day via IRIS at 13:10 for an 08:00 Hamburg→Berlin (FLX 1237 +18 → 45, cancelled FLX 1239 → 1, not-yet-run leg stays pending); Playwright headless: 6/6 opened day charts showed cause tooltips, past-mode badges carry causes, EN switch translates them ("Signal fault", "Delayed provision of the train"), no console errors.

## 2026-07-26 — Tap/click a chart bar to show the delay reason in a bubble

- Hover tooltips don't exist on touch screens, so the per-day chart's full-height hover targets are now clickable: a tap/click shows that day's details (date, delay/cancelled, cause) in a bubble (`.day-chart-bubble`) anchored above the bar tip — clearing the value label / ✕ glyph — with a small arrow pointing at the column and the column tinted while selected. Positioned in CSS pixels at click time (the svg scales with the panel), horizontally clamped into the panel with the arrow tracking the true column center. Clicking the same day again, clicking anywhere else (document-level click-away, added only while open), or closing the chart dismisses it; `pointer-events: none` keeps the bubble from blocking clicks. The bubble text is exactly the tooltip string, so DE/EN and the no-data/cancelled wordings come along for free.
- Verified headless: bubble text equals the tooltip for all 7 days, stays inside the panel and above the plot's bottom edge on every day, reason shown ("25.07. +17 min – Verspätetes Personal aus vorheriger Fahrt"), moves when another day is clicked, second click and click-away dismiss, badge still toggles the panel, no console errors; screenshot-checked ("20.07. +29 min – Behördliche Maßnahme" over the tinted column).
- Cache-busters app.js v=43, style.css v=26.

## 2026-07-26 — SEO basics: meta tags, structured data, robots.txt, sitemap

- `static/index.html` head now carries the on-page SEO set: meta description (DE, mirrors the hero claim + compensation check), canonical `https://delaybahn.com/`, Open Graph tags (type/url/site_name/title/description, `og:image` = logo.png, locale `de_DE` with `en_US` alternate), `twitter:card summary`, and a JSON-LD `WebApplication` block (category `TravelApplication`, `inLanguage` de/en, free offer). Head-only additions — no cache-buster bumps needed.
- New `static/robots.txt`: allow all, `Disallow: /api/` and `/stats/` (the Umami proxy), sitemap pointer. New `static/sitemap.xml`: single URL (the app is one page), `changefreq daily`. Both served at the site root via the existing `StaticFiles` mount in app/main.py.

## 2026-07-26 — Tight-transfer badge in the journey header

- Merely-tight transfers (slack ≤ 2 min after the median delay) were visible only in the inline strip under the affected leg row — nothing in the header summary. Future-mode cards with a non-empty `tightTransfers` now show a yellow "⚠ Knapper Umstieg / Tight transfer" pill directly left of the median delay badge; the tooltip lists the affected station(s). The red "⛔ Anschlussrisiko / Connection risk" pill keeps precedence — unlikely transfers are a subset of tight ones and replace the median badge entirely, so the yellow pill renders only when no transfer is unlikely. Past mode unchanged.
- Cache-buster app.js v=44.

## 2026-07-26 — Tight-transfer badge layout fix

- The new header pill rendered taller than the median delay badge and pushed "Auf bahn.de buchen" onto its own left-aligned line. Two causes: `.badge` had no `line-height`, so the ⚠ glyph's font metrics inflated the span's line box (the median badge is a plain-text `<button>`); and the extra pill overflowed the head row, flex-wrap orphaning only the last item. `.badge` now sets `line-height: 1.2`, and the future-mode header groups badges + price + booking button into a `.journey-cta` flex block (`margin-left: auto`, right-justified, own wrap) that drops to a second line as one unit.
- Playwright-verified on a real Tübingen→Zürich search (data via shareable URL, which restored and re-searched — that feature is hereby browser-verified too): both pills measure 23.6 px on the same row, tight-badge cards show a clean right-aligned action row, cards without the badge keep their one-line header (34 px), no console errors.
- Cache-busters app.js v=45, style.css v=27.

## 2026-07-26 — Sort by transfer risk

- Fourth sort toggle "Geringstes Anschlussrisiko / Lowest connection risk". Future mode ranks by the header-pill tier — no risk, then yellow "⚠ Knapper Umstieg", then red "⛔ Anschlussrisiko" — using the same predicates as the badge rendering (`tightTransfers` non-empty / any `unlikely`). Within a tier, cards order by the worst margin `max(medianDelay − transferMinutes)` over the journey's tight transfers, so the tightest connection sorts last; remaining ties keep departure order (stable sort). Past mode has no tight-transfer prediction, so it ranks by what actually happened: journeys with `missedTransfers` last, departure order otherwise. Sort selection survives mode and language switches like the existing modes.
- Playwright-verified on a real Tübingen→Zürich search (7 cards, mixed tiers): clicking the toggle reorders 0-tier cards before tight-transfer cards, no-risk cards keep departure order, tight cards order by worst margin (excesses −1, −1, 0, 0.5), DOM order matches `sortedJourneys()`, DE/EN labels correct, active state survives the language switch. No live journey with a red pill existed on any probed route (needs median delay > transfer + 30 min), so the red tier and past mode were verified by injecting synthetic journeys into `state` and asserting the sorted order (red worst-first-last, missed-connection journeys last). Only console noise was the Umami `/stats/api/send` CORS rejection, expected from a localhost origin.
- Cache-buster app.js v=46.

## 2026-07-26 — Fewest-transfers sort mode

- Fifth sort toggle "Wenigste Umstiege / Fewest transfers" next to departure/delay/price/risk: ascending by `journey.transfers` (bahn.de's `umstiegsAnzahl`, already in every API response; fallback = non-walking legs − 1, the same derivation the cards use), stable sort keeping departure order on ties. Generic `.sort-btn` click handler and `data-i18n` wiring picked the new button up with no further changes.
- Playwright-verified (CDP against the cached headless Chromium — the npm playwright's bundled revision didn't match the browser cache) on a real Tübingen→Zürich search restored via shareable URL: transfer counts [2,3,4,4,1,3,3] reorder to [1,2,3,3,3,4,4], ties keep departure order, card set unchanged, active-button state moves, EN label translates, switching back to Abfahrtszeit restores the original order, no console errors (localhost-only Umami CORS noise excluded).
- Cache-buster app.js v=47 (the branch and the transfer-risk sort both claimed v=46; re-bumped on merge).

## 2026-07-26 — Journey header fits on one line at desktop width

- On macOS the future-mode header still wrapped the `.journey-cta` block (badges + price + booking button) to a second line even at the full 900 px layout: San Francisco renders a few percent wider than the Linux fonts the layout was tuned against, and the row's content sat within ~10–30 px of the 828 px column. Reproduced headlessly by stress-testing with `letter-spacing: 0.6px` — German wrapped at 900 px, English just below. Tightened instead of restructured: `.journey-head` gap 10→8, `.journey-cta` gap 8→6, `.price` 17→16 px, `.book-btn` 14→13 px with padding 7/12→6/10 and `white-space: nowrap`. One line now holds at 900 px in DE and EN under the wide-font stress test; below ~860 px the block still wraps as one right-aligned unit (mobile fallback unchanged).
- Cache-buster style.css v=28.

## 2026-07-26 — Revert of the header tightening

- The one-line squeeze (previous entry) traded too much CTA presence for density — smaller booking button and price, tighter gaps — and was reverted wholesale: `.journey-head`/`.journey-cta` gaps back to 10/8 px, `.price` back to 17 px, `.book-btn` back to 14 px with 7/12 padding, cache-buster back to v=27 (v=28 was never deployed). The macOS wrap it fixed is back on the table; candidate next step is keeping these sizes and dropping the yellow tight-transfer pill from the header row instead (it duplicates the always-visible red callout under the leg and is the ~115 px that overflows the row).

## 2026-07-26 — Pipeline efficiency: day-incremental DE build, merge window bound, poller cleanup

- Deploy confirmed live (closes the 07-23 "still open" item): `delaybahn-pipeline.timer` runs the 4-step DE→CH→FR→merge flow at 05:30 and restarts the app; `delaybahn-fr-poller.service` is enabled. But the detached session poller from 07-20 (PID 211845) had kept running next to the systemd one since 07-25 — both fetching the SNCF feed every 120 s and upserting the same `obs.sqlite` — and was killed today; exactly one poller remains.
- `build_delay_db.py` no longer re-parses all 31 days nightly. That full re-parse (~4.1M XML responses, 4.76 GB) was ~39 min of the 40.5-min pipeline run, with only ~1 day of it new, because the parsed batches went to `data/temp_processing/` and were deleted after the merge. Each raw day is now parsed once into a persistent cache `data/de/parsed/<day>/{plan,fchg}/batch_*.parquet` (built in a `.tmp` dir, atomic rename, `os.utime` completion stamp) and re-parsed only when the day's newest raw file is newer than the cache — `snapshot_download` gives late-arriving files a fresh mtime, so late upstream uploads for an already-parsed day still trigger a re-parse. The dedup/merge SQL is unchanged but reads explicit per-day file lists, writes via `.parquet.tmp` + rename, and is skipped when no day changed and the window hasn't moved past the last build's calendar day (this replaces the old output-vs-raw mtime guard, which could never fire). Parsed days are pruned alongside the raw mirror. Expected nightly runtime: minutes instead of ~40; the first run after this change still parses everything once to seed the cache.
- `merge_delays.py`: symmetric lower window cut (`--window-days`, default 30 = the app's max stats window) on `time` and `arrival_planned_time`, mirroring the existing last-midnight upper cut. CH prunes at 40 days and FR at 41 vs DE's 30, so the merged parquet carried ~1.5M rows the app could never serve, and `/api/coverage` `minDay` came from the FR floor (2026-06-15 vs DE's 2026-06-26) — the past-mode date picker offered ~11 days that returned nothing for German journeys. Coverage now equals the servable window.
- Docs trued up: README window/size numbers (31 days / ~4.5 GB, was "8 days / ~8 GB") and the missing `merge_delays.py` step in setup; feature_list FR-poller and daily-refresh rows to done; progress.md deploy item moved from "Not done" to accomplishments.

## 2026-07-26 — Refund CTA moved below the hero chart

- The home-page refund CTA sat between the search card and the hero chart, pushing the site's headline claim below it. Reordered in static/index.html only: the `#refund-cta` button now renders after `#hero-chart`, directly above the (hidden) results controls. No content, style, or JS changes; the show/hide logic keys off the element id and is unaffected.

## 2026-07-26 — Risk sort ranks within tiers by the riskiest transfer's margin

- The "Geringstes Anschlussrisiko" sort kept departure order inside the no-risk tier and ordered the yellow/red tiers by worst excess over tight transfers only. Future-mode journeys now carry `minTransferMargin` — min(transfer time − arriving leg's median delay) across *all* train-to-train transfers (walking time subtracted, same `_transfer_pairs` helper the tight-transfer detection uses; null for direct journeys or when no arriving leg has delay stats) — and the risk sort orders within each tier by that margin descending: direct journeys first (no connection to miss), biggest slack next, journeys without delay data last, remaining ties keep departure order (stable sort). Past mode unchanged: it ranks by actual outcomes and keeps departure order within its made/missed tiers. Comparator unit-tested in node with synthetic journeys covering all three tiers plus direct/null-margin edge cases; not browser-verified.
- Cache-buster app.js v=48.
