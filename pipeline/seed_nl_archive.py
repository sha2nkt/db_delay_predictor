import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Rijden de Treinen train archive (CC BY 4.0): one row per station stop, planned
# times + delay minutes + cancellation flags, published ~4 days after month end.
ARCHIVE_URL = "https://opendata.rijdendetreinen.nl/public/services/services-{month}.csv.gz"

# archive Dutch service-type names -> the IFF codes the live poller stores, so a
# day seeded from the archive and a day built by the poller key identically
TYPE_MAP = {
    "Intercity": "IC",
    "Sprinter": "SPR",
    "Stoptrein": "ST",
    "Sneltrein": "S",
    "Intercity direct": "ICD",
    "ICE": "ICE",
    "EuroCity": "ECC",
    "Eurocity Direct": "ECD",
    "Eurostar": "EST",
    "Nightjet": "NJ",
    "GoVolta": "GV",
}


def prev_month() -> str:
    first = date.today().replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def download(month: str, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"services-{month}.csv.gz"
    if dest.exists():
        print(f"{dest.name}: already downloaded")
        return dest
    url = ARCHIVE_URL.format(month=month)
    print(f"downloading {url}")
    tmp = dest.with_suffix(".gz.tmp")
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
    tmp.rename(dest)
    return dest


def seed_month(csv_gz: Path, crosswalk: dict, days_dir: Path, force: bool) -> None:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'Europe/Berlin'")
    con.execute("CREATE TABLE cw (code VARCHAR PRIMARY KEY, eva VARCHAR, name VARCHAR)")
    con.executemany("INSERT INTO cw VALUES (?, ?, ?)",
                    [(code, st["eva"], st["name"]) for code, st in crosswalk.items()])
    con.execute("CREATE TABLE tmap (src VARCHAR PRIMARY KEY, code VARCHAR)")
    con.executemany("INSERT INTO tmap VALUES (?, ?)", list(TYPE_MAP.items()))

    # replacement services (bus/taxi/metro "ipv trein") are dropped: in the archive
    # they can carry the replaced train's number alongside its cancelled rows, and a
    # bus delay must not be picked as that train's stat for the day
    con.execute(f"""
        CREATE TABLE staged AS
        WITH src AS (
            SELECT * FROM read_csv('{csv_gz}', header = true)
            WHERE "Service:Type" NOT LIKE '%ipv trein%' AND "Service:Type" <> 'Bus'
              AND "Stop:Station code" IS NOT NULL
        )
        SELECT
            s."Service:Date" AS svc_date,
            x.name AS station_name,
            CAST(NULL AS VARCHAR) AS xml_station_name,
            x.eva AS eva,
            ltrim(CAST(s."Service:Train number" AS VARCHAR), '0') AS train_number,
            CAST(NULL AS VARCHAR) AS line_number,
            arg_max(s."Stop:Station name",
                    COALESCE(s."Stop:Departure time", s."Stop:Arrival time"))
                OVER (PARTITION BY s."Service:RDT-ID") AS final_destination_station,
            CAST(COALESCE(s."Stop:Departure delay", s."Stop:Arrival delay") AS INTEGER) AS delay_in_min,
            s."Service:Completely cancelled" OR s."Stop:Arrival cancelled"
                OR s."Stop:Departure cancelled" AS is_canceled,
            COALESCE(t.code, s."Service:Type") AS train_type,
            'NLA:' || s."Service:RDT-ID" AS train_line_ride_id,
            CAST(NULL AS INTEGER) AS train_line_station_num,
            CAST(s."Stop:Arrival time" AS TIMESTAMP) AS arrival_planned_time,
            CASE WHEN s."Service:Completely cancelled" OR s."Stop:Arrival cancelled" THEN NULL
                 ELSE CAST(s."Stop:Arrival time" AS TIMESTAMP)
                      + COALESCE(s."Stop:Arrival delay", 0) * INTERVAL 1 MINUTE END AS arrival_change_time,
            CAST(s."Stop:Departure time" AS TIMESTAMP) AS departure_planned_time,
            CASE WHEN s."Service:Completely cancelled" OR s."Stop:Departure cancelled" THEN NULL
                 ELSE CAST(s."Stop:Departure time" AS TIMESTAMP)
                      + COALESCE(s."Stop:Departure delay", 0) * INTERVAL 1 MINUTE END AS departure_change_time,
            'NLA:' || s."Stop:RDT-ID" AS id
        FROM src s
        JOIN cw x ON lower(s."Stop:Station code") = x.code
        LEFT JOIN tmap t ON s."Service:Type" = t.src
        WHERE s."Service:Train number" IS NOT NULL
    """)
    con.execute("""
        ALTER TABLE staged ADD COLUMN time TIMESTAMP;
        UPDATE staged SET time = COALESCE(departure_change_time, arrival_change_time,
                                          departure_planned_time, arrival_planned_time)
    """)

    days = [r[0] for r in con.execute("SELECT DISTINCT svc_date FROM staged ORDER BY 1").fetchall()]
    days_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for day in days:
        out = days_dir / f"{day}.parquet"
        if out.exists() and not force:
            skipped += 1
            continue
        n = con.execute(
            f"""COPY (SELECT station_name, xml_station_name, eva, train_number, line_number,
                       final_destination_station, delay_in_min, time, is_canceled, train_type,
                       train_line_ride_id, train_line_station_num, arrival_planned_time,
                       arrival_change_time, departure_planned_time, departure_change_time, id
                FROM staged WHERE svc_date = ? AND time IS NOT NULL)
                TO '{out}' (FORMAT PARQUET)""", [day]).fetchone()[0]
        written += 1
        print(f"NL {day}: {n:_} rows (archive)")
    con.close()
    if skipped:
        print(f"{skipped} days already present, skipped (use --force to overwrite)")
    print(f"{csv_gz.name}: {written} day parquets written")


def main():
    parser = argparse.ArgumentParser(description="Seed NL per-day delay parquets from the Rijden de Treinen train archive")
    parser.add_argument("--months", nargs="+", default=[prev_month()], help="months to seed as YYYY-MM (default: previous month)")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help="base data directory")
    parser.add_argument("--crosswalk", type=Path, default=PROJECT_ROOT / "config" / "nl_stations.json", help="IFF station-code -> EVA map")
    parser.add_argument("--force", action="store_true", help="overwrite existing day parquets (default: skip them)")
    args = parser.parse_args()

    crosswalk = json.loads(args.crosswalk.read_text())
    for month in args.months:
        csv_gz = download(month, args.data_dir / "nl" / "archive")
        seed_month(csv_gz, crosswalk, args.data_dir / "nl" / "days", args.force)


if __name__ == "__main__":
    main()
