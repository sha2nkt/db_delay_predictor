import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import httpx

MGATE_URL = "https://fahrplan.oebb.at/bin/mgate.exe"
# ÖBB's HAFAS mgate ("Scotty") speaks protocol 1.80 and needs no per-request auth
# beyond this static envelope (same access the public web client uses)
REQ_BASE = {
    "auth": {"type": "AID", "aid": "OWDL4fE4ixNiPBBm"},
    "client": {"id": "OEBB", "type": "IPH", "name": "oebbPROD-ADHOC"},
    "ver": "1.80", "lang": "deu",
}
# product class bitmask, bits 0-5: RJ/RJX, IC/EC, NJ/D, REX/R, CJX, S-Bahn (32);
# bus is bit 6, so 63 = every train category and nothing else
TRAIN_PRODUCTS = "63"

ZI_RE = re.compile(r"#ZI#(\d+)#")


def open_obs_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS obs (
            day TEXT NOT NULL,
            eva TEXT NOT NULL,
            zi TEXT NOT NULL,
            train_number TEXT,
            train_type TEXT,
            line TEXT,
            final_dest TEXT,
            arr_plan TEXT, arr_real TEXT,
            dep_plan TEXT, dep_real TEXT,
            arr_cncl INTEGER, dep_cncl INTEGER,
            last_seen INTEGER,
            PRIMARY KEY (day, eva, zi)
        ) WITHOUT ROWID
    """)
    return conn


def hafas_dt(day: str, t: str | None) -> str | None:
    """HAFAS board time ("HHMMSS", or "DDHHMMSS" with a day offset for
    cross-midnight stops) -> naive local "YYYY-MM-DD HH:MM:SS"."""
    if not t:
        return None
    off = 0
    if len(t) == 8:
        off, t = int(t[:2]), t[2:]
    d = datetime.strptime(day, "%Y%m%d") + timedelta(days=off)
    return f"{d:%Y-%m-%d} {t[:2]}:{t[2:4]}:{t[4:6]}"


def decode_board(svc: dict, eva: str, btype: str, fetch_ts: int) -> list[tuple]:
    """One StationBoard service result -> obs rows keyed by the journey's HAFAS
    ZI id, which is shared between the ARR and DEP boards of the same station."""
    res = svc.get("res", {})
    prods = res.get("common", {}).get("prodL", [])
    rows = []
    for jny in res.get("jnyL", []):
        day = jny.get("date")
        zi = ZI_RE.search(jny.get("jid", ""))
        prod_x = jny.get("prodX", -1)
        if not day or not zi or not 0 <= prod_x < len(prods):
            continue
        st = jny.get("stbStop", {})
        ctx = prods[prod_x].get("prodCtx", {})
        arr_plan = hafas_dt(day, st.get("aTimeS"))
        dep_plan = hafas_dt(day, st.get("dTimeS"))
        if arr_plan is None and dep_plan is None:
            continue
        rows.append((
            day, eva, zi.group(1),
            (ctx.get("num") or "").strip() or None,
            (ctx.get("catOutS") or "").strip() or None,
            (ctx.get("line") or "").strip() or None,
            # dirTxt on an ARR board is the journey's origin, not a destination
            jny.get("dirTxt") if btype == "DEP" else None,
            arr_plan, hafas_dt(day, st.get("aTimeR")),
            dep_plan, hafas_dt(day, st.get("dTimeR")),
            1 if st.get("aCncl") else 0, 1 if st.get("dCncl") else 0,
            fetch_ts,
        ))
    return rows


def fetch_boards(client: httpx.Client, hafas_id: str, eva: str, when: datetime,
                 dur: int, max_jny: int, fetch_ts: int) -> list[tuple]:
    """Fetch the ARR and DEP boards of one station in a single mgate request."""
    req = {
        "date": when.strftime("%Y%m%d"), "time": when.strftime("%H%M%S"),
        "stbLoc": {"lid": f"A=1@L={hafas_id}@"},
        "jnyFltrL": [{"type": "PROD", "mode": "INC", "value": TRAIN_PRODUCTS}],
        "dur": dur, "maxJny": max_jny,
    }
    body = dict(REQ_BASE, svcReqL=[{"meth": "StationBoard", "req": dict(req, type=t)}
                                   for t in ("ARR", "DEP")])
    resp = client.post(MGATE_URL, json=body)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("err") not in (None, "OK"):
        raise RuntimeError(f"mgate error {payload.get('err')}: {payload.get('errTxt', '')}")
    rows = []
    for svc, btype in zip(payload.get("svcResL", []), ("ARR", "DEP")):
        if svc.get("err") != "OK":
            raise RuntimeError(f"{btype} board error {svc.get('err')}: {svc.get('errTxt', '')}")
        rows += decode_board(svc, eva, btype, fetch_ts)
    return rows


def upsert_obs(conn: sqlite3.Connection, rows: list[tuple]):
    """Keep the last observation per journey and station; the arrival and
    departure sides are updated as units so the DEP board (which carries no
    arrival fields) doesn't wipe what the ARR board recorded, and vice versa."""
    conn.executemany("""
        INSERT INTO obs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(day, eva, zi) DO UPDATE SET
            train_number = COALESCE(excluded.train_number, train_number),
            train_type = COALESCE(excluded.train_type, train_type),
            line = COALESCE(excluded.line, line),
            final_dest = COALESCE(excluded.final_dest, final_dest),
            arr_plan = CASE WHEN excluded.arr_plan IS NOT NULL THEN excluded.arr_plan ELSE arr_plan END,
            arr_real = CASE WHEN excluded.arr_plan IS NOT NULL THEN excluded.arr_real ELSE arr_real END,
            arr_cncl = CASE WHEN excluded.arr_plan IS NOT NULL THEN excluded.arr_cncl ELSE arr_cncl END,
            dep_plan = CASE WHEN excluded.dep_plan IS NOT NULL THEN excluded.dep_plan ELSE dep_plan END,
            dep_real = CASE WHEN excluded.dep_plan IS NOT NULL THEN excluded.dep_real ELSE dep_real END,
            dep_cncl = CASE WHEN excluded.dep_plan IS NOT NULL THEN excluded.dep_cncl ELSE dep_cncl END,
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
        "SELECT eva, zi, train_number, train_type, line, final_dest,"
        " arr_plan, arr_real, dep_plan, dep_real, arr_cncl, dep_cncl"
        " FROM obs WHERE day = ?",
        (day.strftime("%Y%m%d"),),
    ).fetchall()

    out = out_dir / f"{day}.parquet"
    records = []
    for eva, zi, num, cat, line, dest, ap, ar, dp, dr, ac, dc in obs:
        station = stations.get(eva)
        # bahn.de sends the line digits (not the run number) as fahrtNr for
        # Austrian S-Bahn legs ("S 4" -> 4), like for Swiss S-Bahn; key those
        # rows the way they get queried
        if cat == "s" and line:
            m = re.search(r"(\d+)", line)
            if m:
                num = m.group(1)
        num = (num or "").lstrip("0")
        if station is None or not num:
            continue
        canceled = bool(ac or dc)
        # a board entry whose real time never appeared was not live-tracked;
        # leave the change columns NULL (unknown) rather than claiming on-time
        arr_change = None if canceled else ar
        dep_change = None if canceled else dr
        delay = _minutes(ap, ar)
        if delay is None:
            delay = _minutes(dp, dr)
        records.append((
            station["name"], None, eva, num, line, dest,
            delay,
            dep_change or arr_change or dp or ap,
            canceled, cat, f"{day:%Y%m%d}:{zi}", None,
            ap, arr_change, dp, dep_change,
            f"{zi}:{day}:{eva}",
        ))

    if out.exists() and len(records) < min_keep_ratio * _parquet_rows(out):
        print(f"AT {day}: new build has {len(records)} rows < {min_keep_ratio:.0%} of existing, keeping existing")
        return -1
    if not records:
        print(f"AT {day}: no rows, skipping")
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
    print(f"AT {day}: {len(records):_} rows")
    return len(records)


def _parquet_rows(path: Path) -> int:
    return duckdb.sql(f"SELECT count(*) FROM '{path}'").fetchone()[0]
