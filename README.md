# DB Delay Predictor

[![Live at delaybahn.com](https://img.shields.io/badge/live-delaybahn.com-EC0016)](https://delaybahn.com)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009485?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![DuckDB](https://img.shields.io/badge/DuckDB-FFF000?logo=duckdb&logoColor=black)](https://duckdb.org/)

A bahn.de-style train connection search that shows the **median arrival delay of the last 7 days** (window selectable up to 30) for every connection, so you can book the one that actually runs on time. "Booking" deep-links to the real bahn.de page pre-filled with the journey.

## Features

- **Delay statistics per leg**: median arrival delay over the last 7/15/30 days, per-day charts, and the official IRIS delay causes behind every badge.
- **Three countries**: Germany, Switzerland, and France — cross-border journeys get statistics on every leg.
- **Transfer-risk warnings**: connections the arriving train's delay history makes tight or unlikely are flagged, and journeys can be sorted by connection risk.
- **Five sort modes**: departure time, least delay, cheapest price, lowest connection risk, fewest transfers.
- **Compensation checker**: reconstructs a past journey as it actually ran — exact delays, missed or cancelled connections, the replacement trains a passenger would have taken — and computes DB Fahrgastrechte compensation (25 %/50 %) with a deep link into the bahn.de claim flow.
- **Same-day results**: a journey is checkable minutes after arrival via live IRIS lookups, showing the same value the nightly build will store.
- **DE/EN interface**, shareable search URLs, station autocomplete served from the local delay data.

## How it works

- **Journey search**: the bahn.de web API (`www.bahn.de/web/api`) provides journey options including transfers and prices — the same API the bahn.de website uses. Station autocomplete is answered from the local delay data, falling back to that API for stations without delay history.
- **Historical delays, Germany**: the public HuggingFace dataset [piebro/deutsche-bahn-data](https://huggingface.co/datasets/piebro/deutsche-bahn-data) publishes raw Deutsche Bahn IRIS timetable responses every 6 hours. The pipeline keeps a rolling 31-day mirror and builds a per-stop delay table covering all German stations.
- **Historical delays, Switzerland and France**: official istdaten daily files (opentransportdata.swiss) and a 24/7 poller on the official SNCF GTFS-RT feed produce per-day tables in the same schema; `pipeline/merge_delays.py` unions all three countries into the served table.
- **Today's delays**: for a journey the nightly pipeline has not ingested yet, delays are read at request time from the [DB Timetables API](https://developers.deutschebahn.com/db-api-marketplace/apis/product/timetables) (IRIS `plan` + `fchg`) — the same field the pipeline stores, so the answer does not change later. Optional: set `DB_API_KEY`/`DB_CLIENT_ID` (e.g. in a `.env`); without them the site simply stops at the last ingested day.
- **Matching**: each train leg of a journey is matched against history by train number + arrival-station EVA + time-of-day proximity (±120 min), one closest match per calendar day. The median arrival delay at the leg destination is taken over the matched days; cancelled days are excluded from the median but counted.

## Setup

Prerequisites:

- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- git

### 1. Clone with the submodule

The delay pipeline imports the parser from the `deutsche-bahn-data` submodule, so it must be checked out:

```bash
git clone --recurse-submodules https://github.com/sha2nkt/db_delay_predictor.git
cd db_delay_predictor
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init
```

### 2. Install dependencies

```bash
uv sync
```

This creates `.venv/` and installs everything from `uv.lock` (FastAPI, DuckDB, pandas, huggingface-hub, ...).

### 3. Build the delay table

The app needs `data/delays.parquet` before it can show delay stats:

```bash
uv run python pipeline/build_delay_db.py            # full window (default: 31 days)
uv run python pipeline/build_delay_db.py --days 3   # quick smoke run
uv run python pipeline/merge_delays.py              # write the data/delays.parquet the app reads
```

This downloads raw parquet files from the HuggingFace dataset into `data/raw_data/` (~4.5 GB for the full window; `data/de/delays.parquet` adds another ~600 MB), parses each day once into the `data/de/parsed/` cache, and merges them into `data/de/delays.parquet`. Re-run it daily to stay fresh — already-downloaded days are skipped and only days with new raw files are re-parsed. No HuggingFace account or token is needed; the dataset is public. `merge_delays.py` then combines the per-country tables (DE alone is fine) into `data/delays.parquet`.

### 4. Run the app

```bash
uv run uvicorn app.main:app --port 8000
```

Open http://localhost:8000, search a connection (e.g. Berlin Hbf → München Hbf), sort by "Wenigste Verspätung".

## Layout

| Path | Purpose |
|---|---|
| `pipeline/build_delay_db.py` | HF download + XML parse → `data/de/delays.parquet` |
| `pipeline/build_ch_days.py`, `pipeline/fr_poller.py`, `pipeline/consolidate_fr.py` | Swiss and French per-day producers |
| `pipeline/merge_delays.py` | unions the per-country tables → `data/delays.parquet` |
| `app/bahn_api.py` | async client for the bahn.de web API |
| `app/delays.py` | DuckDB delay-stats lookup (the core matching query) |
| `app/live_delays.py` | live same-day lookups via the DB Timetables API |
| `app/main.py` | FastAPI endpoints `/api/locations`, `/api/journeys` + static serving |
| `static/` | vanilla HTML/CSS/JS frontend |
| `deutsche-bahn-data/` | git submodule: data collection project whose parser and dataset we reuse |
| `data/` | gitignored: raw parquet mirror + `delays.parquet` |

## Data sources & credits

- Germany: [piebro/deutsche-bahn-data](https://github.com/piebro/deutsche-bahn-data) by [Piet Brömmel](https://github.com/piebro) (DB IRIS timetable data), and the [DB Timetables API](https://developers.deutschebahn.com/db-api-marketplace/apis/product/timetables) for live same-day lookups.
- Switzerland: [opentransportdata.swiss](https://opentransportdata.swiss/) istdaten actual-data files.
- France: SNCF GTFS-RT via [transport.data.gouv.fr](https://transport.data.gouv.fr/datasets/horaires-sncf) (ODbL).

## Repo context for tooling and future work

- `feature_list.md` — what the product does, feature by feature, with status
- `progress.md` — current state snapshot, verification status, known limitations
- `log.md` — append-only change log (newest entry last; never rewrite old entries)
