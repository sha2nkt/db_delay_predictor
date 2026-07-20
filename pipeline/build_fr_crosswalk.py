import argparse
import csv
import io
import json
import re
import time
import zipfile
from pathlib import Path

import httpx
from curl_cffi import requests as curl_requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRAINLINE_CSV = "https://raw.githubusercontent.com/trainline-eu/stations/master/stations.csv"
SNCF_GTFS_ZIP = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"
ORTE_URL = "https://www.bahn.de/web/api/reiseloesung/orte"

# bahn.de location ids carry the national UIC as e.g. "i=U×008768600@"; the
# extId (8700012) is a DB-assigned EVA number that differs from the UIC
UIC_TOKEN_RE = re.compile(r"i=U.00(\d{7})")


def gtfs_station_universe() -> dict[str, str]:
    """8-digit SNCF UIC -> station name for every parent station in the static GTFS
    (the same UIC appears in the GTFS-RT stop_ids)."""
    resp = httpx.get(SNCF_GTFS_ZIP, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    stations = {}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        with zf.open("stops.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                stop_id = row["stop_id"]
                if stop_id.startswith("StopArea:OCE"):
                    m = re.search(r"(\d{8})$", stop_id)
                    if m:
                        stations[m.group(1)] = row["stop_name"]
    return stations


def trainline_seed() -> dict[str, str]:
    """8-digit SNCF UIC -> zero-padded DB EVA from the trainline-eu station list."""
    resp = httpx.get(TRAINLINE_CSV, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    seed = {}
    for row in csv.DictReader(io.StringIO(resp.text), delimiter=";"):
        if row.get("country") == "FR" and row.get("uic8_sncf") and row.get("db_id"):
            seed[row["uic8_sncf"]] = row["db_id"].rjust(8, "0")
    return seed


def lookup_bahn_de(session, name: str, uic7: str) -> str | None:
    """Resolve a station via bahn.de; accept only the result whose location id
    carries the matching national UIC token."""
    resp = session.get(ORTE_URL, params={"suchbegriff": name, "typ": "ALL", "limit": 8}, timeout=30)
    resp.raise_for_status()
    for r in resp.json():
        m = UIC_TOKEN_RE.search(r.get("id", ""))
        if m and m.group(1) == uic7 and r.get("extId"):
            return str(r["extId"]).rjust(8, "0")
    return None


def main():
    parser = argparse.ArgumentParser(description="Build the SNCF-UIC -> bahn.de-EVA station crosswalk")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "config" / "fr_uic_to_eva.json", help="output JSON path")
    parser.add_argument("--sleep", type=float, default=0.7, help="seconds between bahn.de queries")
    parser.add_argument("--limit", type=int, default=None, help="max bahn.de lookups this run (resume later)")
    parser.add_argument("--verify-sample", type=int, default=20, help="seeded entries to cross-check against bahn.de")
    args = parser.parse_args()

    existing = json.loads(args.out.read_text()) if args.out.exists() else {}
    universe = gtfs_station_universe()
    seed = trainline_seed()
    print(f"GTFS universe: {len(universe)} stations, trainline seed: {len(seed)}, existing: {len(existing)}")

    out = dict(existing)
    for uic8, name in universe.items():
        if uic8 not in out and uic8 in seed:
            out[uic8] = {"eva": seed[uic8], "name": name}

    missing = [u for u in universe if u not in out]
    print(f"{sum(1 for u in universe if u in seed)} seeded, {len(missing)} to resolve via bahn.de")

    session = curl_requests.Session(impersonate="chrome")
    looked_up = mapped = 0
    for uic8 in missing:
        if args.limit is not None and looked_up >= args.limit:
            print(f"--limit {args.limit} reached, run again to continue")
            break
        looked_up += 1
        try:
            eva = lookup_bahn_de(session, universe[uic8], uic8[:7])
        except Exception as e:
            print(f"{uic8} {universe[uic8]}: lookup failed ({e})")
            time.sleep(args.sleep)
            continue
        if eva:
            out[uic8] = {"eva": eva, "name": universe[uic8]}
            mapped += 1
        time.sleep(args.sleep)
    print(f"bahn.de lookups: {looked_up}, newly mapped: {mapped}")

    # spot-check a slice of seeded entries against bahn.de to catch a stale seed
    sample = [u for u in universe if u in seed and u in out][: args.verify_sample]
    bad = 0
    for uic8 in sample:
        try:
            eva = lookup_bahn_de(session, universe[uic8], uic8[:7])
        except Exception:
            continue
        if eva and eva != out[uic8]["eva"]:
            bad += 1
            print(f"SEED MISMATCH {uic8} {universe[uic8]}: seed={out[uic8]['eva']} bahn.de={eva} - using bahn.de")
            out[uic8] = {"eva": eva, "name": universe[uic8]}
        time.sleep(args.sleep)
    if sample:
        print(f"verified {len(sample)} seeded entries, {bad} mismatches")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dict(sorted(out.items())), ensure_ascii=False, indent=1) + "\n")
    print(f"Saved {args.out}: {len(out)} of {len(universe)} GTFS stations mapped")


if __name__ == "__main__":
    main()
