from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

DELAYS_PARQUET = Path(__file__).resolve().parent.parent / "data" / "delays.parquet"
BERLIN = ZoneInfo("Europe/Berlin")

_conn: duckdb.DuckDBPyConnection | None = None
_max_day: date | None = None
_cache: dict[tuple[str, str, int], dict | None] = {}


def init():
    global _conn, _max_day
    if not DELAYS_PARQUET.exists():
        raise RuntimeError(
            f"{DELAYS_PARQUET} not found - run: uv run python pipeline/build_delay_db.py"
        )
    _conn = duckdb.connect()
    _conn.execute(f"CREATE TABLE delays AS SELECT * FROM read_parquet('{DELAYS_PARQUET}')")
    _max_day = _conn.execute(
        "SELECT max(CAST(arrival_planned_time AS DATE)) FROM delays"
        " WHERE arrival_planned_time IS NOT NULL"
    ).fetchone()[0]


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
                   date_diff('minute', arrival_planned_time, arrival_change_time) AS arr_delay,
                   is_canceled,
                   least(
                       abs(date_diff('minute', CAST(arrival_planned_time AS TIME), CAST(? AS TIME))),
                       1440 - abs(date_diff('minute', CAST(arrival_planned_time AS TIME), CAST(? AS TIME)))
                   ) AS tod_diff
            FROM delays
            WHERE ltrim(train_number, '0') = ? AND eva = ?
              AND arrival_planned_time IS NOT NULL
              AND CAST(arrival_planned_time AS DATE) >= ?
        )
        -- one stop per calendar day: closest in time-of-day; reject same-numbered
        -- trains running at a very different hour
        SELECT DISTINCT ON (day) day, arr_delay, is_canceled
        FROM candidates WHERE tod_diff <= 120
        ORDER BY day, tod_diff
        """,
        [tod, tod, train_number, eva_padded, cutoff],
    ).fetchall()

    if not rows:
        stats = None
    else:
        ok_delays = [d for _, d, canceled in rows if not canceled and d is not None]
        stats = {
            "avgDelay": round(sum(ok_delays) / len(ok_delays), 1) if ok_delays else None,
            "maxDelay": max(ok_delays) if ok_delays else None,
            "daysMatched": len(rows),
            "canceledDays": sum(1 for _, _, canceled in rows if canceled),
            "windowStart": cutoff.isoformat(),
            "windowEnd": _max_day.isoformat(),
            "days": [
                {
                    "day": day.isoformat(),
                    "delay": None if canceled else delay,
                    "canceled": bool(canceled),
                }
                for day, delay, canceled in rows
            ],
        }
    _cache[cache_key] = stats
    return stats
