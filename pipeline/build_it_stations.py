import argparse
import csv
import io
import json
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from curl_cffi import requests as curl_requests

from it_common import BASE_URL, ROME, fetch_board

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRAINLINE_CSV = "https://raw.githubusercontent.com/trainline-eu/stations/master/stations.csv"
ORTE_URL = "https://www.bahn.de/web/api/reiseloesung/orte"

# bahn.de location ids carry the national UIC as e.g. "i=U×008301700@"; the
# extId (8300046) is a DB-assigned EVA number that differs from the UIC. The
# RFI/ViaggiaTreno station code is the UIC local part ("S01700" <-> 8301700).
UIC_TOKEN_RE = re.compile(r"i=U.00(\d{7})")


def vt_station_universe() -> dict[str, dict]:
    """RFI code -> {name, tipo} for every station ViaggiaTreno lists, swept per
    region; tipoStazione 4 marks technical posts trains never serve."""
    client = httpx.Client(timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    universe = {}
    for region in range(23):
        rows = client.get(f"{BASE_URL}/elencoStazioni/{region}").json()
        for row in rows:
            code = row.get("codiceStazione")
            name = (row.get("localita") or {}).get("nomeLungo") or ""
            if not code or not name.strip() or row.get("tipoStazione") == 4:
                continue
            universe.setdefault(code, {"name": name.strip(), "tipo": row.get("tipoStazione")})
    return universe


def trainline_seed() -> dict[str, dict]:
    """RFI code -> {eva, name, main} from the trainline-eu station list, which
    carries both the ViaggiaTreno code (trenitalia_rtvt_id) and the DB EVA."""
    resp = httpx.get(TRAINLINE_CSV, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    seed = {}
    for row in csv.DictReader(io.StringIO(resp.text), delimiter=";"):
        if row.get("country") == "IT" and row.get("trenitalia_rtvt_id") and row.get("db_id"):
            seed[row["trenitalia_rtvt_id"]] = {
                "eva": row["db_id"].rjust(8, "0"),
                "name": row["name"],
                "main": row.get("is_main_station") == "t",
            }
    return seed


def lookup_bahn_de(session, name: str, uic7: str) -> tuple[str, str] | None:
    """Resolve a station via bahn.de; accept only the result whose location id
    carries the matching national UIC token."""
    resp = session.get(ORTE_URL, params={"suchbegriff": name, "typ": "ALL", "limit": 8}, timeout=30)
    resp.raise_for_status()
    for r in resp.json():
        m = UIC_TOKEN_RE.search(r.get("id", ""))
        if m and m.group(1) == uic7 and r.get("extId"):
            return str(r["extId"]).rjust(8, "0"), r.get("name") or name
    return None


def uic7_of(code: str) -> str | None:
    digits = code[1:]
    if code.startswith("S") and len(digits) == 5 and digits.isdigit():
        return "83" + digits
    return None


def main():
    parser = argparse.ArgumentParser(description="Build the RFI-code -> bahn.de-EVA crosswalk and the IT discovery hub list")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "config" / "it_scode_to_eva.json", help="crosswalk output path")
    parser.add_argument("--out-poll", type=Path, default=PROJECT_ROOT / "config" / "it_poll_stations.json", help="hub-station list output path")
    parser.add_argument("--sleep", type=float, default=0.7, help="seconds between bahn.de queries")
    parser.add_argument("--limit", type=int, default=None, help="max bahn.de lookups this run (resume later)")
    parser.add_argument("--verify-sample", type=int, default=20, help="seeded entries to cross-check against bahn.de")
    parser.add_argument("--probe-hubs", type=int, default=None, metavar="N",
                        help="probe every mapped station's departure board once and add the N busiest to the hub list")
    parser.add_argument("--probe-pace", type=float, default=0.25, help="seconds between board probes")
    args = parser.parse_args()

    existing = json.loads(args.out.read_text()) if args.out.exists() else {}
    universe = vt_station_universe()
    seed = trainline_seed()
    print(f"ViaggiaTreno universe: {len(universe)} stations, trainline seed: {len(seed)}, existing: {len(existing)}")

    out = dict(existing)
    for code in universe:
        if code not in out and code in seed:
            out[code] = {"eva": seed[code]["eva"], "name": seed[code]["name"]}

    missing = [c for c in universe if c not in out and uic7_of(c)]
    print(f"{sum(1 for c in universe if c in seed)} seeded, {len(missing)} to resolve via bahn.de")

    session = curl_requests.Session(impersonate="chrome")
    looked_up = mapped = 0
    for code in missing:
        if args.limit is not None and looked_up >= args.limit:
            print(f"--limit {args.limit} reached, run again to continue")
            break
        looked_up += 1
        try:
            hit = lookup_bahn_de(session, universe[code]["name"], uic7_of(code))
        except Exception as e:
            print(f"{code} {universe[code]['name']}: lookup failed ({e})")
            time.sleep(args.sleep)
            continue
        if hit:
            out[code] = {"eva": hit[0], "name": hit[1]}
            mapped += 1
        time.sleep(args.sleep)
    print(f"bahn.de lookups: {looked_up}, newly mapped: {mapped}")

    # spot-check a slice of seeded entries against bahn.de to catch a stale seed
    sample = [c for c in universe if c in seed and c in out][: args.verify_sample]
    bad = 0
    for code in sample:
        try:
            hit = lookup_bahn_de(session, universe[code]["name"], uic7_of(code))
        except Exception:
            continue
        if hit and hit[0] != out[code]["eva"]:
            bad += 1
            print(f"SEED MISMATCH {code} {universe[code]['name']}: seed={out[code]['eva']} bahn.de={hit[0]} - using bahn.de")
            out[code] = {"eva": hit[0], "name": hit[1]}
        time.sleep(args.sleep)
    if sample:
        print(f"verified {len(sample)} seeded entries, {bad} mismatches")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dict(sorted(out.items())), ensure_ascii=False, indent=1) + "\n")
    print(f"Saved {args.out}: {len(out)} of {len(universe)} stations mapped")

    # discovery hubs: stations trainline flags as main plus ViaggiaTreno's
    # tipoStazione-1 majors; every train touching one of them gets tracked
    keep = {c for c in universe
            if c in out and (seed.get(c, {}).get("main") or universe[c]["tipo"] == 1)}
    if args.probe_hubs:
        # the flags alone are too thin (~60 stations) to see purely regional
        # runs; rank the whole mapped universe by live departure-board size and
        # add the busiest stations
        client = httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        now = datetime.now(ROME)
        counts = {}
        for i, c in enumerate(sorted(set(out) & set(universe))):
            try:
                counts[c] = len(fetch_board(client, c, "DEP", now))
            except Exception as e:
                print(f"probe {c} {universe[c]['name']}: {e}")
            if (i + 1) % 500 == 0:
                print(f"probed {i + 1} boards...")
            time.sleep(args.probe_pace)
        ranked = sorted(counts, key=counts.get, reverse=True)
        keep.update(ranked[: args.probe_hubs])
        print(f"probed {len(counts)} boards, busiest: "
              + ", ".join(f"{universe[c]['name']}={counts[c]}" for c in ranked[:5]))
    hubs = [{"code": c, "name": universe[c]["name"]} for c in sorted(keep)]
    args.out_poll.write_text(json.dumps(hubs, ensure_ascii=False, indent=1) + "\n")
    print(f"Saved {args.out_poll}: {len(hubs)} discovery hubs")


if __name__ == "__main__":
    main()
