# DB Delay Predictor

A bahn.com-style train connection search that shows the **average arrival delay of the last 7 days** for every connection, so you can book the one with the least delay. "Booking" deep-links to the real bahn.de page pre-filled with the journey.

## How it works

- **Journey search**: the bahn.de web API (`www.bahn.de/web/api`) provides journey options including transfers and prices — the same API the bahn.de website uses. Station autocomplete is answered from the local delay data, falling back to that API for stations without delay history.
- **Today's delays**: for a journey the nightly pipeline has not ingested yet, delays are read at request time from the [DB Timetables API](https://developers.deutschebahn.com/db-api-marketplace/apis/product/timetables) (IRIS `plan` + `fchg`) — the same field the pipeline stores, so the answer does not change later. Optional: set `DB_API_KEY`/`DB_CLIENT_ID` (e.g. in a `.env`); without them the site simply stops at the last ingested day.
- **Historical delays**: the public HuggingFace dataset [piebro/deutsche-bahn-data](https://huggingface.co/datasets/piebro/deutsche-bahn-data) publishes raw Deutsche Bahn IRIS timetable responses every 6 hours. The pipeline downloads the last 31 days, reuses the parser from the [deutsche-bahn-data](deutsche-bahn-data/) submodule, and builds a per-stop delay table (`data/delays.parquet`, ~3M rows/week, all German stations).
- **Matching**: each train leg of a journey is matched against history by train number + arrival-station EVA + time-of-day proximity (±120 min), one closest match per calendar day. Arrival delay at the leg destination is averaged over the matched days; cancelled days are excluded from the average but counted.

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
| `pipeline/build_delay_db.py` | HF download + XML reprocess → `data/delays.parquet` |
| `app/bahn_api.py` | async client for the bahn.de web API |
| `app/delays.py` | DuckDB delay-stats lookup (the core matching query) |
| `app/main.py` | FastAPI endpoints `/api/locations`, `/api/journeys` + static serving |
| `static/` | vanilla HTML/CSS/JS frontend |
| `deutsche-bahn-data/` | git submodule: data collection project whose parser and dataset we reuse |
| `data/` | gitignored: raw parquet mirror + `delays.parquet` |

## Credits

Historical delay data comes from [piebro/deutsche-bahn-data](https://github.com/piebro/deutsche-bahn-data) by [Piet Brömmel](https://github.com/piebro).

## Repo context for tooling and future work

- `feature_list.md` — what the product does, feature by feature, with status
- `progress.md` — current state snapshot, verification status, known limitations
- `log.md` — append-only change log (newest entry last; never rewrite old entries)
