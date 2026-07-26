import argparse
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
from huggingface_hub import snapshot_download

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUBMODULE_ROOT = PROJECT_ROOT / "deutsche-bahn-data"
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.fchg_parse import process_files_to_temp  # noqa: E402

HF_REPO = "piebro/deutsche-bahn-data"


def get_window_parquet_files(raw_dir: Path, dates: list[date]) -> list[Path]:
    parquet_files = []
    for d in dates:
        day_dir = raw_dir / f"year={d.year}" / f"month={d.month}" / f"day={d.day}"
        parquet_files.extend(day_dir.glob("*.parquet"))

    def sort_key(path: Path):
        parts = {p.split("=")[0]: int(p.split("=")[1]) for p in path.parts if "=" in p}
        return (parts.get("year", 0), parts.get("month", 0), parts.get("day", 0), path.name)

    return sorted(parquet_files, key=sort_key)


def prune_old_raw_days(raw_dir: Path, keep: set[date]):
    """Delete mirrored day dirs outside the current window so the mirror doesn't grow unboundedly."""
    for day_dir in sorted(raw_dir.glob("year=*/month=*/day=*")):
        parts = {p.split("=")[0]: int(p.split("=")[1]) for p in day_dir.parts if "=" in p}
        d = date(parts["year"], parts["month"], parts["day"])
        if d not in keep:
            shutil.rmtree(day_dir)
            print(f"Pruned old raw day {d}")


def main():
    parser = argparse.ArgumentParser(description="Download raw DB delay data from HuggingFace and build data/delays.parquet")
    parser.add_argument("--days", type=int, default=31, help="days of raw data to use; the oldest only catches cross-midnight trains (default: 31 = 30 full days)")
    parser.add_argument("--end-date", type=lambda s: date.fromisoformat(s), default=None, help="last day of the window, YYYY-MM-DD (default: yesterday in Europe/Berlin)")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help="directory for raw data mirror and output parquet")
    parser.add_argument("--output", type=Path, default=None, help="output parquet path (default: <data-dir>/de/delays.parquet)")
    parser.add_argument("--force", action="store_true", help="reprocess even if delays.parquet is newer than the newest raw file")
    args = parser.parse_args()

    end_date = args.end_date or (datetime.now(ZoneInfo("Europe/Berlin")).date() - timedelta(days=1))
    dates = [end_date - timedelta(days=d) for d in range(args.days)]
    # oldest downloaded day is boundary-only: filter window starts one day after it
    window_start = end_date - timedelta(days=args.days - 2)
    window_end = end_date + timedelta(days=1)

    args.data_dir.mkdir(parents=True, exist_ok=True)
    output_file = args.output or args.data_dir / "de" / "delays.parquet"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"Window: {window_start} .. {end_date} (downloading {len(dates)} days incl. boundary day)")
    snapshot_download(
        repo_id=HF_REPO,
        repo_type="dataset",
        allow_patterns=[f"raw_data/year={d.year}/month={d.month}/day={d.day}/*" for d in dates],
        local_dir=args.data_dir,
    )

    parquet_files = get_window_parquet_files(args.data_dir / "raw_data", dates)
    if not parquet_files:
        sys.exit("No raw parquet files found for the requested window.")
    total_bytes = sum(f.stat().st_size for f in parquet_files)
    print(f"{len(parquet_files)} raw files, {total_bytes / 1e9:.2f} GB")

    newest_raw = max(f.stat().st_mtime for f in parquet_files)
    if not args.force and output_file.exists() and output_file.stat().st_mtime > newest_raw:
        print(f"{output_file} is up to date, skipping (use --force to reprocess)")
        return

    eva_to_station = json.load(open(SUBMODULE_ROOT / "config" / "eva_to_station_name.json"))
    temp_dir = args.data_dir / "temp_processing"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    xml_count, plan_count, fchg_count = process_files_to_temp(parquet_files, eva_to_station, temp_dir)
    print(f"Parsed {xml_count:_} xml responses -> {plan_count:_} plan rows, {fchg_count:_} fchg rows")

    plan_pattern = str(temp_dir / "plan" / "*.parquet")
    fchg_pattern = str(temp_dir / "fchg" / "*.parquet")

    # merge/dedup SQL adapted from deutsche-bahn-data/scripts/create_monthly_data_release.py main()
    # (rolling window bounds instead of hardcoded month)
    duckdb.sql(f"""
        COPY (
            WITH plan_deduped AS (
                SELECT DISTINCT ON (id)
                    id, station_name, xml_station_name, eva, train_number, line_number,
                    final_destination_station, train_type, arrival_planned_time, departure_planned_time
                FROM '{plan_pattern}'
                ORDER BY id, xml_timestamp DESC
            ),
            fchg_deduped AS (
                SELECT DISTINCT ON (id)
                    id, arrival_change_time, departure_change_time, is_canceled
                FROM '{fchg_pattern}'
                ORDER BY id, xml_timestamp DESC
            ),
            -- latest delay-cause message per stop, independent of the newest
            -- fchg response (which may no longer carry the message)
            reasons AS (
                SELECT id, arg_max(reason_code, reason_ts) AS reason_code
                FROM '{fchg_pattern}'
                WHERE reason_code IS NOT NULL
                GROUP BY id
            ),
            merged AS (
                SELECT
                    p.*,
                    COALESCE(f.arrival_change_time, p.arrival_planned_time) AS arrival_change_time,
                    COALESCE(f.departure_change_time, p.departure_planned_time) AS departure_change_time,
                    COALESCE(f.is_canceled, false) AS is_canceled,
                    r.reason_code
                FROM plan_deduped p
                LEFT JOIN fchg_deduped f ON p.id = f.id
                LEFT JOIN reasons r ON p.id = r.id
            ),
            transformed AS (
                SELECT
                    station_name, xml_station_name, eva, train_number, line_number,
                    final_destination_station,
                    CAST(COALESCE(
                        date_diff('minute', departure_planned_time, departure_change_time),
                        date_diff('minute', arrival_planned_time, arrival_change_time)
                    ) AS INTEGER) AS delay_in_min,
                    COALESCE(departure_change_time, arrival_change_time) AS time,
                    is_canceled, train_type,
                    regexp_extract(id, '^(.*)-\\d{{10}}-\\d+$', 1) AS train_line_ride_id,
                    CAST(split_part(id, '-', -1) AS INTEGER) AS train_line_station_num,
                    arrival_planned_time, arrival_change_time,
                    departure_planned_time, departure_change_time, id,
                    CAST(reason_code AS INTEGER) AS reason_code
                FROM merged
                ORDER BY time
            )
            SELECT * FROM transformed
            WHERE time >= TIMESTAMP '{window_start} 00:00:00'
                AND time < TIMESTAMP '{window_end} 00:00:00'
        ) TO '{output_file}' (FORMAT PARQUET)
    """)
    shutil.rmtree(temp_dir)
    prune_old_raw_days(args.data_dir / "raw_data", set(dates))

    print(f"Saved {output_file}")
    duckdb.sql(f"""
        SELECT CAST(time AS DATE) AS day, count(*) AS stops,
               round(avg(delay_in_min), 2) AS avg_delay_min
        FROM '{output_file}' GROUP BY day ORDER BY day
    """).show()


if __name__ == "__main__":
    main()
