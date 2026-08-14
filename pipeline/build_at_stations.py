import argparse
import json
import re
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

import duckdb
import httpx
from curl_cffi import requests as curl_requests

from at_common import REQ_BASE, MGATE_URL, TRAIN_PRODUCTS, decode_board

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# year-versioned; point --gtfs-zip at the current file after a timetable change
GTFS_URL = "https://static.web.oebb.at/open-data/soll-fahrplan-gtfs/GTFS_Fahrplan_2026.zip"
ORTE_URL = "https://www.bahn.de/web/api/reiseloesung/orte"

# lid strings carry WGS84 coordinates as micro-degrees: X=longitude, Y=latitude
XY_RE = re.compile(r"@X=(-?\d+)@Y=(-?\d+)@")
TRAIN_PRODUCT_NAMES = {"ICE", "EC_IC", "IR", "REGIONAL", "SBAHN"}
# rail-replacement buses ride the train product classes, so a bus stop can have a
# non-empty mask-63 board; only these count as proof a location sees actual trains
NON_RAIL_CATS = ("Bsv", "Bus", "obu")


def rank_rail_stations(gtfs_zip: Path, top: int) -> list[tuple[str, float, float, int]]:
    """(name, lat, lon, stop events) for the busiest rail parent stations."""
    members = {n.rsplit("/", 1)[-1]: n for n in zipfile.ZipFile(gtfs_zip).namelist()}
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(gtfs_zip) as zf:
            for name in ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt"):
                (Path(td) / name).write_bytes(zf.read(members[name]))
        return duckdb.sql(f"""
            WITH rail_events AS (
                SELECT COALESCE(s.parent_station, s.stop_id) AS pid, count(*) AS n
                FROM read_csv('{td}/stop_times.txt', all_varchar=true) st
                JOIN read_csv('{td}/trips.txt', all_varchar=true) t USING (trip_id)
                JOIN read_csv('{td}/routes.txt', all_varchar=true) r USING (route_id)
                JOIN read_csv('{td}/stops.txt', all_varchar=true) s ON st.stop_id = s.stop_id
                WHERE r.route_type = '2'
                GROUP BY 1
            )
            SELECT p.stop_name, CAST(p.stop_lat AS DOUBLE), CAST(p.stop_lon AS DOUBLE), n
            FROM rail_events JOIN read_csv('{td}/stops.txt', all_varchar=true) p ON p.stop_id = pid
            ORDER BY n DESC LIMIT {top}
        """).fetchall()


def _dist_km(lat1, lon1, lat2, lon2) -> float:
    # equirectangular approximation, fine at the ~2 km scale used here
    from math import cos, radians
    dx = (lon2 - lon1) * 111.32 * cos(radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 111.32
    return (dx * dx + dy * dy) ** 0.5


def lookup_bahn_de(session, name: str, lat: float, lon: float) -> tuple[str, str] | None:
    """(extId, bahn.de name) of the matching Austrian train station, or None."""
    # GTFS suffixes like "... Bahnhof" pull bahn.de toward bus stops named
    # "Bahnhof, <town>"; the bare town/station name matches the station itself
    query = re.sub(r"\s+(Bahnhof|Bahnhst)$", "", name)
    resp = session.get(ORTE_URL, params={"suchbegriff": query, "typ": "ALL", "limit": 10}, timeout=30)
    resp.raise_for_status()
    for r in resp.json():
        ext = str(r.get("extId") or "")
        if (r.get("type") == "ST" and ext.startswith("81") and len(ext) == 7
                and TRAIN_PRODUCT_NAMES & set(r.get("products") or [])
                and r.get("lat") and r.get("lon")
                and _dist_km(lat, lon, r["lat"], r["lon"]) < 2.0):
            return ext, r["name"]
    return None


def loc_match(client: httpx.Client, name: str) -> list[tuple[str, float, float]]:
    """(extId, lat, lon) candidates from ÖBB HAFAS's own station search."""
    body = dict(REQ_BASE, svcReqL=[{"meth": "LocMatch", "req": {
        "input": {"field": "S", "loc": {"type": "S", "name": name}, "maxLoc": 8}}}])
    resp = client.post(MGATE_URL, json=body)
    resp.raise_for_status()
    out = []
    for loc in resp.json()["svcResL"][0].get("res", {}).get("match", {}).get("locL", []):
        m = XY_RE.search(loc.get("lid", ""))
        if loc.get("extId") and m:
            out.append((loc["extId"], int(m.group(2)) / 1e6, int(m.group(1)) / 1e6))
    return out


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-zäöüß]+", name.lower()) if len(t) >= 4}


def rail_board(client: httpx.Client, hafas_id: str) -> tuple[int, str | None]:
    """(number of actual-train board entries in the next 6 h, resolved location
    name) — or (-1, None) when the id is unknown to ÖBB HAFAS."""
    now = datetime.now()
    req = {"type": "DEP", "date": now.strftime("%Y%m%d"), "time": now.strftime("%H%M%S"),
           "stbLoc": {"lid": f"A=1@L={hafas_id}@"},
           "jnyFltrL": [{"type": "PROD", "mode": "INC", "value": TRAIN_PRODUCTS}],
           "dur": 360, "maxJny": 60}
    for attempt in (1, 2):
        try:
            resp = client.post(MGATE_URL, json=dict(REQ_BASE, svcReqL=[{"meth": "StationBoard", "req": req}]))
            resp.raise_for_status()
            svc = resp.json()["svcResL"][0]
            break
        except Exception:
            if attempt == 2:
                return -1, None
            time.sleep(2)
    if svc.get("err") != "OK":
        return -1, None
    rows = decode_board(svc, "0", "DEP", 0)
    rails = sum(1 for r in rows if (r[4] or "") not in NON_RAIL_CATS)
    locs = svc.get("res", {}).get("common", {}).get("locL", [])
    return rails, locs[0].get("name") if locs else None


def main():
    parser = argparse.ArgumentParser(description="Build the curated AT poll-station list from ÖBB GTFS ranking + bahn.de/HAFAS id resolution")
    parser.add_argument("--gtfs-zip", type=Path, default=PROJECT_ROOT / "data" / "at" / "raw" / "gtfs_fahrplan.zip", help="ÖBB static GTFS (downloaded if missing)")
    parser.add_argument("--top", type=int, default=200, help="stations to keep, by rail stop-event volume")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "config" / "at_poll_stations.json", help="output JSON path")
    parser.add_argument("--sleep", type=float, default=0.7, help="seconds between remote lookups")
    args = parser.parse_args()

    if not args.gtfs_zip.exists():
        args.gtfs_zip.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {GTFS_URL}")
        tmp = args.gtfs_zip.with_suffix(".zip.tmp")
        with httpx.stream("GET", GTFS_URL, timeout=300, follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.replace(args.gtfs_zip)

    ranked = rank_rail_stations(args.gtfs_zip, args.top)
    print(f"Top {len(ranked)} rail stations by GTFS stop events")

    session = curl_requests.Session(impersonate="chrome")
    client = httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    entries, skipped = [], []
    for name, lat, lon, n in ranked:
        time.sleep(args.sleep)
        try:
            hit = lookup_bahn_de(session, name, lat, lon)
        except Exception as e:
            print(f"{name}: bahn.de lookup failed ({e})", file=sys.stderr)
            skipped.append(name)
            continue
        if hit is None:
            # non-Austrian stations of cross-border runs land here too; only
            # 81xxxxx ids survive the merge filter, so skipping them is correct
            skipped.append(name)
            continue
        ext_id, bahn_name = hit
        # most stations answer boards on their bahn.de extId; the name check
        # guards against an extId that is a different station in ÖBB's namespace
        rails, loc_name = rail_board(client, ext_id)
        hafas_id = ext_id if rails > 0 and loc_name and _name_tokens(loc_name) & _name_tokens(bahn_name) else None
        if hafas_id is None and rails == 0:
            # valid location, zero actual trains: dormant (e.g. a line closure,
            # like the 2026/27 Wien Stammstrecke works) - re-run after reopening
            print(f"{name} ({ext_id}): board valid but no trains, skipping", file=sys.stderr)
            skipped.append(name)
            continue
        if hafas_id is None:
            time.sleep(args.sleep)
            for cand, clat, clon in loc_match(client, name):
                if cand != ext_id and _dist_km(lat, lon, clat, clon) < 2.0 and rail_board(client, cand)[0] > 0:
                    hafas_id = cand
                    break
                time.sleep(args.sleep)
        if hafas_id is None:
            print(f"{name} ({ext_id}): no board answers, skipping", file=sys.stderr)
            skipped.append(name)
            continue
        entries.append({"eva": ext_id.rjust(8, "0"), "hafas_id": hafas_id, "name": bahn_name})
        if len(entries) % 25 == 0:
            print(f"{len(entries)} resolved...")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(entries, ensure_ascii=False, indent=1) + "\n")
    print(f"Saved {args.out}: {len(entries)} stations ({len(skipped)} skipped)")
    if skipped:
        print("Skipped:", ", ".join(skipped[:30]) + ("..." if len(skipped) > 30 else ""))


if __name__ == "__main__":
    main()
