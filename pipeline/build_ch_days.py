import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import duckdb
import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET_PAGE = "https://data.opentransportdata.swiss/en/dataset/ist-daten-v2"


def discover_day_urls() -> dict[date, str]:
    """Map operating day -> CSV download URL by scraping the CKAN dataset page
    (resource UUIDs change daily; the CKAN action API rejects anonymous calls)."""
    resp = httpx.get(DATASET_PAGE, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    urls = {}
    for href, day in re.findall(
        r'href="([^"]*?/download/(\d{4}-\d{2}-\d{2})_istdaten\.csv)"', resp.text
    ):
        urls[date.fromisoformat(day)] = urljoin(DATASET_PAGE, href)
    return urls


def download(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
    os.replace(tmp, dest)


def normalize_day(raw_csv: Path, out_parquet: Path):
    """Filter the all-modes istdaten CSV down to train stops with usable arrival
    actuals (or cancellations) and write the site's 17-column delay schema.
    Cross-midnight rows are kept; merge_delays.py applies the global window cut."""
    out_parquet.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_parquet.with_suffix(".parquet.tmp")
    duckdb.sql(f"""
        COPY (
            SELECT
                CAST(HALTESTELLEN_NAME AS VARCHAR) AS station_name,
                CAST(NULL AS VARCHAR) AS xml_station_name,
                lpad(BPUIC, 8, '0') AS eva,
                -- bahn.de sends the line number (not the run number) as fahrtNr for
                -- Swiss S-Bahn legs ('S12' -> 12), but the run number for all other
                -- products (IC 1519, RE 24 -> 4720); key each row the way it gets queried
                CASE
                    WHEN VERKEHRSMITTEL_TEXT IN ('S', 'SN') AND regexp_matches(LINIEN_TEXT, '\\d')
                    THEN ltrim(regexp_extract(LINIEN_TEXT, '(\\d+)', 1), '0')
                    ELSE ltrim(LINIEN_ID, '0')
                END AS train_number,
                CAST(LINIEN_TEXT AS VARCHAR) AS line_number,
                CAST(NULL AS VARCHAR) AS final_destination_station,
                CAST(date_diff('minute', ank_plan, ank_real) AS INTEGER) AS delay_in_min,
                COALESCE(abf_real, ank_real, abf_plan, ank_plan) AS time,
                FAELLT_AUS_TF = 'true' AS is_canceled,
                CAST(VERKEHRSMITTEL_TEXT AS VARCHAR) AS train_type,
                CAST(FAHRT_BEZEICHNER AS VARCHAR) AS train_line_ride_id,
                CAST(NULL AS INTEGER) AS train_line_station_num,
                ank_plan AS arrival_planned_time,
                CASE WHEN FAELLT_AUS_TF = 'true' THEN NULL ELSE ank_real END AS arrival_change_time,
                abf_plan AS departure_planned_time,
                CASE WHEN FAELLT_AUS_TF = 'true' THEN NULL ELSE abf_real END AS departure_change_time,
                FAHRT_BEZEICHNER || ':' || BETRIEBSTAG || ':' || BPUIC AS id
            FROM (
                SELECT *,
                    COALESCE(try_strptime(ANKUNFTSZEIT, '%d.%m.%Y %H:%M:%S'),
                             try_strptime(ANKUNFTSZEIT, '%d.%m.%Y %H:%M')) AS ank_plan,
                    COALESCE(try_strptime(AN_PROGNOSE, '%d.%m.%Y %H:%M:%S'),
                             try_strptime(AN_PROGNOSE, '%d.%m.%Y %H:%M')) AS ank_real,
                    COALESCE(try_strptime(ABFAHRTSZEIT, '%d.%m.%Y %H:%M:%S'),
                             try_strptime(ABFAHRTSZEIT, '%d.%m.%Y %H:%M')) AS abf_plan,
                    COALESCE(try_strptime(AB_PROGNOSE, '%d.%m.%Y %H:%M:%S'),
                             try_strptime(AB_PROGNOSE, '%d.%m.%Y %H:%M')) AS abf_real
                FROM read_csv('{raw_csv}', delim=';', header=true, all_varchar=true)
            )
            WHERE PRODUKT_ID = 'Zug'
              AND DURCHFAHRT_TF != 'true'
              AND BPUIC LIKE '85%'
              AND ank_plan IS NOT NULL
              AND (FAELLT_AUS_TF = 'true'
                   OR (ank_real IS NOT NULL AND AN_PROGNOSE_STATUS IN ('REAL', 'GESCHAETZT')))
        ) TO '{tmp}' (FORMAT PARQUET)
    """)
    os.replace(tmp, out_parquet)


def prune_old_days(days_dir: Path, cutoff: date):
    for f in sorted(days_dir.glob("*.parquet")):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if d < cutoff:
            f.unlink()
            print(f"Pruned old CH day {d}")


def main():
    parser = argparse.ArgumentParser(description="Download Swiss istdaten actual data and build per-day delay parquets")
    parser.add_argument("--days", type=int, default=31, help="days of data to keep current (default: 31)")
    parser.add_argument("--end-date", type=lambda s: date.fromisoformat(s), default=None, help="last day of the window, YYYY-MM-DD (default: yesterday in Europe/Berlin)")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help="base data directory")
    parser.add_argument("--force", action="store_true", help="reprocess days whose parquet already exists")
    args = parser.parse_args()

    end_date = args.end_date or (datetime.now(ZoneInfo("Europe/Berlin")).date() - timedelta(days=1))
    wanted = [end_date - timedelta(days=d) for d in range(args.days)]
    raw_dir = args.data_dir / "ch" / "raw"
    days_dir = args.data_dir / "ch" / "days"
    days_dir.mkdir(parents=True, exist_ok=True)

    missing = [d for d in wanted if args.force or not (days_dir / f"{d}.parquet").exists()]
    if not missing:
        print("All CH day files up to date")
        prune_old_days(days_dir, end_date - timedelta(days=40))
        return

    available = discover_day_urls()
    print(f"{len(missing)} day(s) to build, {len(available)} available on portal")
    failures = 0
    for d in sorted(missing):
        url = available.get(d)
        if url is None:
            # yesterday often publishes later in the morning; caught up on the next run
            print(f"{d}: not published yet, skipping")
            continue
        raw_csv = raw_dir / f"{d}_istdaten.csv"
        out = days_dir / f"{d}.parquet"
        try:
            print(f"{d}: downloading ...", flush=True)
            download(url, raw_csv)
            normalize_day(raw_csv, out)
            rows = duckdb.sql(f"SELECT count(*) FROM '{out}'").fetchone()[0]
            print(f"{d}: {rows:_} train-stop rows")
        except Exception as e:  # keep going; a single bad day shouldn't kill the catch-up
            failures += 1
            print(f"{d}: FAILED ({e})", file=sys.stderr)
        finally:
            raw_csv.unlink(missing_ok=True)

    prune_old_days(days_dir, end_date - timedelta(days=40))
    if failures:
        sys.exit(f"{failures} day(s) failed")


if __name__ == "__main__":
    main()
