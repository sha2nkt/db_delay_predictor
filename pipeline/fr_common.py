import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
from google.transit import gtfs_realtime_pb2

BERLIN = ZoneInfo("Europe/Berlin")
FEED_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"

# trip_id "OCESN17752F1187_F:TER:FR:Line::..." -> train 17752, brand TER
TRAIN_NUMBER_RE = re.compile(r"^OCESN(\d+)F")
BRAND_RE = re.compile(r"^[^:]*:([A-Z]{2,4}):")
# stop_id "StopPoint:OCETrain TER-87686006" -> SNCF UIC 87686006
STOP_UIC_RE = re.compile(r"(\d{8})$")

TRIP_CANCELED = 3  # TripDescriptor.ScheduleRelationship.CANCELED
STOP_SKIPPED = 1   # StopTimeUpdate.ScheduleRelationship.SKIPPED


def open_obs_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS obs (
            start_date TEXT NOT NULL,
            trip_id TEXT NOT NULL,
            stop_id TEXT NOT NULL,
            train_number TEXT,
            stop_uic8 TEXT,
            arr_time INTEGER, arr_delay INTEGER,
            dep_time INTEGER, dep_delay INTEGER,
            trip_rel INTEGER, stop_rel INTEGER,
            last_seen INTEGER,
            PRIMARY KEY (start_date, trip_id, stop_id)
        ) WITHOUT ROWID
    """)
    return conn


def decode_feed(raw: bytes, fetch_ts: int) -> list[tuple]:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(raw)
    rows = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip = tu.trip
        m = TRAIN_NUMBER_RE.match(trip.trip_id)
        train_number = m.group(1).lstrip("0") if m else None
        trip_rel = trip.schedule_relationship
        if not tu.stop_time_update and trip_rel == TRIP_CANCELED:
            # whole-trip cancellation without stop details; kept for the record
            rows.append((trip.start_date, trip.trip_id, "", train_number, None,
                         None, None, None, None, trip_rel, None, fetch_ts))
        for stu in tu.stop_time_update:
            arr_time = arr_delay = dep_time = dep_delay = None
            if stu.HasField("arrival"):
                arr_time = stu.arrival.time if stu.arrival.HasField("time") else None
                arr_delay = stu.arrival.delay if stu.arrival.HasField("delay") else None
            if stu.HasField("departure"):
                dep_time = stu.departure.time if stu.departure.HasField("time") else None
                dep_delay = stu.departure.delay if stu.departure.HasField("delay") else None
            stop_rel = stu.schedule_relationship
            if arr_time is None and dep_time is None and stop_rel != STOP_SKIPPED and trip_rel != TRIP_CANCELED:
                continue
            um = STOP_UIC_RE.search(stu.stop_id)
            rows.append((trip.start_date, trip.trip_id, stu.stop_id, train_number,
                         um.group(1) if um else None,
                         arr_time, arr_delay, dep_time, dep_delay,
                         trip_rel, stop_rel, fetch_ts))
    return rows


def upsert_obs(conn: sqlite3.Connection, rows: list[tuple]):
    """Keep the last observation per stop; arrival/departure are updated as units so a
    later snapshot that omits one side doesn't wipe the recorded final value."""
    conn.executemany("""
        INSERT INTO obs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(start_date, trip_id, stop_id) DO UPDATE SET
            train_number = COALESCE(excluded.train_number, train_number),
            stop_uic8 = COALESCE(excluded.stop_uic8, stop_uic8),
            arr_time  = CASE WHEN excluded.arr_time IS NOT NULL OR excluded.arr_delay IS NOT NULL
                             THEN excluded.arr_time ELSE arr_time END,
            arr_delay = CASE WHEN excluded.arr_time IS NOT NULL OR excluded.arr_delay IS NOT NULL
                             THEN excluded.arr_delay ELSE arr_delay END,
            dep_time  = CASE WHEN excluded.dep_time IS NOT NULL OR excluded.dep_delay IS NOT NULL
                             THEN excluded.dep_time ELSE dep_time END,
            dep_delay = CASE WHEN excluded.dep_time IS NOT NULL OR excluded.dep_delay IS NOT NULL
                             THEN excluded.dep_delay ELSE dep_delay END,
            trip_rel = excluded.trip_rel,
            stop_rel = excluded.stop_rel,
            last_seen = excluded.last_seen
    """, rows)
    conn.commit()


def _berlin_naive(epoch: int | None) -> datetime | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=BERLIN).replace(tzinfo=None)


def consolidate_day(conn: sqlite3.Connection, day: date, crosswalk: dict, out_dir: Path,
                    min_keep_ratio: float = 0.5) -> int:
    """Write one normalized 17-column day parquet from the observation store.
    Returns rows written, or -1 if the existing file was kept."""
    obs = conn.execute(
        "SELECT trip_id, stop_id, train_number, stop_uic8, arr_time, arr_delay,"
        " dep_time, dep_delay, trip_rel, stop_rel FROM obs WHERE start_date = ?",
        (day.strftime("%Y%m%d"),),
    ).fetchall()

    out = out_dir / f"{day}.parquet"
    # whole-trip cancellations may arrive as a bare marker row (no stop updates);
    # propagate them to the trip's previously observed per-stop rows
    canceled_trips = {trip_id for trip_id, *_, trip_rel, _sr in obs if trip_rel == TRIP_CANCELED}
    records = []
    for trip_id, stop_id, train_number, uic8, at, ad, dt, dd, trip_rel, stop_rel in obs:
        station = crosswalk.get(uic8) if uic8 else None
        if station is None or not train_number:
            continue
        canceled = trip_id in canceled_trips or stop_rel == STOP_SKIPPED
        # the feed carries actual time + delay; planned = time - delay
        arr_planned = _berlin_naive(at - ad) if at is not None and ad is not None else None
        dep_planned = _berlin_naive(dt - dd) if dt is not None and dd is not None else None
        if arr_planned is None and dep_planned is None:
            continue
        arr_change = None if canceled else _berlin_naive(at)
        dep_change = None if canceled else _berlin_naive(dt)
        delay_s = dd if dd is not None else ad
        m = BRAND_RE.search(trip_id)
        records.append((
            station["name"], None, station["eva"], train_number, None, None,
            round(delay_s / 60) if delay_s is not None else None,
            dep_change or arr_change or dep_planned or arr_planned,
            canceled, m.group(1) if m else "SNCF", trip_id, None,
            arr_planned, arr_change, dep_planned, dep_change,
            f"{trip_id}:{day}:{stop_id}",
        ))

    if out.exists() and len(records) < min_keep_ratio * _parquet_rows(out):
        print(f"FR {day}: new build has {len(records)} rows < {min_keep_ratio:.0%} of existing, keeping existing")
        return -1
    if not records:
        print(f"FR {day}: no rows, skipping")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE day_rows (
            station_name VARCHAR, xml_station_name VARCHAR, eva VARCHAR, train_number VARCHAR,
            line_number VARCHAR, final_destination_station VARCHAR, delay_in_min INTEGER,
            time TIMESTAMP, is_canceled BOOLEAN, train_type VARCHAR, train_line_ride_id VARCHAR,
            train_line_station_num INTEGER, arrival_planned_time TIMESTAMP, arrival_change_time TIMESTAMP,
            departure_planned_time TIMESTAMP, departure_change_time TIMESTAMP, id VARCHAR
        )
    """)
    con.executemany(f"INSERT INTO day_rows VALUES ({','.join('?' * 17)})", records)
    con.execute(f"COPY day_rows TO '{tmp}' (FORMAT PARQUET)")
    con.close()
    os.replace(tmp, out)
    print(f"FR {day}: {len(records):_} rows")
    return len(records)


def _parquet_rows(path: Path) -> int:
    return duckdb.sql(f"SELECT count(*) FROM '{path}'").fetchone()[0]
