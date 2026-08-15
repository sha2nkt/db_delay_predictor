import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from it_common import (ROME, fetch_andamento, fetch_board, open_obs_db,
                       decode_andamento, register_trains, upsert_obs)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def discover(client, conn, stations, pace: float, fetch_ts: int) -> tuple[int, int]:
    """Sweep the departure and arrival boards of the hub stations and register
    every train run seen; a run is tracked from whichever hub it touches first."""
    now = datetime.now(ROME)
    min_day = (now - timedelta(days=1)).strftime("%Y%m%d")
    new = failed = 0
    for st in stations:
        try:
            rows = fetch_board(client, st["code"], "DEP", now) + fetch_board(client, st["code"], "ARR", now)
            new += register_trains(conn, rows, fetch_ts, min_day)
        except Exception as e:
            print(f"{st['code']} {st['name']}: {e}", file=sys.stderr, flush=True)
            failed += 1
        time.sleep(pace)
    return new, failed


def track_due(client, conn, pace: float, repoll: float, batch: int, fetch_ts: int) -> tuple[int, int]:
    """Poll andamentoTreno for every registered run that is due, until its last
    stop reports a real arrival (or it times out / disappears from the source)."""
    due = conn.execute(
        "SELECT day, train_key, origin, number, dep_ms, fails FROM trains"
        " WHERE done = 0 AND next_poll <= ? ORDER BY next_poll LIMIT ?",
        (fetch_ts, batch),
    ).fetchall()
    polled = finished = 0
    for day, key, origin, number, dep_ms, fails in due:
        polled += 1
        try:
            payload = fetch_andamento(client, origin, number, dep_ms)
        except Exception as e:
            print(f"{key}: {e}", file=sys.stderr, flush=True)
            payload = None
        if payload is None:
            # boards list some runs andamento never covers (e.g. bus replacements);
            # retire them once they are past their departure and keep failing
            fails += 1
            done = 1 if fails >= 5 and fetch_ts * 1000 > dep_ms else 0
            conn.execute(
                "UPDATE trains SET fails = ?, done = ?, next_poll = ?, last_seen = ? WHERE day = ? AND train_key = ?",
                (fails, done, fetch_ts + 300 * fails, fetch_ts, day, key))
            finished += done
        else:
            rows, last_plan_ms, arrived = decode_andamento(payload, day, key, fetch_ts)
            upsert_obs(conn, rows)
            end_ms = last_plan_ms or dep_ms
            if not rows:
                # fermate can be empty until shortly before departure; only give
                # up once the run is long past due
                done = 1 if fetch_ts * 1000 > end_ms + 4 * 3600 * 1000 else 0
            else:
                done = 1 if arrived or fetch_ts * 1000 > end_ms + 4 * 3600 * 1000 else 0
            # no point re-polling long before the run starts moving
            next_poll = max(int(fetch_ts + repoll), dep_ms // 1000 - 900)
            conn.execute(
                "UPDATE trains SET category = COALESCE(?, category), fails = 0, done = ?,"
                " next_poll = ?, last_seen = ? WHERE day = ? AND train_key = ?",
                (payload.get("categoria") or None, done, next_poll, fetch_ts, day, key))
            finished += done
        conn.commit()
        time.sleep(pace)
    return polled, finished


def main():
    parser = argparse.ArgumentParser(description="Continuously track Italian train runs from ViaggiaTreno into the IT observation store")
    parser.add_argument("--cycle", type=float, default=120, help="seconds between tracker cycles (default: 120)")
    parser.add_argument("--discover-interval", type=float, default=900, help="seconds between board discovery sweeps (default: 900)")
    parser.add_argument("--pace", type=float, default=0.25, help="seconds between requests")
    parser.add_argument("--repoll", type=float, default=900, help="seconds between andamento polls of an active run (default: 900)")
    parser.add_argument("--batch", type=int, default=400, help="max andamento polls per cycle")
    parser.add_argument("--stations", type=Path, default=PROJECT_ROOT / "config" / "it_poll_stations.json", help="discovery hub-station list")
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "it" / "obs.sqlite", help="observation store path")
    parser.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: run forever)")
    args = parser.parse_args()

    stations = json.loads(args.stations.read_text())
    conn = open_obs_db(args.db)
    client = httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    cycle, last_discovery = 0, 0.0
    while args.cycles is None or cycle < args.cycles:
        cycle += 1
        started = time.monotonic()
        fetch_ts = int(datetime.now(timezone.utc).timestamp())
        new = failed = 0
        if fetch_ts - last_discovery >= args.discover_interval:
            last_discovery = fetch_ts
            new, failed = discover(client, conn, stations, args.pace, fetch_ts)
        polled, finished = track_due(client, conn, args.pace, args.repoll, args.batch, fetch_ts)
        open_, total = conn.execute(
            "SELECT sum(done = 0), count(*) FROM trains").fetchone()
        n_obs, days = conn.execute("SELECT count(*), count(DISTINCT day) FROM obs").fetchone()
        print(f"cycle {cycle}: +{new} discovered ({failed} board errors), {polled} polled,"
              f" {finished} finished, registry: {open_ or 0} open / {total} runs,"
              f" store: {n_obs:_} stop rows / {days} days", flush=True)
        if args.cycles is None or cycle < args.cycles:
            time.sleep(max(0.0, args.cycle - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
