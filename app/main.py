from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from curl_cffi.requests.exceptions import HTTPError, RequestException
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles

from app import bahn_api, delays

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Self-hosted Umami; proxied first-party under /stats/* so adblock list rules
# for analytics hosts/paths don't match.
umami = httpx.AsyncClient(base_url="http://127.0.0.1:3001", timeout=5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    delays.init()
    yield
    await bahn_api.client.close()
    await umami.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/api/locations")
async def locations(query: str):
    try:
        results = await bahn_api.locations(query)
    except RequestException as e:
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


# minutes of slack that must remain after the median delay for a transfer to count as safe
TRANSFER_TOLERANCE_MIN = 2


def tight_transfers(legs: list[dict]) -> list[dict]:
    """Transfers where the arriving leg's median delay leaves <= TRANSFER_TOLERANCE_MIN
    minutes to reach the next train (walking legs in between eat into the buffer)."""
    out = []
    train_idx = [i for i, leg in enumerate(legs) if not leg["walking"]]
    for a, b in zip(train_idx, train_idx[1:]):
        prev, nxt = legs[a], legs[b]
        stats = prev.get("delayStats")
        if not stats or stats["medianDelay"] is None:
            continue
        if not prev["plannedArrival"] or not nxt["plannedDeparture"]:
            continue
        gap_min = (
            delays.to_berlin_naive(nxt["plannedDeparture"])
            - delays.to_berlin_naive(prev["plannedArrival"])
        ).total_seconds() / 60
        walk_min = sum(
            (
                delays.to_berlin_naive(w["plannedArrival"])
                - delays.to_berlin_naive(w["plannedDeparture"])
            ).total_seconds() / 60
            for w in legs[a + 1 : b]
            if w["plannedArrival"] and w["plannedDeparture"]
        )
        transfer_min = gap_min - walk_min
        if transfer_min - stats["medianDelay"] <= TRANSFER_TOLERANCE_MIN:
            out.append({
                "station": prev["destination"]["name"],
                "legIndex": a,  # index of the arriving leg in `legs`
                "transferMinutes": max(0, round(transfer_min)),
                "medianDelay": stats["medianDelay"],
            })
    return out


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
    except HTTPError as e:
        raise HTTPException(502, f"bahn.de error {e.response.status_code}: {e.response.text[:300]}")
    except RequestException as e:
        raise HTTPException(502, f"bahn.de error: {e}")

    journeys_out = []
    for verbindung in data.get("verbindungen", []):
        legs = [normalize_leg(a, window) for a in verbindung.get("verbindungsAbschnitte", [])]
        train_legs = [leg for leg in legs if not leg["walking"]]
        if not train_legs:
            continue

        final_stats = train_legs[-1].get("delayStats")
        leg_medians = [
            s["medianDelay"]
            for leg in train_legs
            if (s := leg.get("delayStats")) and s["medianDelay"] is not None
        ]
        price = (verbindung.get("angebotsPreis") or {}).get("betrag")
        journeys_out.append({
            "legs": legs,
            "transfers": verbindung.get("umstiegsAnzahl", 0),
            "durationSeconds": verbindung.get("verbindungsDauerInSeconds"),
            "price": price,
            # headline: median arrival delay at the passenger's destination (final leg)
            "delayScore": final_stats["medianDelay"] if final_stats and final_stats["medianDelay"] is not None else None,
            "maxLegMedianDelay": max(leg_medians) if leg_medians else None,
            "tightTransfers": tight_transfers(legs),
        })

    ref = data.get("verbindungReference") or {}
    return {"journeys": journeys_out, "earlierRef": ref.get("earlier"), "laterRef": ref.get("later")}


@app.get("/stats/script.js")
async def umami_script():
    try:
        resp = await umami.get("/script.js")
    except httpx.HTTPError:
        raise HTTPException(502, "analytics unavailable")
    headers = {}
    if "cache-control" in resp.headers:
        headers["Cache-Control"] = resp.headers["cache-control"]
    return Response(resp.content, resp.status_code, headers, media_type="text/javascript")


@app.post("/stats/api/send")
async def umami_send(request: Request):
    headers = {
        # Umami rejects requests without a User-Agent and uses it for device stats
        "User-Agent": request.headers.get("user-agent", ""),
        "Content-Type": request.headers.get("content-type", "application/json"),
        # real client IP for geo/visitor hashing (behind Cloudflare tunnel)
        "X-Forwarded-For": request.headers.get("cf-connecting-ip")
        or (request.client.host if request.client else ""),
    }
    try:
        resp = await umami.post("/api/send", content=await request.body(), headers=headers)
    except httpx.HTTPError:
        raise HTTPException(502, "analytics unavailable")
    return Response(resp.content, resp.status_code, media_type=resp.headers.get("content-type"))


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
