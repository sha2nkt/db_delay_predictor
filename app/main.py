from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from app import bahn_api, delays

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    delays.init()
    yield
    await bahn_api.client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/api/locations")
async def locations(query: str):
    try:
        results = await bahn_api.locations(query)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"bahn.de error: {e}")
    return [
        {"id": r["id"], "extId": r["extId"], "name": r["name"]}
        for r in results
        if r.get("id") and r.get("extId") and r.get("name")
    ]


def normalize_leg(abschnitt: dict, window: int) -> dict:
    vm = abschnitt.get("verkehrsmittel") or {}
    leg = {
        "walking": vm.get("typ") != "PUBLICTRANSPORT",
        "line": {
            "name": vm.get("mittelText") or vm.get("name"),
            "fahrtNr": vm.get("nummer"),
            "product": vm.get("produktGattung"),
        },
        "origin": {"id": abschnitt.get("abfahrtsOrtExtId"), "name": abschnitt.get("abfahrtsOrt")},
        "destination": {"id": abschnitt.get("ankunftsOrtExtId"), "name": abschnitt.get("ankunftsOrt")},
        "plannedDeparture": (abschnitt.get("abfahrt") or {}).get("sollzeit"),
        "plannedArrival": (abschnitt.get("ankunft") or {}).get("sollzeit"),
    }

    fahrt_nr = leg["line"]["fahrtNr"]
    if not leg["walking"] and fahrt_nr and leg["plannedArrival"] and leg["destination"]["id"]:
        leg["delayStats"] = delays.leg_delay_stats(
            str(fahrt_nr),
            delays.pad_eva(str(leg["destination"]["id"])),
            delays.to_berlin_naive(leg["plannedArrival"]),
            window=window,
        )
    return leg


@app.get("/api/journeys")
async def journeys(
    from_id: str = Query(alias="from"),
    to_id: str = Query(alias="to"),
    departure: str = Query(),
    window: int = Query(7),
    paging_ref: str | None = Query(None, alias="pagingRef"),
):
    if window not in (7, 15, 30):
        raise HTTPException(422, "window must be 7, 15 or 30")
    try:
        data = await bahn_api.journeys(from_id, to_id, departure, paging_ref)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"bahn.de error {e.response.status_code}: {e.response.text[:300]}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"bahn.de error: {e}")

    journeys_out = []
    for verbindung in data.get("verbindungen", []):
        legs = [normalize_leg(a, window) for a in verbindung.get("verbindungsAbschnitte", [])]
        train_legs = [leg for leg in legs if not leg["walking"]]
        if not train_legs:
            continue

        final_stats = train_legs[-1].get("delayStats")
        leg_avgs = [
            s["avgDelay"]
            for leg in train_legs
            if (s := leg.get("delayStats")) and s["avgDelay"] is not None
        ]
        price = (verbindung.get("angebotsPreis") or {}).get("betrag")
        journeys_out.append({
            "legs": legs,
            "transfers": verbindung.get("umstiegsAnzahl", 0),
            "durationSeconds": verbindung.get("verbindungsDauerInSeconds"),
            "price": price,
            # headline: avg arrival delay at the passenger's destination (final leg)
            "delayScore": final_stats["avgDelay"] if final_stats and final_stats["avgDelay"] is not None else None,
            "maxLegAvgDelay": max(leg_avgs) if leg_avgs else None,
        })

    ref = data.get("verbindungReference") or {}
    return {"journeys": journeys_out, "earlierRef": ref.get("earlier"), "laterRef": ref.get("later")}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
