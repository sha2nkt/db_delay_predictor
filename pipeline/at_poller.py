import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from at_common import fetch_boards, open_obs_db, upsert_obs

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="Continuously sweep ÖBB HAFAS station boards into the AT observation store")
    parser.add_argument("--interval", type=float, default=900, help="seconds between sweep starts (default: 900)")
    parser.add_argument("--pace", type=float, default=0.7, help="seconds between station requests within a sweep")
    parser.add_argument("--dur", type=int, default=75, help="board lookahead minutes (default: 75, five sweeps per train)")
    parser.add_argument("--max-jny", type=int, default=120, help="max board entries per request side")
    parser.add_argument("--stations", type=Path, default=PROJECT_ROOT / "config" / "at_poll_stations.json", help="poll-station list")
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "at" / "obs.sqlite", help="observation store path")
    parser.add_argument("--sweeps", type=int, default=None, help="stop after N sweeps (default: run forever)")
    args = parser.parse_args()

    stations = json.loads(args.stations.read_text())
    conn = open_obs_db(args.db)
    client = httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    sweep = 0
    while args.sweeps is None or sweep < args.sweeps:
        sweep += 1
        started = time.monotonic()
        now = datetime.now()
        fetch_ts = int(datetime.now(timezone.utc).timestamp())
        n_rows = failed = 0
        for st in stations:
            try:
                rows = fetch_boards(client, st["hafas_id"], st["eva"], now,
                                    args.dur, args.max_jny, fetch_ts)
                upsert_obs(conn, rows)
                n_rows += len(rows)
            except Exception as e:
                print(f"{st['eva']} {st['name']}: {e}", file=sys.stderr, flush=True)
                failed += 1
            time.sleep(args.pace)
        total, days = conn.execute("SELECT count(*), count(DISTINCT day) FROM obs").fetchone()
        print(f"sweep {sweep}: {n_rows} board rows from {len(stations) - failed}/{len(stations)}"
              f" stations, store: {total:_} rows / {days} days", flush=True)
        if args.sweeps is None or sweep < args.sweeps:
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
