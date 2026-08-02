from collections import OrderedDict
from datetime import date, datetime, timedelta
from statistics import median
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

DELAYS_PARQUET = Path(__file__).resolve().parent.parent / "data" / "delays.parquet"
BERLIN = ZoneInfo("Europe/Berlin")

# Traffic spread across many routes grows these without bound, on top of a ~6GB process.
# Same LRU eviction as bahn_api._cached.
CACHE_MAX = 50_000

_conn: duckdb.DuckDBPyConnection | None = None
_max_day: date | None = None
_min_day: date | None = None
_cache: "OrderedDict[tuple[str, str, int], dict | None]" = OrderedDict()
_date_cache: "OrderedDict[tuple[str, str, date], dict | None]" = OrderedDict()
_dep_date_cache: "OrderedDict[tuple[str, str, date], dict | None]" = OrderedDict()
_stations: list[dict] = []


def _remember(cache: OrderedDict, key: tuple, value):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > CACHE_MAX:
        cache.popitem(last=False)
    return value


def init():
    global _conn, _max_day, _min_day
    if not DELAYS_PARQUET.exists():
        raise RuntimeError(
            f"{DELAYS_PARQUET} not found - run: uv run python pipeline/build_delay_db.py"
        )
    _conn = duckdb.connect()
    # train_no is materialized (not ltrim'd per query) and the table is sorted on it so
    # DuckDB's zone maps can skip row groups. Without this every lookup scans all 19.7M
    # rows: measured 110ms of CPU per leg against 1.2ms with it, and a journey search
    # does ~30 lookups. Costs ~12s of startup.
    _conn.execute(
        "CREATE TABLE delays AS"
        " SELECT *,"
        "  CASE"
        # German S-Bahn: bahn.de sends the line label ("S5") as fahrtNr, so key those
        # rows by line. CH S-Bahn (eva 085%) is already re-keyed to bare digits by
        # build_ch_days.py and must keep the run-number key.
        "   WHEN train_type = 'S' AND eva LIKE '080%' AND line_number LIKE 'S%'"
        "    THEN replace(line_number, ' ', '')"
        "   WHEN train_type = 'S' AND eva LIKE '080%' AND regexp_matches(line_number, '^[0-9]+$')"
        "    THEN 'S' || line_number"  # a few networks report bare digits in IRIS l
        "   ELSE ltrim(train_number, '0')"
        "  END AS train_no"
        f" FROM read_parquet('{DELAYS_PARQUET}')"
        " ORDER BY train_no, eva"
    )
    # a sorted lookup no longer needs 32 threads, and capping them keeps concurrent
    # queries from oversubscribing the box
    _conn.execute("SET threads=4")
    # parquets built before the reason feature lack the column
    _conn.execute("ALTER TABLE delays ADD COLUMN IF NOT EXISTS reason_code INTEGER")
    _min_day, _max_day = _conn.execute(
        "SELECT min(CAST(arrival_planned_time AS DATE)), max(CAST(arrival_planned_time AS DATE))"
        " FROM delays WHERE arrival_planned_time IS NOT NULL"
    ).fetchone()
    _build_station_index()


def coverage() -> tuple[date | None, date | None]:
    return _min_day, _max_day


def row_count() -> int:
    return _conn.execute("SELECT count(*) FROM delays").fetchone()[0] if _conn else 0


def _fold(s: str) -> str:
    """Diacritic/separator-insensitive form so 'Munchen'/'Tubingen' match the umlaut names
    and 'Berlin Hbf' matches the stored 'Berlin Hauptbahnhof'."""
    s = s.lower()
    for a, b in (("ü", "u"), ("ö", "o"), ("ä", "a"), ("ß", "ss"),
                 ("é", "e"), ("è", "e"), ("ê", "e"), ("á", "a"), ("à", "a"),
                 ("hauptbahnhof", "hbf"),
                 ("-", " "), (".", " "), (",", " ")):
        s = s.replace(a, b)
    return " ".join(s.split())


def _build_station_index():
    """Every station in the delay data as an autocomplete entry, deduped by name (the
    multi-level Hbf EVAs collapse to one — journey search resolves any level the same)
    and ranked by observation volume. Lets /api/locations answer without calling bahn.de."""
    global _stations
    rows = _conn.execute(
        """
        SELECT eva, station_name, count(*) AS cnt
        FROM delays
        WHERE station_name IS NOT NULL AND station_name <> '' AND eva IS NOT NULL
        GROUP BY eva, station_name
        """
    ).fetchall()

    # one entry per folded name: keep the busiest EVA/spelling, sum volume across levels
    best: dict[str, tuple[str, str, int]] = {}  # norm -> (eva, display name, its count)
    totals: dict[str, int] = {}
    for eva, name, cnt in rows:
        norm = _fold(name)
        totals[norm] = totals.get(norm, 0) + cnt
        if norm not in best or cnt > best[norm][2]:
            best[norm] = (eva, name, cnt)

    stations = []
    for norm, (eva, name, _) in best.items():
        ext = eva.lstrip("0")  # bahn.de extId / HAFAS L= is unpadded
        stations.append({
            "id": f"A=1@O={name}@L={ext}@",
            "extId": ext,
            "name": name,
            "norm": norm,
            "total": totals[norm],
            "is_hbf": "hbf" in norm.split(),
        })
    _stations = stations


def station_search(query: str, limit: int = 8) -> list[dict]:
    """Local autocomplete: {id, extId, name} for stations matching `query`, or [] if none.
    prefix > word-start > substring; within a tier main stations lead, then busier ones."""
    q = _fold(query)
    if not q:
        return []
    scored = []
    for s in _stations:
        n = s["norm"]
        if n.startswith(q):
            rank = 0
        elif (" " + q) in n:
            rank = 1
        elif q in n:
            rank = 2
        else:
            continue
        scored.append((rank, 0 if s["is_hbf"] else 1, -s["total"], s))
    scored.sort(key=lambda x: x[:3])
    return [{"id": s["id"], "extId": s["extId"], "name": s["name"]} for *_, s in scored[:limit]]


def pad_eva(stop_id: str) -> str:
    return stop_id.rjust(8, "0")


def to_berlin_naive(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return dt  # bahn.de sollzeit is already Berlin-local naive
    return dt.astimezone(BERLIN).replace(tzinfo=None)


def leg_delay_stats(
    train_number: str, eva_padded: str, planned_arrival_local: datetime, window: int = 7
) -> dict | None:
    """Arrival delay stats over the last `window` days for one train at one station, or None."""
    if _max_day is None:
        return None
    train_number = train_number.lstrip("0")
    cache_key = (train_number, eva_padded, window)
    if cache_key in _cache:
        return _cache[cache_key]

    tod = planned_arrival_local.strftime("%H:%M:%S")
    cutoff = _max_day - timedelta(days=window - 1)
    rows = _conn.execute(
        """
        WITH candidates AS (
            SELECT CAST(arrival_planned_time AS DATE) AS day,
                   arrival_planned_time,
                   arrival_change_time,
                   date_diff('minute', arrival_planned_time, arrival_change_time) AS arr_delay,
                   is_canceled,
                   reason_code,
                   least(
                       abs(date_diff('minute', CAST(arrival_planned_time AS TIME), CAST(? AS TIME))),
                       1440 - abs(date_diff('minute', CAST(arrival_planned_time AS TIME), CAST(? AS TIME)))
                   ) AS tod_diff
            FROM delays
            WHERE train_no = ? AND eva = ?
              AND arrival_planned_time IS NOT NULL
              AND CAST(arrival_planned_time AS DATE) >= ?
        )
        -- one stop per calendar day: closest in time-of-day; reject same-numbered
        -- trains running at a very different hour
        SELECT DISTINCT ON (day) day, arr_delay, is_canceled, reason_code
        FROM candidates WHERE tod_diff <= 120
        ORDER BY day, tod_diff, arrival_planned_time, arrival_change_time
        """,
        [tod, tod, train_number, eva_padded, cutoff],
    ).fetchall()

    if not rows:
        stats = None
    else:
        ok_delays = [d for _, d, canceled, _ in rows if not canceled and d is not None]
        stats = {
            "medianDelay": round(median(ok_delays), 1) if ok_delays else None,
            "maxDelay": max(ok_delays) if ok_delays else None,
            "daysMatched": len(rows),
            "canceledDays": sum(1 for _, _, canceled, _ in rows if canceled),
            "windowStart": cutoff.isoformat(),
            "windowEnd": _max_day.isoformat(),
            "days": [
                {
                    "day": day.isoformat(),
                    "delay": None if canceled else delay,
                    "canceled": bool(canceled),
                    "reason": reason,
                }
                for day, delay, canceled, reason in rows
            ],
        }
    return _remember(_cache, cache_key, stats)


def leg_delay_on_date(
    train_number: str, eva_padded: str, planned_arrival_local: datetime
) -> dict | None:
    """Exact arrival delay for one train at one station on one specific day, or None
    if that day has no matching observation. Same train/station/time-of-day matching
    as leg_delay_stats, restricted to the planned arrival's calendar date."""
    if _max_day is None:
        return None
    train_number = train_number.lstrip("0")
    day = planned_arrival_local.date()
    cache_key = (train_number, eva_padded, day)
    if cache_key in _date_cache:
        return _date_cache[cache_key]

    tod = planned_arrival_local.strftime("%H:%M:%S")
    row = _conn.execute(
        """
        SELECT date_diff('minute', arrival_planned_time, arrival_change_time) AS arr_delay,
               is_canceled, reason_code
        FROM delays
        WHERE train_no = ? AND eva = ?
          AND arrival_planned_time IS NOT NULL
          AND CAST(arrival_planned_time AS DATE) = ?
          AND least(
                  abs(date_diff('minute', CAST(arrival_planned_time AS TIME), CAST(? AS TIME))),
                  1440 - abs(date_diff('minute', CAST(arrival_planned_time AS TIME), CAST(? AS TIME)))
              ) <= 120
        ORDER BY abs(date_diff('minute', arrival_planned_time, ?)),
                 arrival_planned_time, arrival_change_time
        LIMIT 1
        """,
        [train_number, eva_padded, day, tod, tod, planned_arrival_local],
    ).fetchone()

    if row is None:
        result = None
    else:
        arr_delay, canceled, reason = row
        result = {
            # no change message recorded means no delay was reported: on time
            "delayMin": None if canceled else int(arr_delay or 0),
            "canceled": bool(canceled),
            "reason": reason,
        }
    return _remember(_date_cache, cache_key, result)


def leg_departure_on_date(
    train_number: str, eva_padded: str, planned_departure_local: datetime
) -> dict | None:
    """Exact departure delay for one train at one station on one specific day, or None.
    Used to decide whether a delayed connecting train was still catchable."""
    if _max_day is None:
        return None
    train_number = train_number.lstrip("0")
    day = planned_departure_local.date()
    cache_key = (train_number, eva_padded, day)
    if cache_key in _dep_date_cache:
        return _dep_date_cache[cache_key]

    tod = planned_departure_local.strftime("%H:%M:%S")
    row = _conn.execute(
        """
        SELECT date_diff('minute', departure_planned_time, departure_change_time) AS dep_delay,
               is_canceled
        FROM delays
        WHERE train_no = ? AND eva = ?
          AND departure_planned_time IS NOT NULL
          AND CAST(departure_planned_time AS DATE) = ?
          AND least(
                  abs(date_diff('minute', CAST(departure_planned_time AS TIME), CAST(? AS TIME))),
                  1440 - abs(date_diff('minute', CAST(departure_planned_time AS TIME), CAST(? AS TIME)))
              ) <= 120
        ORDER BY abs(date_diff('minute', departure_planned_time, ?)),
                 departure_planned_time, departure_change_time
        LIMIT 1
        """,
        [train_number, eva_padded, day, tod, tod, planned_departure_local],
    ).fetchone()

    if row is None:
        result = None
    else:
        dep_delay, canceled = row
        result = {
            "delayMin": None if canceled else int(dep_delay or 0),
            "canceled": bool(canceled),
        }
    return _remember(_dep_date_cache, cache_key, result)
