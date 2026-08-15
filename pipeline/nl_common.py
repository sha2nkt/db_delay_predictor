import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
from google.protobuf import unknown_fields
from google.transit import gtfs_realtime_pb2

# Amsterdam shares Berlin's offsets year-round; the whole store is Berlin-naive
BERLIN = ZoneInfo("Europe/Berlin")
FEED_URL = "https://gtfs.ovapi.nl/nl/trainUpdates.pb"

TRIP_CANCELED = 3  # TripDescriptor.ScheduleRelationship.CANCELED
STOP_SKIPPED = 1   # StopTimeUpdate.ScheduleRelationship.SKIPPED

# OVapi extension (gtfs-realtime-OVapi.proto), field 1003 on TripDescriptor and
# StopTimeUpdate. The stock gtfs_realtime_pb2 doesn't know it, so it surfaces as an
# unknown length-delimited field whose payload is a submessage of string fields only.
OVAPI_EXT = 1003
TRIP_EXT_REALTIME_ID = 1  # "IFF:<type>:<number>", matches trips.txt realtime_trip_id
STOP_EXT_STATION_ID = 4   # IFF station code, e.g. "asd"


def _varint(data: bytes, i: int) -> tuple[int, int]:
    val = shift = 0
    while True:
        b = data[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def _wire_strings(data: bytes) -> dict[int, str]:
    out = {}
    i = 0
    while i < len(data):
        tag, i = _varint(data, i)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            ln, i = _varint(data, i)
            try:
                out[field] = data[i:i + ln].decode()
            except UnicodeDecodeError:
                pass
            i += ln
        elif wire == 0:
            _, i = _varint(data, i)
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:
            break
    return out


def _ext_strings(msg) -> dict[int, str]:
    for f in unknown_fields.UnknownFieldSet(msg):
        if f.field_number == OVAPI_EXT and isinstance(f.data, bytes):
            return _wire_strings(f.data)
    return {}


def open_obs_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    # Keyed by the IFF identity, not trip_id/stop_id: OVapi regenerates the static
    # GTFS nightly and rotates both ids, while train number + station survive.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS obs (
            start_date TEXT NOT NULL,
            train_key TEXT NOT NULL,
            station TEXT NOT NULL,
            train_number TEXT, train_type TEXT, headsign TEXT,
            arr_time INTEGER, arr_delay INTEGER,
            dep_time INTEGER, dep_delay INTEGER,
            trip_rel INTEGER, stop_rel INTEGER,
            last_seen INTEGER,
            PRIMARY KEY (start_date, train_key, station)
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
        rid = _ext_strings(trip).get(TRIP_EXT_REALTIME_ID, "")
        if not rid.startswith("IFF:") or rid.count(":") != 2:
            # added/replacement services without an IFF identity (~4% of entities);
            # nothing to key a bahn.de fahrtNr against
            continue
        _, train_type, train_number = rid.split(":")
        train_key = f"{train_type}:{train_number}"
        train_number = train_number.lstrip("0")
        headsign = _ext_strings(tu).get(1)
        trip_rel = trip.schedule_relationship
        if not tu.stop_time_update and trip_rel == TRIP_CANCELED:
            # whole-trip cancellation without stop details; kept for the record
            rows.append((trip.start_date, train_key, "", train_number, train_type,
                         headsign, None, None, None, None, trip_rel, None, fetch_ts))
        for stu in tu.stop_time_update:
            station = _ext_strings(stu).get(STOP_EXT_STATION_ID, "").lower()
            if not station:
                continue
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
            rows.append((trip.start_date, train_key, station, train_number, train_type,
                         headsign, arr_time, arr_delay, dep_time, dep_delay,
                         trip_rel, stop_rel, fetch_ts))
    return rows


def upsert_obs(conn: sqlite3.Connection, rows: list[tuple]):
    """Keep the last observation per stop; arrival/departure are updated as units so a
    later snapshot that omits one side doesn't wipe the recorded final value."""
    conn.executemany("""
        INSERT INTO obs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(start_date, train_key, station) DO UPDATE SET
            train_number = COALESCE(excluded.train_number, train_number),
            train_type = COALESCE(excluded.train_type, train_type),
            headsign = COALESCE(excluded.headsign, headsign),
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
        "SELECT train_key, station, train_number, train_type, headsign, arr_time,"
        " arr_delay, dep_time, dep_delay, trip_rel, stop_rel FROM obs WHERE start_date = ?",
        (day.strftime("%Y%m%d"),),
    ).fetchall()

    out = out_dir / f"{day}.parquet"
    # whole-trip cancellations may arrive as a bare marker row (no stop updates);
    # propagate them to the trip's previously observed per-stop rows
    canceled_keys = {train_key for train_key, *_, trip_rel, _sr in obs if trip_rel == TRIP_CANCELED}
    unmapped = set()
    records = []
    for train_key, station, train_number, train_type, headsign, at, ad, dt, dd, trip_rel, stop_rel in obs:
        st = crosswalk.get(station) if station else None
        if st is None:
            # foreign stops (BE/DE legs) surface as bare UIC numbers, not IFF codes;
            # they'd be dropped by the merge's 084% filter anyway, so skip silently
            if station and not station.isdigit():
                unmapped.add(station)
            continue
        canceled = train_key in canceled_keys or stop_rel == STOP_SKIPPED
        # the feed carries actual time + delay; planned = time - delay
        arr_planned = _berlin_naive(at - ad) if at is not None and ad is not None else None
        dep_planned = _berlin_naive(dt - dd) if dt is not None and dd is not None else None
        if arr_planned is None and dep_planned is None:
            continue
        arr_change = None if canceled else _berlin_naive(at)
        dep_change = None if canceled else _berlin_naive(dt)
        delay_s = dd if dd is not None else ad
        records.append((
            st["name"], None, st["eva"], train_number, None, headsign,
            round(delay_s / 60) if delay_s is not None else None,
            dep_change or arr_change or dep_planned or arr_planned,
            canceled, train_type, f"NL:{train_key}", None,
            arr_planned, arr_change, dep_planned, dep_change,
            f"NL:{train_key}:{day}:{station}",
        ))

    if unmapped:
        print(f"NL {day}: {len(unmapped)} station codes not in crosswalk: {sorted(unmapped)[:10]}")
    if out.exists() and len(records) < min_keep_ratio * _parquet_rows(out):
        print(f"NL {day}: new build has {len(records)} rows < {min_keep_ratio:.0%} of existing, keeping existing")
        return -1
    if not records:
        print(f"NL {day}: no rows, skipping")
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
    print(f"NL {day}: {len(records):_} rows")
    return len(records)


def _parquet_rows(path: Path) -> int:
    return duckdb.sql(f"SELECT count(*) FROM '{path}'").fetchone()[0]
