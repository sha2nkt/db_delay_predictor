# Feature List

Status: done = implemented and verified end-to-end; partial = works with caveats; planned = not started.

## Search & journeys

| Feature | Status | Notes |
|---|---|---|
| Station autocomplete (Von/Nach) | done | `/api/locations` served from the local delay data (folded-name index built at startup, multi-level Hbf EVAs deduped, ranked prefix > word-start > substring then volume); bahn.de `/reiseloesung/orte` only for stations with no delay history; debounced 250 ms in UI |
| Typed-station fallback | partial | exact case-insensitive name match resolves un-selected input at search time; implemented 2026-07-19, browser verification pending |
| Journey search with transfers | done | `/api/journeys` → bahn.de `/angebote/fahrplan` (POST), 1 adult 2nd class, all products |
| Earlier/later paging | done | bahn.de `verbindungReference` tokens; buttons prepend/append the adjacent result page, deduped by planned times + train names (2026-07-12) |
| Departure date/time selection | done | defaults to now; Berlin-local naive timestamps end to end; iOS Safari overflow of the native date/time controls fixed by stripping `-webkit-appearance` + left-aligning the value (2026-08-02, on-device re-check pending) |
| Swap origin/destination | done | ⇅ button |
| Ticket prices | done | `angebotsPreis` from bahn.de; prominent per-card display, "Preis auf bahn.de" fallback when missing |
| Sort by cheapest price | done | "Günstigster Preis" toggle; journeys without price last |
| Sort by transfer risk | done | "Geringstes Anschlussrisiko / Lowest connection risk" toggle; ranks by the header-pill tier (no risk < tight transfer < connection risk), within a tier by the riskiest transfer's slack min(transfer time − median delay) over all transfers descending (`minTransferMargin`, since 2026-07-26; direct journeys first, journeys without delay data last), ties keep departure order; past mode ranks journeys with missed connections last; implemented and Playwright-verified 2026-07-26 (within-tier margin rework comparator-tested only) |
| Sort by fewest transfers | done | "Wenigste Umstiege" toggle; ascending by bahn.de `umstiegsAnzahl` (non-walking legs − 1 fallback), ties keep departure order; added + browser-verified 2026-07-26 |
| Shareable search URLs | done | search params synced to the query string, restored (and re-searched) on load; implemented 2026-07-19, browser-verified 2026-07-26 (Playwright loaded a query-string URL, search restored and results rendered) |
| Recent-station suggestions | partial | focusing an empty Von/Nach input suggests the last 6 searched stations (localStorage, deduped); implemented 2026-07-19, browser verification pending |
| Live (echtzeit) times on today's connections | done | bahn.de real-time departure/arrival shown where it deviates from the schedule: struck-out planned time + red live time (journey header and leg rows, tooltip explains), header duration switches to the re-planned `ezVerbindungsDauerInSeconds`; transfer-feasibility gaps computed from live times; past mode stays schedule-only; user-verified in browser (2026-08-03) |
| Leg timeline rail | done | vertical rail on each journey card: a dark-ringed circle per train leg joined by a gray line so connections read at a glance; walks sit on the bare line, tight-transfer strips draw a pass-through segment, past-mode continuation starts its own rail run, direct journeys show a lone circle; CSS pseudo-elements only; pixel-scan-verified in headless Chrome at desktop + 400 px (2026-08-02); circles anchor to each row's first line instead of 50 % height, fixing drift on wrapped mobile rows (2026-08-02) |

## Delay statistics

| Feature | Status | Notes |
|---|---|---|
| Median arrival delay per train leg | done | matched by train number + arrival EVA + time-of-day ±120 min, closest match per day; median since 2026-07-19 (was avg) |
| Journey-level delay score | done | median arrival delay of the final train leg (= delay at the passenger's destination); headline badge overridden by a red "⛔ Anschlussrisiko / Connection risk" pill when any transfer is unlikely (2026-07-22, browser-verified) |
| Worst-leg indicator (`maxLegMedianDelay`) | done | transfer-risk signal, used as sort tiebreaker |
| Per-day delay chart | done | per-day breakdown behind the delay badges (added 2026-07-15); clickable badges show a full-size ▾ disclosure caret (rotates while open) + mobile tap-target/press feedback after user feedback that the feature was undiscoverable (2026-08-02, Playwright-verified) |
| Delay reason on hover | done | IRIS delay-cause codes extracted into `reason_code` by the DE pipeline, mapped client-side to the official cause texts (DE/EN); shown in per-day chart tooltips, in a tap/click bubble over the bar (touch), and in past-mode badge tooltips incl. live legs; DE legs only — CH istdaten and FR GTFS-RT carry no cause data (added + browser-verified 2026-07-26) |
| Tight-transfer warning | done | flags transfers where the arriving leg's median delay leaves ≤ 2 min buffer (walking legs subtracted): inline red strip under the leg row with transfer time and the previous train's median delay; escalates to "⛔ Unwahrscheinlicher Umstieg / Unlikely transfer" when the median delay exceeds the transfer time by > 30 min (2026-07-22); any tight transfer also adds a yellow "⚠ Knapper Umstieg / Tight transfer" pill in the journey header, red risk pill taking precedence (2026-07-26), and suppresses the median delay badge unless it is red (2026-07-27, not browser-verified); browser-verified 2026-07-26 |
| Cancellation tracking | done | cancelled days excluded from the median, surfaced as "N× (teil-)ausgefallen" note |
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
| French 24/7 poller + consolidation | done | `fr_poller.py` under `delaybahn-fr-poller.service` (enabled 2026-07-25) → `consolidate_fr.py` rewrites last 2 start_dates daily |
| French history backfill | done | `backfill_fr.py` from mirror.traines.eu tarballs (resumable, skip-if-exists); seam days 07-19..22 plugged 2026-07-25 |
| SNCF-UIC → DB-EVA crosswalk | done | `build_fr_crosswalk.py` → committed `config/fr_uic_to_eva.json` (3472/3534 stations; trainline seed + bahn.de `i=U×` token match) |
| Country merge | done | `merge_delays.py`: eva-prefix partition (080/085/087) + global last-midnight cut; tolerant of missing sources |
| Skip-if-fresh | done | per-day parsed cache (`data/de/parsed/`); only days with new raw files are re-parsed, final merge skipped when nothing changed (`--force` overrides) |
| Scheduled daily refresh | done | 05:30 timer runs the 4-step DE→CH→FR→merge flow then restarts the app (units deployed 2026-07-25/26) |
| Live same-day delay lookup | done | `app/live_delays.py`: IRIS `plan` + `fchg` at request time for days the parquet hasn't reached, same `ar/@ct` field the nightly build stores; measured 14–17 h lookback; DE only; inert without `DB_API_KEY`/`DB_CLIENT_ID` |

## Booking

| Feature | Status | Notes |
|---|---|---|
| Deep-link to bahn.de booking | done | pre-filled origin/destination/time, opens in new tab |
| Real in-app booking | not possible | no public booking API exists |

## Compensation checker (past journeys, 2026-07-23)

| Feature | Status | Notes |
|---|---|---|
| Past-journey mode | done | home-page-only CTA (below the hero chart since 2026-07-26, hidden after future-mode results) or the permanent header pill "Entschädigung beantragen / Apply delay compensation" (2026-07-25) flips the search card into `mode=past`; CTA and banner spell out that the journey is reconstructed as it actually ran (2026-07-25); date picker clamped to `/api/coverage`, too-recent dates get a check-back-when message; shareable via `&mode=past` |
| Exact per-day leg delays | done | `leg_delay_on_date`: same matching as the median query, restricted to the searched calendar day; cancellations shown |
| Missed-connection simulation | done | journey walked with actual delays; transfer made only if the connecting train's actual departure (own delay included) leaves > 2 min; miss/cancellation → re-plan via bahn.de to the destination, ≤ 3 chained re-plans, earliest-actual-arrival candidate (delayed earlier trains considered) |
| Struck-out legs + actual continuation | done | missed legs struck out with "verpasst"/"ausgefallen" badges; "↳ Tatsächliche Weiterfahrt" section shows the replacement legs; header shows planned arrival struck + simulated actual |
| Compensation % + claim link | done | 25 % ≥ 60 min / 50 % ≥ 120 min vs booked planned arrival, from the simulated arrival; the claim button opens a claim-steps modal (2026-07-31, headless-Chrome-verified DE/EN + mobile): the journey as shown in the bahn.de past-trips list + DE/EN replicas of the buttons to click, continue link → bahn.de/buchung/reiseuebersicht/vergangene, Fahrgastrechte-form fallback inside the modal; disclaimer (ticket price basis, €4 minimum) |
| Same-day journeys (checkable on arrival) | done | date picker reaches today via `liveMaxDay` in `/api/coverage`; delays come from IRIS live (2026-07-25, verified minutes after the trains ran), re-planning after a missed connection resolves live too |
| Not-yet-reported legs | done | a leg IRIS hasn't reported shows "noch offen / pending" instead of "keine Daten", and the card says "Ankunft noch nicht bestätigt – morgen früh prüfen"; stops still in the future are never reported as on time |

## Site & SEO

| Feature | Status | Notes |
|---|---|---|
| Site-wide DE/EN toggle | done | header pills; static text via `data-i18n`, dynamic strings via the `I18N` dict; choice persists in localStorage (2026-07-13) |
| Hero chart | done | two-line claim in-page ("Verspätete Züge bleiben verspätet…"), plot behind a "Daten ansehen" toggle (2026-07-25) |
| SEO basics | done | meta description, canonical, Open Graph/Twitter tags, JSON-LD WebApplication (de/en) in index.html; robots.txt disallows /api/ and /stats/; single-URL sitemap.xml (added 2026-07-26) |
| Impressum & Datenschutz page | done | static impressum.html (§ 5 DDG identification, § 18 MStV, DB-independence note, liability disclaimer, DSGVO privacy policy: Cloudflare, server logs, self-hosted Umami, server-side bahn.de proxying, localStorage); footer-linked DE/EN, noindex; contact kontakt@delaybahn.com via Cloudflare Email Routing (2026-07-27) |
| Ko-fi donate link | disabled | plain links to ko-fi.com/delaybahn (no widget/script): muted footer link + one-line "Hat dir das geholfen?" nudge under the results, shown only after a successful search, hidden on search start/mode switch/empty results; `track("donate", {placement})` Umami events; one ask per view — the footer link hides while the nudge shows (`setDonateNudge` + `body.nudge-on`); Playwright-verified incl. tracking payloads (2026-07-27); currently off via `DONATE_ENABLED = false` kill switch in app.js, markup/i18n/CSS/tracking left intact (2026-07-28) |
| Toy-train footer scene | done | full-width CSS/SVG strip above the footer: cartoon loco + coaches with synced spinning wheels roll in, 1.5 s stop at a centered station under a red "+75 min" board, then off; hills/trees/signal/sun/clouds/birds; language-neutral, `aria-hidden`, `prefers-reduced-motion` static pose; headless-Chrome-verified desktop + mobile (2026-07-31) |

## Known limitations

- Delay stats are per-train-number history; a rescheduled or renumbered train shows "keine Daten".
- `delays._cache` keys on `(train, eva, window)` while the SQL also filters on time-of-day, so two same-numbered trains calling at one station at different hours can return each other's stats; `_date_cache`/`_dep_date_cache` have the same gap within a day. Deterministic tie-breaking (2026-07-27) fixed result *stability* across rebuilds, not this.
- Load testing runs against recorded fixtures (`pipeline/loadtest_stub.py`); real bahn.de and IRIS behaviour under load is unmeasured by design, and the origin's own capacity is only as good as the upstream latency the stub simulates (250 ms default).
- Walking legs get no badge. U-Bahn, tram, city-bus and ferry legs show "nicht erfasst" — IRIS has no data for them (parquet `train_type='Bus'` rows are SEV rail-replacement only, keyed by run number, and municipal stops carry 6-digit non-EVA ids). Sourcing them would need GTFS-RT (DELFI) plus a stop-id crosswalk; deferred.
- On cards with the tight-transfer pill, a non-red median badge is hidden, and with it that card's badge-click day chart.
- Journey search covers what bahn.de returns (~6 connections per page; earlier/later buttons fetch adjacent pages).
- bahn.de web API is unofficial and could change without notice; it is bot-protected by Akamai, which blocks one TLS fingerprint at a time (the app rotates through firefox/safari/chrome profiles on a 403, but a simultaneous block of all three would take the search down).
- France: Trenitalia France and other non-SNCF operators are absent from the feed; "actual" times are the last realtime projection, not measured; poller downtime creates permanent holes for those hours.
- Switzerland: GESCHAETZT (estimated) actuals are accepted alongside REAL; foreign stops of international trains carry no Swiss actuals (each country's own source covers its own stations).
- Austria not covered yet — Austrian legs show "keine Daten" as before.
- Compensation checker: reaches back only as far as the live parquet (~30 days) while DB accepts claims up to 1 year; monthly archives are not wired up yet. The recent end is covered live, so the gap is at the far end only.
- Live same-day lookups are German only (IRIS has no CH/FR stops) and need `DB_API_KEY`/`DB_CLIENT_ID`; without credentials the date picker stops at the parquet's last day, exactly as before. German S-Bahn legs match by line label ("S5") since 2026-08-02 — nearest run of that line within the time-of-day tolerance, which for a headway service is the intended proxy, not an exact-run match.
- Simulation assumes a rational passenger taking the earliest-arriving catchable connection; replacement legs without delay data count as on time; the DB claim page lists only journeys booked in that bahn.de account (form fallback linked).
- The claim modal's bahn.de button labels are best-effort recreations, not captured from the live bahn.de UI; if DB renames its buttons the modal needs a one-line i18n update.
