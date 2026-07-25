from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from curl_cffi.requests.exceptions import HTTPError, RequestException
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles

from app import bahn_api, delays, live_delays

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Self-hosted Umami; proxied first-party under /stats/* so adblock list rules
# for analytics hosts/paths don't match.
umami = httpx.AsyncClient(base_url="http://127.0.0.1:3001", timeout=5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    delays.init()
    yield
    await bahn_api.close()
    await live_delays.close()
    await umami.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/api/locations")
async def locations(query: str, response: Response):
    response.headers["Cache-Control"] = "public, max-age=600"
    # Serve from the local station index first; only stations without delay data
    # (rural stops, POIs, addresses) fall through to bahn.de.
    local = delays.station_search(query)
    if local:
        return local
    try:
        results = await bahn_api.locations(query)
    except RequestException as e:
        raise HTTPException(502, f"bahn.de error: {e}")
    return [
        {"id": r["id"], "extId": r["extId"], "name": r["name"]}
        for r in results
        if r.get("id") and r.get("extId") and r.get("name")
    ]


def normalize_leg(abschnitt: dict, window: int, past: bool = False, live: bool = False) -> dict:
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
    if leg["walking"]:
        leg["durationSeconds"] = abschnitt.get("abschnittsDauer")

    fahrt_nr = leg["line"]["fahrtNr"]
    if not leg["walking"] and fahrt_nr and leg["plannedArrival"] and leg["destination"]["id"]:
        train = str(fahrt_nr)
        eva = delays.pad_eva(str(leg["destination"]["id"]))
        arrival = delays.to_berlin_naive(leg["plannedArrival"])
        if past:
            # on a day the parquet doesn't cover yet, IRIS answers directly; the
            # parquet stays authoritative wherever it has the day
            hit = live_delays.leg_delay_on_date(train, eva, arrival) if live else None
            leg["delayOnDate"] = hit if hit is not None else delays.leg_delay_on_date(train, eva, arrival)
        else:
            leg["delayStats"] = delays.leg_delay_stats(train, eva, arrival, window=window)
    return leg


def _live_stops(abschnitte: list[dict]) -> set[tuple[str, datetime]]:
    """Every (station, planned time) an itinerary touches, for a live IRIS prefetch."""
    stops = set()
    for a in abschnitte:
        vm = a.get("verkehrsmittel") or {}
        if vm.get("typ") != "PUBLICTRANSPORT" or not vm.get("nummer"):
            continue
        for ext_id, event in (
            (a.get("abfahrtsOrtExtId"), (a.get("abfahrt") or {}).get("sollzeit")),
            (a.get("ankunftsOrtExtId"), (a.get("ankunft") or {}).get("sollzeit")),
        ):
            if ext_id and event:
                stops.add((delays.pad_eva(str(ext_id)), delays.to_berlin_naive(event)))
    return stops


async def _warm_live(data: dict | None) -> None:
    stops = set()
    for verbindung in (data or {}).get("verbindungen", []):
        stops |= _live_stops(verbindung.get("verbindungsAbschnitte", []))
    await live_delays.warm(stops)


# minutes of slack that must remain after the median delay for a transfer to count as safe
TRANSFER_TOLERANCE_MIN = 2
# median delay exceeding the transfer time by more than this makes the transfer unlikely
UNLIKELY_EXCESS_MIN = 30


def _walk_minutes(legs: list[dict], a: int, b: int) -> float:
    """Total walking time between train legs a and b."""
    return sum(
        (
            delays.to_berlin_naive(w["plannedArrival"])
            - delays.to_berlin_naive(w["plannedDeparture"])
        ).total_seconds() / 60
        for w in legs[a + 1 : b]
        if w["plannedArrival"] and w["plannedDeparture"]
    )


def _transfer_pairs(legs: list[dict]):
    """Yield (arriving_leg_idx, departing_leg_idx, transfer_min) for each train-to-train
    transfer; walking legs in between eat into the buffer."""
    train_idx = [i for i, leg in enumerate(legs) if not leg["walking"]]
    for a, b in zip(train_idx, train_idx[1:]):
        prev, nxt = legs[a], legs[b]
        if not prev["plannedArrival"] or not nxt["plannedDeparture"]:
            continue
        gap_min = (
            delays.to_berlin_naive(nxt["plannedDeparture"])
            - delays.to_berlin_naive(prev["plannedArrival"])
        ).total_seconds() / 60
        yield a, b, gap_min - _walk_minutes(legs, a, b)


def tight_transfers(legs: list[dict]) -> list[dict]:
    """Transfers where the arriving leg's median delay leaves <= TRANSFER_TOLERANCE_MIN
    minutes to reach the next train."""
    out = []
    for a, _b, transfer_min in _transfer_pairs(legs):
        stats = legs[a].get("delayStats")
        if not stats or stats["medianDelay"] is None:
            continue
        if transfer_min - stats["medianDelay"] <= TRANSFER_TOLERANCE_MIN:
            out.append({
                "station": legs[a]["destination"]["name"],
                "legIndex": a,  # index of the arriving leg in `legs`
                "transferMinutes": max(0, round(transfer_min)),
                "medianDelay": stats["medianDelay"],
                "unlikely": stats["medianDelay"] - transfer_min > UNLIKELY_EXCESS_MIN,
            })
    return out


# how often a past journey may be re-planned after missed connections
MAX_REPLANS = 3

_replan_cache: dict[tuple, dict] = {}


def _planned_dt(leg: dict, key: str):
    return delays.to_berlin_naive(leg[key]) if leg.get(key) else None


def _actual_arrival(leg: dict):
    """Planned arrival plus that day's actual delay (unknown delay counts as on time)."""
    arr = _planned_dt(leg, "plannedArrival")
    d = leg.get("delayOnDate")
    if arr is not None and d and d["delayMin"] is not None:
        return arr + timedelta(minutes=d["delayMin"])
    return arr


def _departure_info(leg: dict, live: bool = False) -> dict | None:
    """That day's actual departure delay/cancellation of a train leg at its origin."""
    nr = leg["line"]["fahrtNr"]
    dep = _planned_dt(leg, "plannedDeparture")
    if leg["walking"] or not nr or dep is None or not leg["origin"]["id"]:
        return None
    train, eva = str(nr), delays.pad_eva(str(leg["origin"]["id"]))
    hit = live_delays.leg_departure_on_date(train, eva, dep) if live else None
    return hit if hit is not None else delays.leg_departure_on_date(train, eva, dep)


async def _replan(origin: dict, dest: dict, ready) -> dict | None:
    """Next connections origin -> dest from `ready` on, cached per request minute."""
    key = (origin["id"], dest["id"], ready.strftime("%Y-%m-%dT%H:%M"))
    if key in _replan_cache:
        return _replan_cache[key]
    try:
        data = await bahn_api.journeys(
            f"A=1@O={origin['name']}@L={origin['id']}@",
            f"A=1@O={dest['name']}@L={dest['id']}@",
            ready.strftime("%Y-%m-%dT%H:%M:%S"),
        )
    except (HTTPError, RequestException):
        return None  # transient: don't cache failures
    if len(_replan_cache) > 5000:
        _replan_cache.clear()
    _replan_cache[key] = data
    return data


async def _next_connection(origin: dict, dest: dict, ready, window: int, live: bool = False) -> list[dict] | None:
    """Earliest-arriving catchable connection from `ready` on, as normalized legs.
    Probes 45 min before `ready` first so that trains with an earlier planned
    departure that were themselves delayed past `ready` — typically the train the
    passenger just rode, continuing onward — are considered too."""
    best_arrival, best_legs = None, None
    for probe in (ready - timedelta(minutes=45), ready):
        data = await _replan(origin, dest, probe)
        if live:
            await _warm_live(data)
        for verbindung in (data or {}).get("verbindungen", []):
            rlegs = [normalize_leg(a, window, past=True, live=live) for a in verbindung.get("verbindungsAbschnitte", [])]
            rtrain = [l for l in rlegs if not l["walking"]]
            if not rtrain:
                continue
            first_dep = _planned_dt(rtrain[0], "plannedDeparture")
            if first_dep is None:
                continue
            fd = _departure_info(rtrain[0], live)
            if fd and fd["canceled"]:
                continue
            fdelay = fd["delayMin"] if fd and fd["delayMin"] is not None else 0
            slack = (first_dep + timedelta(minutes=fdelay) - ready).total_seconds() / 60
            if slack <= TRANSFER_TOLERANCE_MIN:
                continue
            est = _actual_arrival(rtrain[-1])
            if est is None:
                continue
            if best_arrival is None or est < best_arrival:
                best_arrival, best_legs = est, rlegs
        if best_legs is not None:
            break
    return best_legs


async def _simulate_walk(legs: list[dict], window: int, replans_left: int, live: bool = False) -> dict:
    """Ride `legs` with each leg's actual delay that day. A connection counts as made
    when the next train's actual departure (its own delay included) leaves more than
    TRANSFER_TOLERANCE_MIN minutes after the passenger arrives. On a miss or a
    cancellation, re-plan from that station to the final destination via bahn.de and
    continue over the replacement legs the same way.

    Returns: extra (replacement legs actually ridden, flagged "replacement"), missed
    (first miss event at this level), missedAtLegIndex (first index in `legs` not
    ridden), arrival (actual final arrival datetime), incomplete, uncertain."""
    train_idx = [i for i, l in enumerate(legs) if not l["walking"]]
    dest = legs[train_idx[-1]]["destination"]
    uncertain = False
    prev_arrival = None          # actual arrival of the previously ridden train leg
    prev_planned_arrival = None
    prev_delay = None

    for pos, i in enumerate(train_idx):
        leg = legs[i]
        d = leg.get("delayOnDate")
        dep_planned = _planned_dt(leg, "plannedDeparture")
        dep_info = _departure_info(leg, live) if pos > 0 else None
        canceled = bool(d and d["canceled"]) or bool(dep_info and dep_info["canceled"])

        missed_event = None
        ready = dep_planned
        if pos == 0:
            if canceled:
                missed_event = {
                    "legIndex": i, "station": leg["origin"]["name"],
                    "trainName": leg["line"]["name"], "canceled": True,
                    "transferMinutes": None, "delayThatDay": None,
                }
        else:
            walk_min = _walk_minutes(legs, train_idx[pos - 1], i)
            ready = prev_arrival + timedelta(minutes=walk_min) if prev_arrival else None
            if ready is None or dep_planned is None:
                uncertain = True  # can't evaluate the transfer: assume it was made
            else:
                dep_delay = dep_info["delayMin"] if dep_info and dep_info["delayMin"] is not None else 0
                slack = (dep_planned + timedelta(minutes=dep_delay) - ready).total_seconds() / 60
                if canceled or slack <= TRANSFER_TOLERANCE_MIN:
                    transfer_min = None
                    if prev_planned_arrival is not None:
                        transfer_min = max(0, round(
                            (dep_planned - prev_planned_arrival).total_seconds() / 60 - walk_min))
                    missed_event = {
                        # cancelled trains anchor under their own (struck) row,
                        # delay-caused misses under the arriving leg
                        "legIndex": i if canceled else train_idx[pos - 1],
                        "station": leg["origin"]["name"],
                        "trainName": leg["line"]["name"],
                        "canceled": canceled,
                        "transferMinutes": transfer_min,
                        "delayThatDay": prev_delay,
                    }

        if missed_event is None:
            if d is None or (d["delayMin"] is None and not d["canceled"]):
                uncertain = True
            prev_arrival = _actual_arrival(leg)
            prev_planned_arrival = _planned_dt(leg, "plannedArrival")
            prev_delay = d["delayMin"] if d else None
            continue

        # missed: re-plan from this station to the final destination
        base = {"missedAtLegIndex": i, "missed": missed_event}
        if replans_left <= 0 or ready is None or not leg["origin"]["id"] or not dest["id"]:
            return {**base, "extra": [], "arrival": None, "incomplete": True, "uncertain": uncertain}
        cand_legs = await _next_connection(leg["origin"], dest, ready, window, live)
        if cand_legs is None:
            return {**base, "extra": [], "arrival": None, "incomplete": True, "uncertain": uncertain}
        sub = await _simulate_walk(cand_legs, window, replans_left - 1, live)
        kept = cand_legs if sub["missedAtLegIndex"] is None else cand_legs[: sub["missedAtLegIndex"]]
        for l in kept:
            l["replacement"] = True
        return {
            **base,
            "extra": kept + sub["extra"],
            "arrival": sub["arrival"],
            "incomplete": sub["incomplete"],
            "uncertain": uncertain or sub["uncertain"],
        }

    return {"extra": [], "missedAtLegIndex": None, "missed": None,
            "arrival": prev_arrival, "incomplete": False, "uncertain": uncertain}


# DB Fahrgastrechte: 25% of the fare back from 60 min arrival delay, 50% from 120 min
def compensation_pct(arrival_delay: int | None) -> int | None:
    if arrival_delay is None:
        return None
    return 50 if arrival_delay >= 120 else 25 if arrival_delay >= 60 else 0


@app.get("/api/journeys")
async def journeys(
    response: Response,
    from_id: str = Query(alias="from"),
    to_id: str = Query(alias="to"),
    departure: str = Query(),
    window: int = Query(7),
    paging_ref: str | None = Query(None, alias="pagingRef"),
    mode: str = Query("future"),
):
    if window not in (7, 15, 30):
        raise HTTPException(422, "window must be 7, 15 or 30")
    if mode not in ("future", "past"):
        raise HTTPException(422, "mode must be future or past")
    response.headers["Cache-Control"] = "public, max-age=120"
    past = mode == "past"
    try:
        data = await bahn_api.journeys(from_id, to_id, departure, paging_ref)
    except HTTPError as e:
        raise HTTPException(502, f"bahn.de error {e.response.status_code}: {e.response.text[:300]}")
    except RequestException as e:
        raise HTTPException(502, f"bahn.de error: {e}")

    # days the nightly parquet hasn't reached yet are answered live from IRIS
    parquet_max = delays.coverage()[1]
    live = past and live_max_day() is not None and (parquet_max is None or departure[:10] > parquet_max.isoformat())
    if live:
        await _warm_live(data)

    journeys_out = []
    for verbindung in data.get("verbindungen", []):
        legs = [normalize_leg(a, window, past, live) for a in verbindung.get("verbindungsAbschnitte", [])]
        train_legs = [leg for leg in legs if not leg["walking"]]
        if not train_legs:
            continue

        price = (verbindung.get("angebotsPreis") or {}).get("betrag")
        journey = {
            "legs": legs,
            "transfers": verbindung.get("umstiegsAnzahl", 0),
            "durationSeconds": verbindung.get("verbindungsDauerInSeconds"),
            "price": price,
        }
        if past:
            final_d = train_legs[-1].get("delayOnDate")
            sim = await _simulate_walk(legs, window, MAX_REPLANS, live)
            if live:
                # on a live day a missing observation means "not reported yet",
                # which is a different message than "we have no data for this train"
                journey["pending"] = any(leg.get("delayOnDate") is None for leg in train_legs)
            if sim["missedAtLegIndex"] is not None:
                # a connection was missed: the realistic arrival comes from the
                # simulated continuation, not the booked itinerary's final leg
                planned_final = _planned_dt(train_legs[-1], "plannedArrival")
                arrival_delay = None
                if sim["arrival"] and planned_final:
                    arrival_delay = round((sim["arrival"] - planned_final).total_seconds() / 60)
                journey.update({
                    "arrivalDelay": arrival_delay,
                    "arrivalCanceled": bool(final_d and final_d["canceled"]),
                    "missedTransfers": [sim["missed"]] if sim["missed"] else [],
                    "compensationPct": compensation_pct(arrival_delay),
                    "simulation": {
                        "missedAtLegIndex": sim["missedAtLegIndex"],
                        "legs": sim["extra"],
                        "actualArrival": sim["arrival"].isoformat() if sim["arrival"] else None,
                        "incomplete": sim["incomplete"],
                        "uncertain": sim["uncertain"],
                    },
                })
            else:
                # every connection was made: exact arrival delay of the final leg
                arrival_delay = final_d["delayMin"] if final_d else None
                journey.update({
                    "arrivalDelay": arrival_delay,
                    "arrivalCanceled": bool(final_d and final_d["canceled"]),
                    "missedTransfers": [],
                    "compensationPct": compensation_pct(arrival_delay),
                })
        else:
            final_stats = train_legs[-1].get("delayStats")
            leg_medians = [
                s["medianDelay"]
                for leg in train_legs
                if (s := leg.get("delayStats")) and s["medianDelay"] is not None
            ]
            journey.update({
                # headline: median arrival delay at the passenger's destination (final leg)
                "delayScore": final_stats["medianDelay"] if final_stats and final_stats["medianDelay"] is not None else None,
                "maxLegMedianDelay": max(leg_medians) if leg_medians else None,
                "tightTransfers": tight_transfers(legs),
            })
        journeys_out.append(journey)

    ref = data.get("verbindungReference") or {}
    return {"journeys": journeys_out, "earlierRef": ref.get("earlier"), "laterRef": ref.get("later")}


def live_max_day() -> date | None:
    """Last day answerable live from IRIS, or None when no credentials are configured."""
    return datetime.now(ZoneInfo("Europe/Berlin")).date() if live_delays.configured() else None


@app.get("/api/coverage")
async def coverage():
    """Date range the past-journey date picker may offer: the local delay data, plus
    today when live IRIS lookups are available."""
    lo, hi = delays.coverage()
    live_hi = live_max_day()
    return {
        "minDay": lo.isoformat() if lo else None,
        "maxDay": hi.isoformat() if hi else None,
        "liveMaxDay": live_hi.isoformat() if live_hi else None,
    }


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
