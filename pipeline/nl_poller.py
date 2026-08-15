import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from nl_common import FEED_URL, decode_feed, open_obs_db, upsert_obs

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="Continuously poll the OVapi NL train GTFS-RT feed into the observation store")
    parser.add_argument("--interval", type=float, default=120, help="seconds between fetches (default: 120)")
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "nl" / "obs.sqlite", help="observation store path")
    parser.add_argument("--url", default=FEED_URL, help="GTFS-RT train updates URL")
    args = parser.parse_args()

    conn = open_obs_db(args.db)
    client = httpx.Client(timeout=60, follow_redirects=True)
    cycle = 0
    while True:
        started = time.monotonic()
        try:
            resp = client.get(args.url)
            resp.raise_for_status()
            rows = decode_feed(resp.content, int(datetime.now(timezone.utc).timestamp()))
            upsert_obs(conn, rows)
            cycle += 1
            if cycle % 30 == 1:
                total, days = conn.execute(
                    "SELECT count(*), count(DISTINCT start_date) FROM obs"
                ).fetchone()
                print(f"cycle {cycle}: {len(rows)} obs this fetch, store: {total:_} rows / {days} days", flush=True)
        except Exception as e:
            print(f"poll failed: {e}", file=sys.stderr, flush=True)
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
