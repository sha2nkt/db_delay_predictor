import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import duckdb
import httpx

# ViaggiaTreno is RFI/Trenitalia's public real-time site; its REST backend needs no
# auth. There is no official delay open data for Italy, so this is the same source
# every Italian delay tracker (TrainStats, OpenRitardi, ...) builds on.
BASE_URL = "http://www.viaggiatreno.it/infomobilita/resteasy/viaggiatreno"
ROME = ZoneInfo("Europe/Rome")

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

DAY_MS = 86_400_000


def js_date(dt: datetime) -> str:
    """The board endpoints take the timestamp as a JS Date().toString(); build it
    with hardcoded English names so the system locale can't corrupt it."""
    off = int(dt.utcoffset().total_seconds())
    return (f"{_DAYS[dt.weekday()]} {_MONTHS[dt.month - 1]} {dt.day:02d} {dt.year}"
            f" {dt:%H:%M:%S} GMT{'+' if off >= 0 else '-'}{abs(off) // 3600:02d}{abs(off) % 3600 // 60:02d}")


def rome_dt(ms: int | None) -> str | None:
    """Epoch ms -> naive Europe/Rome "YYYY-MM-DD HH:MM:SS" (the timestamp
    convention of the day parquets)."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=ROME).strftime("%Y-%m-%d %H:%M:%S")


def rome_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=ROME).strftime("%Y%m%d")


def _get_json(client: httpx.Client, url: str):
    """VT signals "no data" as HTTP 200 with an empty (or non-JSON) body."""
    resp = client.get(url)
    resp.raise_for_status()
    if not resp.text.strip():
        return None
    try:
        return json.loads(resp.text)
    except ValueError:
        return None


def fetch_board(client: httpx.Client, code: str, btype: str, when: datetime) -> list[dict]:
    kind = "partenze" if btype == "DEP" else "arrivi"
    rows = _get_json(client, f"{BASE_URL}/{kind}/{code}/{quote(js_date(when))}")
    return rows if isinstance(rows, list) else []


def fetch_andamento(client: httpx.Client, origin: str, number: int, dep_ms: int) -> dict | None:
    payload = _get_json(client, f"{BASE_URL}/andamentoTreno/{origin}/{number}/{dep_ms}")
    return payload if isinstance(payload, dict) else None


def open_obs_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    # registry of discovered train runs; (origin, number, dep_ms) is the key
    # triple andamentoTreno is queried with
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trains (
            day TEXT NOT NULL,
            train_key TEXT NOT NULL,
            origin TEXT NOT NULL,
            number INTEGER NOT NULL,
            dep_ms INTEGER NOT NULL,
            category TEXT,
            first_seen INTEGER, last_seen INTEGER,
            next_poll INTEGER, done INTEGER DEFAULT 0, fails INTEGER DEFAULT 0,
            PRIMARY KEY (day, train_key)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS obs (
            day TEXT NOT NULL,
            train_key TEXT NOT NULL,
            stop_code TEXT NOT NULL,
            stop_num INTEGER,
            train_number TEXT,
            train_type TEXT,
            final_dest TEXT,
            arr_plan TEXT, arr_real TEXT,
            dep_plan TEXT, dep_real TEXT,
            arr_cncl INTEGER, dep_cncl INTEGER,
            last_seen INTEGER,
            PRIMARY KEY (day, train_key, stop_code)
        ) WITHOUT ROWID
    """)
    return conn


def register_trains(conn: sqlite3.Connection, board_rows: list[dict], fetch_ts: int,
                    min_day: str) -> int:
    """Board rows (partenze or arrivi) -> new registry entries. Both board types
    carry the full andamento key (codOrigine, numeroTreno, dataPartenzaTreno)."""
    new = 0
    for r in board_rows:
        num, origin, dep_ms = r.get("numeroTreno"), r.get("codOrigine"), r.get("dataPartenzaTreno")
        if not num or not origin or not dep_ms:
            continue
        day = rome_day(dep_ms)
        if day < min_day:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO trains (day, train_key, origin, number, dep_ms,"
            " category, first_seen, last_seen, next_poll) VALUES (?,?,?,?,?,?,?,?,?)",
            (day, f"{origin}-{num}-{dep_ms}", origin, num, dep_ms,
             r.get("categoria"), fetch_ts, fetch_ts, fetch_ts),
        )
        new += cur.rowcount
    conn.commit()
    return new


def decode_andamento(payload: dict, day: str, train_key: str, fetch_ts: int) -> tuple[list[tuple], int | None, bool]:
    """One andamentoTreno payload -> (obs rows, last planned arrival ms, arrived).

    VT renders some post-midnight stops on the departure day (24h early); times
    that regress by more than 12h against the running maximum get shifted forward
    a day so cross-midnight runs stay monotonic."""
    number = str(payload.get("numeroTreno") or "")
    category = (payload.get("categoria") or "").strip() or None
    dest = (payload.get("destinazione") or "").strip() or None
    # 1 = train cancelled outright; partial cancellations come as per-stop flags
    train_cncl = payload.get("provvedimento") == 1
    rows, running_max, last_plan_ms = [], payload.get("dataPartenzaTreno") or 0, None
    fermate = payload.get("fermate") or []
    arrived = bool(fermate) and fermate[-1].get("arrivoReale") is not None

    def unwrap(ms):
        nonlocal running_max
        if ms is None:
            return None
        if ms < running_max - 12 * 3600 * 1000:
            ms += DAY_MS
        running_max = max(running_max, ms)
        return ms

    for i, f in enumerate(fermate):
        code = f.get("id")
        if not code:
            continue
        arr_plan = unwrap(f.get("arrivo_teorico"))
        arr_real = unwrap(f.get("arrivoReale"))
        dep_plan = unwrap(f.get("partenza_teorica"))
        dep_real = unwrap(f.get("partenzaReale"))
        if arr_plan is None and dep_plan is None:
            continue
        last_plan_ms = arr_plan or dep_plan or last_plan_ms
        # actualFermataType 3 marks a stop the run skipped/cancelled
        cncl = 1 if (train_cncl or f.get("actualFermataType") == 3) else 0
        rows.append((
            day, train_key, code, i, number, category, dest,
            rome_dt(arr_plan), rome_dt(arr_real),
            rome_dt(dep_plan), rome_dt(dep_real),
            cncl, cncl, fetch_ts,
        ))
    return rows, last_plan_ms, arrived


def upsert_obs(conn: sqlite3.Connection, rows: list[tuple]):
    """Each andamento fetch is a full snapshot of the run, so later values win;
    real times only COALESCE so a source hiccup can't erase an observed event."""
    conn.executemany("""
        INSERT INTO obs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(day, train_key, stop_code) DO UPDATE SET
            stop_num = excluded.stop_num,
            train_number = COALESCE(excluded.train_number, train_number),
            train_type = COALESCE(excluded.train_type, train_type),
            final_dest = COALESCE(excluded.final_dest, final_dest),
            arr_plan = COALESCE(excluded.arr_plan, arr_plan),
            arr_real = COALESCE(excluded.arr_real, arr_real),
            dep_plan = COALESCE(excluded.dep_plan, dep_plan),
            dep_real = COALESCE(excluded.dep_real, dep_real),
            arr_cncl = excluded.arr_cncl,
            dep_cncl = excluded.dep_cncl,
            last_seen = excluded.last_seen
    """, rows)
    conn.commit()


def _minutes(plan: str | None, real: str | None) -> int | None:
    if plan is None or real is None:
        return None
    diff = datetime.fromisoformat(real) - datetime.fromisoformat(plan)
    return round(diff.total_seconds() / 60)


def consolidate_day(conn: sqlite3.Connection, day: date, stations: dict, out_dir: Path,
                    min_keep_ratio: float = 0.5) -> int:
    """Write one normalized 17-column day parquet from the observation store.
    Returns rows written, or -1 if the existing file was kept."""
    obs = conn.execute(
        "SELECT train_key, stop_code, stop_num, train_number, train_type, final_dest,"
        " arr_plan, arr_real, dep_plan, dep_real, arr_cncl, dep_cncl"
        " FROM obs WHERE day = ?",
        (day.strftime("%Y%m%d"),),
    ).fetchall()

    out = out_dir / f"{day}.parquet"
    records = []
    for key, code, num_idx, num, cat, dest, ap, ar, dp, dr, ac, dc in obs:
        station = stations.get(code)
        num = (num or "").lstrip("0")
        # only stops mapped to a bahn.de EVA can ever be queried; foreign stops of
        # international runs are covered by the other country sources
        if station is None or not num:
            continue
        canceled = bool(ac or dc)
        # a stop whose real time never appeared was not live-tracked; leave the
        # change columns NULL (unknown) rather than claiming on-time
        arr_change = None if canceled else ar
        dep_change = None if canceled else dr
        delay = _minutes(ap, ar)
        if delay is None:
            delay = _minutes(dp, dr)
        records.append((
            station["name"], None, station["eva"], num, None, dest,
            delay,
            dep_change or arr_change or dp or ap,
            canceled, cat, f"{day:%Y%m%d}:{key}", num_idx,
            ap, arr_change, dp, dep_change,
            f"{key}:{day}:{code}",
        ))

    if out.exists() and len(records) < min_keep_ratio * _parquet_rows(out):
        print(f"IT {day}: new build has {len(records)} rows < {min_keep_ratio:.0%} of existing, keeping existing")
        return -1
    if not records:
        print(f"IT {day}: no rows, skipping")
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
    print(f"IT {day}: {len(records):_} rows")
    return len(records)


def _parquet_rows(path: Path) -> int:
    return duckdb.sql(f"SELECT count(*) FROM '{path}'").fetchone()[0]
