# Feature List

Status: done = implemented and verified end-to-end; partial = works with caveats; planned = not started.

## Search & journeys

| Feature | Status | Notes |
|---|---|---|
| Station autocomplete (Von/Nach) | done | `/api/locations` served from the local delay data (folded-name index built at startup, multi-level Hbf EVAs deduped, ranked prefix > word-start > substring then volume); bahn.de `/reiseloesung/orte` only for stations with no delay history; debounced 250 ms in UI |
| Typed-station fallback | partial | exact case-insensitive name match resolves un-selected input at search time; implemented 2026-07-19, browser verification pending |
| Journey search with transfers | done | `/api/journeys` → bahn.de `/angebote/fahrplan` (POST), 1 adult 2nd class, all products |
| Departure date/time selection | done | defaults to now; Berlin-local naive timestamps end to end |
| Swap origin/destination | done | ⇅ button |
| Ticket prices | done | `angebotsPreis` from bahn.de; prominent per-card display, "Preis auf bahn.de" fallback when missing |
| Sort by cheapest price | done | "Günstigster Preis" toggle; journeys without price last |
| Sort by transfer risk | done | "Geringstes Anschlussrisiko / Lowest connection risk" toggle; ranks by the header-pill tier (no risk < tight transfer < connection risk), within a tier by worst margin (median delay − transfer time), ties keep departure order; past mode ranks journeys with missed connections last; implemented and Playwright-verified 2026-07-26 |
| Shareable search URLs | done | search params synced to the query string, restored (and re-searched) on load; implemented 2026-07-19, browser-verified 2026-07-26 (Playwright loaded a query-string URL, search restored and results rendered) |
| Recent-station suggestions | partial | focusing an empty Von/Nach input suggests the last 6 searched stations (localStorage, deduped); implemented 2026-07-19, browser verification pending |

## Delay statistics

| Feature | Status | Notes |
|---|---|---|
| Median arrival delay per train leg | done | matched by train number + arrival EVA + time-of-day ±120 min, closest match per day; median since 2026-07-19 (was avg) |
| Journey-level delay score | done | median arrival delay of the final train leg (= delay at the passenger's destination); headline badge overridden by a red "Connection likely missed" pill when any transfer is unlikely (2026-07-22, browser-verified) |
| Worst-leg indicator (`maxLegMedianDelay`) | done | transfer-risk signal, used as sort tiebreaker |
| Per-day delay chart | done | per-day breakdown behind the delay badges (added 2026-07-15) |
| Delay reason on hover | done | IRIS delay-cause codes (`<m t="d" c="…"/>`) extracted into `reason_code` by the DE pipeline, mapped to the official German texts (EN translated) client-side; shown in the per-day chart tooltips (hover) and in a bubble over the bar on tap/click (touch-friendly) and past-mode badge tooltips (added 2026-07-25, browser-verified 2026-07-26); DE legs only — CH istdaten and FR GTFS-RT carry no cause data |
| Tight-transfer warning | done | flags transfers where the arriving leg's median delay leaves ≤ 2 min buffer (walking legs subtracted); shown as an inline red strip under the affected leg row with transfer time and the previous train's median delay; escalates to "⛔ Unwahrscheinlicher Umstieg / Unlikely transfer" when the median delay exceeds the transfer time by > 30 min (2026-07-22); any tight transfer also surfaces as a yellow "⚠ Knapper Umstieg / Tight transfer" pill in the journey header next to the median delay badge, red risk pill taking precedence (2026-07-26); implemented 2026-07-19 (inline since 2026-07-20), browser-verified 2026-07-26 (Playwright: strip + header pill render, pill height matches the delay badge, header actions wrap as one right-aligned block) |
| Cancellation tracking | done | cancelled days excluded from avg, surfaced as "N× (teil-)ausgefallen" note |
| Honest partial coverage | done | badge shows "n/7 Tage"; no data → gray "keine Daten", never a fake 0 |
| Color-coded badges | done | green < 3 min, yellow 3–9, red ≥ 10, gray no data |
| Sort by least delay | done | missing-data journeys last; journeys with a likely-missed connection after normal ones (2026-07-22); ties broken by worst leg |
| Swiss delay coverage | done | official istdaten v2 daily files; 31-day history from day one; all operators feeding SBB customer info (SBB/BLS/RhB/SOB verified) |
| French delay coverage | done | SNCF GTFS-RT poller + 35-day mirror backfill; TGV/Ouigo/TER/Intercités; "actual" = last realtime projection before arrival |
| Austrian delay coverage | planned | no per-stop open data; ÖBB HAFAS (Scotty) board polling is the identified path |

## Data pipeline

| Feature | Status | Notes |
|---|---|---|
| Download raw data from HuggingFace | done | `piebro/deutsche-bahn-data` dataset, rolling ~31-day window, no API key; skips existing files |
| Reprocess into per-stop delay table | done | reuses submodule parser; now writes `data/de/delays.parquet` (`--output`) |
| Swiss daily ingest | done | `build_ch_days.py`: scrapes the CKAN page for the rotating download URL, filters trains, per-day parquets, catch-up + prune |
| French 24/7 poller + consolidation | partial | `fr_poller.py` (running from session; systemd unit pending deploy) → `consolidate_fr.py` rewrites last 2 start_dates daily |
| French history backfill | done | `backfill_fr.py` from mirror.traines.eu tarballs (resumable, skip-if-exists); seam days 07-19..22 plugged 2026-07-25 |
| SNCF-UIC → DB-EVA crosswalk | done | `build_fr_crosswalk.py` → committed `config/fr_uic_to_eva.json` (3472/3534 stations; trainline seed + bahn.de `i=U×` token match) |
| Country merge | done | `merge_delays.py`: eva-prefix partition (080/085/087) + global last-midnight cut; tolerant of missing sources |
| Skip-if-fresh | done | reprocess only when raw data newer than output (`--force` overrides) |
| Scheduled daily refresh | partial | timer unchanged (05:30); pipeline unit still runs only `build_delay_db.py`, so the served `data/delays.parquet` went unwritten 07-20..07-25 until a manual catch-up — must be updated to the 4-step DE→CH→FR→merge flow (deploy pending, needs sudo) |
| Live same-day delay lookup | done | `app/live_delays.py`: IRIS `plan` + `fchg` at request time for days the parquet hasn't reached, same `ar/@ct` field the nightly build stores; measured 14–17 h lookback; DE only; inert without `DB_API_KEY`/`DB_CLIENT_ID` |

## Booking

| Feature | Status | Notes |
|---|---|---|
| Deep-link to bahn.de booking | done | pre-filled origin/destination/time, opens in new tab |
| Real in-app booking | not possible | no public booking API exists |

## Compensation checker (past journeys, 2026-07-23)

| Feature | Status | Notes |
|---|---|---|
| Past-journey mode | done | home-page-only CTA (hidden after future-mode results) flips the search card into `mode=past`; CTA and past-mode banner spell out that the journey is reconstructed as it actually ran, not just the refund (2026-07-25); "Entschädigung prüfen / Find my compensation" button; date picker clamped to `/api/coverage`, too-recent dates get a check-back-when message; shareable via `&mode=past`; header pill button "Entschädigung beantragen / Apply delay compensation" as an always-visible second entry point (2026-07-25) |
| Exact per-day leg delays | done | `leg_delay_on_date`: same matching as the median query, restricted to the searched calendar day; cancellations shown |
| Missed-connection simulation | done | journey walked with actual delays; transfer made only if the connecting train's actual departure (own delay included) leaves > 2 min; miss/cancellation → re-plan via bahn.de to the destination, ≤ 3 chained re-plans, earliest-actual-arrival candidate (delayed earlier trains considered) |
| Struck-out legs + actual continuation | done | missed legs struck out with "verpasst"/"ausgefallen" badges; "↳ Tatsächliche Weiterfahrt" section shows the replacement legs; header shows planned arrival struck + simulated actual |
| Compensation % + claim link | done | 25 % ≥ 60 min / 50 % ≥ 120 min vs booked planned arrival, from the simulated arrival; button → bahn.de/buchung/reiseuebersicht/vergangene, fallback link to the Fahrgastrechte form; disclaimer (ticket price basis, €4 minimum) |
| Same-day journeys (checkable on arrival) | done | date picker reaches today via `liveMaxDay` in `/api/coverage`; delays come from IRIS live (2026-07-25, verified minutes after the trains ran), re-planning after a missed connection resolves live too |
| Not-yet-reported legs | done | a leg IRIS hasn't reported shows "noch offen / pending" instead of "keine Daten", and the card says "Ankunft noch nicht bestätigt – morgen früh prüfen"; stops still in the future are never reported as on time |

## Site & SEO

| Feature | Status | Notes |
|---|---|---|
| SEO basics | done | meta description, canonical, Open Graph/Twitter tags, JSON-LD WebApplication (de/en) in index.html; robots.txt disallows /api/ and /stats/; single-URL sitemap.xml (added 2026-07-26) |

## Known limitations

- Delay stats are per-train-number history; a rescheduled or renumbered train shows "keine Daten".
- Walking legs and vehicles without a train number (some buses) get no badge.
- Journey search covers what bahn.de returns (6 results per query, no pagination yet).
- bahn.de web API is unofficial and could change without notice; it is bot-protected by Akamai, which blocks one TLS fingerprint at a time (the app rotates through firefox/safari/chrome profiles on a 403, but a simultaneous block of all three would take the search down).
- France: Trenitalia France and other non-SNCF operators are absent from the feed; "actual" times are the last realtime projection, not measured; poller downtime creates permanent holes for those hours.
- Switzerland: GESCHAETZT (estimated) actuals are accepted alongside REAL; foreign stops of international trains carry no Swiss actuals (each country's own source covers its own stations).
- Austria not covered yet — Austrian legs show "keine Daten" as before.
- Compensation checker: reaches back only as far as the live parquet (~30 days) while DB accepts claims up to 1 year; monthly archives are not wired up yet. The recent end is covered live, so the gap is at the far end only.
- Live same-day lookups are German only (IRIS has no CH/FR stops) and need `DB_API_KEY`/`DB_CLIENT_ID`; without credentials the date picker stops at the parquet's last day, exactly as before. S-Bahn legs where bahn.de sends a line label instead of a Zugnummer stay unmatched, live and in the parquet alike.
- Simulation assumes a rational passenger taking the earliest-arriving catchable connection; replacement legs without delay data count as on time; the DB claim page lists only journeys booked in that bahn.de account (form fallback linked).
