import argparse
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta
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
    ("IT", "it/days/*.parquet", "083%"),
]


def _ntfy_topic() -> str | None:
    """NTFY_TOPIC from the environment, else from .env the way app/config.py reads it -
    the pipeline unit passes no environment, and only this key is wanted here (the
    rest of .env configures the app's own network paths, not this script's)."""
    if os.environ.get("NTFY_TOPIC"):
        return os.environ["NTFY_TOPIC"]
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "NTFY_TOPIC":
            return value.strip().strip("'\"") or None
    return None


def notify(title: str, body: str) -> None:
    """Push one ntfy message; a warning on stderr instead of an exception if it cannot,
    so the merge never fails because the notifier is unreachable."""
    topic = _ntfy_topic()
    if not topic:
        print("NTFY_TOPIC is unset: coverage warning not pushed", file=sys.stderr)
        return
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(
        f"{base}/{topic}", data=body.encode(), method="POST",
        headers={"Title": title, "Priority": "high", "Tags": "warning"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                print(f"ntfy push failed: HTTP {resp.status}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - any failure is a warning, never a merge failure
        print(f"ntfy push failed: {exc}", file=sys.stderr)


def check_coverage(out: Path, merged: list[tuple[str, str]], expected: date) -> list[str]:
    """Warn when any merged source stops short of `expected` (yesterday: every source
    delivers the previous day by the time the timer runs).

    The per-country steps run as ExecStart=- so one failing country cannot block the
    others, which also means a country can fail every night without the unit ever
    failing: on 2026-08-19..25 the DE build died daily while the merge kept shipping a
    stale DE parquet under a maxDay the other countries advanced, and every German
    leg lost its stats for a week with /health green. This is the alert that was
    missing. Returns the lagging lines (for the journal and for tests)."""
    sys.stdout.flush()  # the summary table precedes the verdict in the journal (stderr is unbuffered)
    newest = {
        prefix: day for prefix, day in duckdb.sql(f"""
            SELECT substr(eva, 1, 3), max(CAST(arrival_planned_time AS DATE))
            FROM '{out}' GROUP BY 1
        """).fetchall()
    }
    lagging = []
    for name, prefix in merged:
        day = newest.get(prefix.rstrip("%"))
        if day is None:
            lagging.append(f"{name}: no rows in the merged window")
        elif day < expected:
            lagging.append(f"{name}: newest day {day}, {(expected - day).days} day(s) behind {expected}")
    if not lagging:
        print(f"coverage: every source reaches {expected}")
        return lagging
    served = max((d for d in newest.values() if d is not None), default=None)
    body = "\n".join(lagging) + (
        f"\nThe app serves stats up to {served}; a lagging source has no rows in the newest"
        " days of the window. Check: journalctl -u delaybahn-pipeline -n 300 | grep -A12 Traceback"
    )
    print("coverage warning:\n" + body, file=sys.stderr)
    notify(f"delaybahn pipeline: {', '.join(line.split(':')[0] for line in lagging)} coverage lagging", body)
    return lagging


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
    merged = []  # (name, prefix) of every source that made it into the union
    for name, pattern, prefix in SOURCES:
        if not list(args.data_dir.glob(pattern)):
            print(f"{name}: no data at {args.data_dir / pattern}, skipping", file=sys.stderr)
            continue
        present = duckdb.sql(f"SELECT * FROM read_parquet('{args.data_dir / pattern}') LIMIT 0").columns
        optional = ", ".join(
            col if col in present else f"CAST(NULL AS {typ}) AS {col}"
            for col, typ in OPTIONAL_COLUMNS.items()
        )
        merged.append((name, prefix))
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
        SELECT CASE substr(eva, 2, 2) WHEN '80' THEN 'DE' WHEN '81' THEN 'AT' WHEN '83' THEN 'IT' WHEN '84' THEN 'NL' WHEN '85' THEN 'CH' WHEN '87' THEN 'FR' ELSE substr(eva, 2, 2) END AS country,
               count(*) AS rows_, count(DISTINCT CAST(arrival_planned_time AS DATE)) AS days_
        FROM '{out}' GROUP BY country ORDER BY country
    """).show()
    # yesterday in Berlin: the window ends at last midnight, and every source has the
    # previous day by the time the timer runs
    check_coverage(out, merged, (now - timedelta(days=1)).date())


if __name__ == "__main__":
    main()
