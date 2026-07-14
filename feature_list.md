# Feature List

Status: done = implemented and verified end-to-end; partial = works with caveats; planned = not started.

## Search & journeys

| Feature | Status | Notes |
|---|---|---|
| Station autocomplete (Von/Nach) | done | `/api/locations` → bahn.de `/reiseloesung/orte`, debounced 250 ms in UI |
| Journey search with transfers | done | `/api/journeys` → bahn.de `/angebote/fahrplan` (POST), 1 adult 2nd class, all products |
| Departure date/time selection | done | defaults to now; Berlin-local naive timestamps end to end |
| Swap origin/destination | done | ⇅ button |
| Ticket prices | done | `angebotsPreis` from bahn.de; prominent per-card display, "Preis auf bahn.de" fallback when missing |
| Sort by cheapest price | done | "Günstigster Preis" toggle; journeys without price last |

## Delay statistics

| Feature | Status | Notes |
|---|---|---|
| 7-day avg arrival delay per train leg | done | matched by train number + arrival EVA + time-of-day ±120 min, closest match per day |
| Journey-level delay score | done | avg arrival delay of the final train leg (= delay at the passenger's destination) |
| Worst-leg indicator (`maxLegAvgDelay`) | done | transfer-risk signal, used as sort tiebreaker |
| Cancellation tracking | done | cancelled days excluded from avg, surfaced as "N× (teil-)ausgefallen" note |
| Honest partial coverage | done | badge shows "n/7 Tage"; no data → gray "keine Daten", never a fake 0 |
| Color-coded badges | done | green < 3 min, yellow 3–9, red ≥ 10, gray no data |
| Sort by least delay | done | missing-data journeys last; ties broken by worst leg |

## Data pipeline

| Feature | Status | Notes |
|---|---|---|
| Download raw data from HuggingFace | done | `piebro/deutsche-bahn-data` dataset, rolling ~31-day window, no API key; skips existing files |
| Reprocess into per-stop delay table | done | reuses submodule parser; `data/delays.parquet` |
| Skip-if-fresh | done | reprocess only when raw data newer than output (`--force` overrides) |
| Scheduled daily refresh | done | systemd timer `delaybahn-pipeline.timer` on ps083, daily 05:30 Europe/Berlin, restarts the app service after the build |

## Booking

| Feature | Status | Notes |
|---|---|---|
| Deep-link to bahn.de booking | done | pre-filled origin/destination/time, opens in new tab |
| Real in-app booking | not possible | no public booking API exists |

## Known limitations

- Delay stats are per-train-number history; a rescheduled or renumbered train shows "keine Daten".
- Walking legs and vehicles without a train number (some buses) get no badge.
- Journey search covers what bahn.de returns (6 results per query, no pagination yet).
- bahn.de web API is unofficial and could change without notice.
