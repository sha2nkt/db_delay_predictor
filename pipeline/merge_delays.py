import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.delays import build_db_file  # import needs the sys.path insert above

COLUMNS = (
    "station_name, xml_station_name, eva, train_number, line_number,"
    " final_destination_station, delay_in_min, time, is_canceled, train_type,"
    " train_line_ride_id, train_line_station_num, arrival_planned_time,"
    " arrival_change_time, departure_planned_time, departure_change_time, id"
)

# only the DE (IRIS) build carries it; CH istdaten and FR GTFS-RT have no cause data
OPTIONAL_COLUMNS = {"reason_code": "INTEGER"}

# country partition on the padded eva prefix keeps sources from ever colliding
# (e.g. IRIS carries foreign border stops like Basel SBB that CH data also has)
SOURCES = [
    ("DE", "de/delays.parquet", "080%"),
    ("AT", "at/days/*.parquet", "081%"),
    ("NL", "nl/days/*.parquet", "084%"),
    ("CH", "ch/days/*.parquet", "085%"),
    ("FR", "fr/days/*.parquet", "087%"),
]


def main():
    parser = argparse.ArgumentParser(description="Merge per-country delay parquets into the single table the app reads")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help="base data directory")
    parser.add_argument("--out", type=Path, default=None, help="output parquet (default: <data-dir>/delays.parquet)")
    parser.add_argument("--window-days", type=int, default=30, help="days of data to keep, matching the app's max stats window (default: 30)")
    args = parser.parse_args()

    out = args.out or args.data_dir / "delays.parquet"
    # cut at last midnight, like the DE build's own window end: interior days keep
    # their cross-midnight tails, but no source may push the stats window anchor
    # (max arrival_planned_time) past what all countries have data for
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    window_end = now.strftime("%Y-%m-%d 00:00:00")
    # symmetric lower cut: CH/FR retain more days on disk than the app's stats
    # window can ever serve; without this the merged table carries dead rows and
    # /api/coverage advertises days that are empty for DE journeys
    window_start = (now - timedelta(days=args.window_days)).strftime("%Y-%m-%d 00:00:00")
    selects = []
    for name, pattern, prefix in SOURCES:
        if not list(args.data_dir.glob(pattern)):
            print(f"{name}: no data at {args.data_dir / pattern}, skipping", file=sys.stderr)
            continue
        present = duckdb.sql(f"SELECT * FROM read_parquet('{args.data_dir / pattern}') LIMIT 0").columns
        optional = ", ".join(
            col if col in present else f"CAST(NULL AS {typ}) AS {col}"
            for col, typ in OPTIONAL_COLUMNS.items()
        )
        selects.append(
            f"SELECT {COLUMNS}, {optional} FROM read_parquet('{args.data_dir / pattern}')"
            f" WHERE eva LIKE '{prefix}'"
            f" AND time >= TIMESTAMP '{window_start}' AND time < TIMESTAMP '{window_end}'"
            # arrival_planned_time drives the app's window anchors (_min_day/_max_day);
            # bound it on both sides so coverage is exactly the servable window
            f" AND (arrival_planned_time IS NULL OR (arrival_planned_time >= TIMESTAMP '{window_start}'"
            f" AND arrival_planned_time < TIMESTAMP '{window_end}'))"
        )
    if not selects:
        sys.exit("No source data found - nothing to merge")

    tmp = out.with_suffix(".parquet.tmp")
    duckdb.sql(f"COPY ({' UNION ALL '.join(selects)}) TO '{tmp}' (FORMAT PARQUET)")
    # materialize the sorted DuckDB file the app opens directly — from the staged
    # parquet, before swapping it in: a failed build must leave parquet and db
    # consistent (both old), or a later app restart silently serves stale data.
    # The parquet stays the exchange/backup format (and the fallback for checkouts
    # without a db file).
    db_out = out.with_suffix(".duckdb")
    build_db_file(tmp, db_out)
    os.replace(tmp, out)
    print(f"Saved {out} and {db_out}")
    duckdb.sql(f"""
        SELECT CASE substr(eva, 2, 2) WHEN '80' THEN 'DE' WHEN '81' THEN 'AT' WHEN '84' THEN 'NL' WHEN '85' THEN 'CH' WHEN '87' THEN 'FR' ELSE substr(eva, 2, 2) END AS country,
               count(*) AS rows_, count(DISTINCT CAST(arrival_planned_time AS DATE)) AS days_
        FROM '{out}' GROUP BY country ORDER BY country
    """).show()


if __name__ == "__main__":
    main()
