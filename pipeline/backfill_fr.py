import argparse
import json
import shutil
import sys
import tarfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import brotli
import httpx

from fr_common import consolidate_day, decode_feed, open_obs_db, upsert_obs

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def download_tarball(client: httpx.Client, base: str, day: date, dest_dir: Path) -> Path | None:
    for suffix in (".tar.br", ".tar.bz2"):
        url = f"{base}french-gtfs-rt/{day}{suffix}"
        resp = client.get(url)
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        dest = dest_dir / f"{day}.tar"
        if suffix == ".tar.br":
            dest.write_bytes(brotli.decompress(resp.content))
        else:
            dest.write_bytes(resp.content)
        return dest
    return None


def ingest_tarball(conn, tar_path: Path) -> int:
    """Feed every snapshot into the store in timestamp order (member names embed the
    fetch time, so name order == chronological order and last write wins)."""
    snapshots = 0
    with tarfile.open(tar_path) as tf:
        members = sorted((m for m in tf.getmembers() if m.isfile()), key=lambda m: m.name)
        for i, member in enumerate(members):
            raw = tf.extractfile(member).read()
            try:
                rows = decode_feed(raw, i)
            except Exception as e:
                print(f"  bad snapshot {member.name}: {e}", file=sys.stderr)
                continue
            upsert_obs(conn, rows)
            snapshots += 1
    return snapshots


def main():
    parser = argparse.ArgumentParser(description="One-time FR history bootstrap from the mirror.traines.eu GTFS-RT archive")
    parser.add_argument("--days", type=int, default=35, help="days of history to build (default: 35)")
    parser.add_argument("--end-date", type=lambda s: date.fromisoformat(s), default=None, help="last day, YYYY-MM-DD (default: yesterday in Europe/Berlin)")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help="base data directory")
    parser.add_argument("--crosswalk", type=Path, default=PROJECT_ROOT / "config" / "fr_uic_to_eva.json", help="SNCF-UIC -> EVA station map")
    parser.add_argument("--mirror-base", default="https://mirror.traines.eu/", help="mirror base URL")
    args = parser.parse_args()

    end_date = args.end_date or (datetime.now(ZoneInfo("Europe/Berlin")).date() - timedelta(days=1))
    wanted = [end_date - timedelta(days=d) for d in range(args.days)][::-1]
    days_dir = args.data_dir / "fr" / "days"
    missing = [d for d in wanted if not (days_dir / f"{d}.parquet").exists()]
    if not missing:
        print("All FR day files exist, nothing to backfill")
        return

    crosswalk = json.loads(args.crosswalk.read_text())
    tmp_dir = args.data_dir / "fr" / "backfill_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    # a day's cross-midnight trains finish inside the next day's tarball, so ingest
    # through day+1 before consolidating each missing day
    ingest_days = sorted({d for m in missing for d in (m, m + timedelta(days=1)) if d <= end_date + timedelta(days=1)})

    conn = open_obs_db(tmp_dir / "obs.sqlite")
    client = httpx.Client(timeout=300, follow_redirects=True)
    ingested: set[date] = set()
    for day in ingest_days:
        tar_path = download_tarball(client, args.mirror_base, day, tmp_dir)
        if tar_path is None:
            print(f"{day}: no tarball on mirror")
            continue
        snapshots = ingest_tarball(conn, tar_path)
        tar_path.unlink()
        ingested.add(day)
        print(f"{day}: ingested {snapshots} snapshots")
        prev = day - timedelta(days=1)
        if prev in missing and prev in ingested:
            consolidate_day(conn, prev, crosswalk, days_dir)
            conn.execute("DELETE FROM obs WHERE start_date <= ?", (prev.strftime("%Y%m%d"),))
            conn.commit()
    # days whose next-day tarball wasn't on the mirror yet still deserve a (slightly
    # cross-midnight-incomplete) day file from what we have
    for d in missing:
        if d in ingested and not (days_dir / f"{d}.parquet").exists():
            consolidate_day(conn, d, crosswalk, days_dir)

    conn.close()
    shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    main()
