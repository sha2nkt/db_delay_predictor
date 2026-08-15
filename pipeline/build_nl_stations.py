import argparse
import csv
import io
import json
from collections import Counter
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Rijden de Treinen stations dataset (CC0): NS/IFF station code -> UIC, names, country
STATIONS_URL = "https://opendata.rijdendetreinen.nl/public/stations/stations-2023-09.csv"


def main():
    parser = argparse.ArgumentParser(description="Build the IFF station-code -> EVA crosswalk from the Rijden de Treinen stations dataset")
    parser.add_argument("--url", default=STATIONS_URL, help="stations CSV URL")
    parser.add_argument("--csv", type=Path, default=None, help="local stations CSV to use instead of downloading")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "config" / "nl_stations.json", help="output crosswalk path")
    args = parser.parse_args()

    if args.csv:
        text = args.csv.read_text()
    else:
        resp = httpx.get(args.url, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text

    # Foreign stations of cross-border runs are kept with their real UIC: they pad to
    # non-084 EVAs and the merge prefix filter drops those rows, same as FR/CH.
    crosswalk = {}
    countries = Counter()
    for row in csv.DictReader(io.StringIO(text)):
        code = row["code"].strip().lower()
        uic = row["uic"].strip()
        if not code or not uic:
            continue
        crosswalk[code] = {"eva": f"0{uic}", "name": row["name_long"].strip()}
        countries[row["country"].strip()] += 1

    args.out.write_text(json.dumps(crosswalk, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    print(f"{len(crosswalk)} stations -> {args.out}")
    print("by country:", dict(countries.most_common()))


if __name__ == "__main__":
    main()
