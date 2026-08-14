import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from at_common import consolidate_day, open_obs_db

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def prune_old_days(days_dir: Path, cutoff: date):
    for f in sorted(days_dir.glob("*.parquet")):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if d < cutoff:
            f.unlink()
            print(f"Pruned old AT day {d}")


def main():
    parser = argparse.ArgumentParser(description="Consolidate the AT observation store into per-day delay parquets")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help="base data directory")
    parser.add_argument("--stations", type=Path, default=PROJECT_ROOT / "config" / "at_poll_stations.json", help="poll-station list")
    args = parser.parse_args()

    stations = {e["eva"]: e for e in json.loads(args.stations.read_text())}
    db_path = args.data_dir / "at" / "obs.sqlite"
    days_dir = args.data_dir / "at" / "days"
    if not db_path.exists():
        print("No observation store yet - nothing to consolidate")
        return

    conn = open_obs_db(db_path)
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    # the two most recent operating days are rewritten every run: at 05:30 night
    # trains from D-1 are still finishing, so D-1 only becomes final on D+1
    for day in (today - timedelta(days=2), today - timedelta(days=1)):
        consolidate_day(conn, day, stations, days_dir)

    cutoff = (today - timedelta(days=3)).strftime("%Y%m%d")
    deleted = conn.execute("DELETE FROM obs WHERE day < ?", (cutoff,)).rowcount
    conn.commit()
    if deleted:
        # freed pages are reused by later inserts; the file plateaus at ~3-day size
        print(f"Pruned {deleted:_} old observations")
    prune_old_days(days_dir, today - timedelta(days=41))


if __name__ == "__main__":
    main()
