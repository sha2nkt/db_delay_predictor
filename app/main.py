import asyncio
import json
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from functools import lru_cache
from html import escape
from math import asin, ceil, cos, radians, sin, sqrt
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import bahn_api, delays, feedback, live_delays, ratelimit
from app.config import env_int

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# bahn.de products IRIS never covers; a delay lookup could only ever false-match
# (e.g. tram line "1" hitting train number 1). Unknown/None products fail open so
# a new bahn.de label can't silently kill CH/FR stats. BUS includes SEV replacement
# buses, which do exist in the parquet — but their legs never matched anyway
# (6-digit municipal stop ids), so gating them is status quo with an honest label.
UNTRACKED_PRODUCTS = {"BUS", "TRAM", "UBAHN", "SCHIFF", "ANRUFPFLICHTIG"}

# Self-hosted Umami; proxied first-party under /stats/* so adblock list rules
# for analytics hosts/paths don't match.
umami = httpx.AsyncClient(base_url="http://127.0.0.1:3001", timeout=5)

# Per-client search budget: bursty legitimate use (outbound + return + paging +
# one retry) stays well inside it; only hammering trips it. Limits are per
# process, which is global in this single-worker deployment.
CLIENT_SEARCH_BURST_LIMIT = env_int("CLIENT_SEARCH_BURST_LIMIT", 10)
CLIENT_SEARCH_BURST_WINDOW = 10
CLIENT_SEARCH_PER_MINUTE_LIMIT = env_int("CLIENT_SEARCH_PER_MINUTE_LIMIT", 40)

_search_limiter = ratelimit.SlidingWindowLimiter(
    burst_limit=CLIENT_SEARCH_BURST_LIMIT,
    burst_window=CLIENT_SEARCH_BURST_WINDOW,
    sustained_limit=CLIENT_SEARCH_PER_MINUTE_LIMIT,
    sustained_window=60,
)

# Tighter budget for the nearby lookup. Unlike autocomplete it can never be
# answered from the local index, and every distinct coordinate is a guaranteed
# cache miss, so a coordinate sweep would be one bahn.de call apiece. A visitor
# taps it once, twice if the first fix was poor.
CLIENT_NEARBY_BURST_LIMIT = env_int("CLIENT_NEARBY_BURST_LIMIT", 3)
CLIENT_NEARBY_BURST_WINDOW = 10
CLIENT_NEARBY_PER_MINUTE_LIMIT = env_int("CLIENT_NEARBY_PER_MINUTE_LIMIT", 15)

_nearby_limiter = ratelimit.SlidingWindowLimiter(
    burst_limit=CLIENT_NEARBY_BURST_LIMIT,
    burst_window=CLIENT_NEARBY_BURST_WINDOW,
    sustained_limit=CLIENT_NEARBY_PER_MINUTE_LIMIT,
    sustained_window=60,
)


def client_ip(request: Request) -> str:
    """Real client IP. cf-connecting-ip is trustworthy in this deployment:
    production is reachable only through the Cloudflare tunnel (no open inbound
    port), so the header always comes from Cloudflare itself. Direct dev/LAN
    requests carry no such header and fall back to the socket address."""
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else ""
    )


def _upstream_http_error(e: bahn_api.UpstreamError) -> HTTPException:
    """Map the upstream failure taxonomy to what our clients should see: a
    throttled or unreachable bahn.de is a temporary outage (503 + Retry-After);
    only a genuinely unusable response is a bad gateway (502). Details stay
    generic — no internal URLs, headers or anti-bot specifics."""
    if isinstance(e, bahn_api.UpstreamProtocolError):
        return HTTPException(502, "bahn.de returned an unusable response")
    retry_in = max(1, ceil(e.retry_after or bahn_api.RATE_BASE_COOLDOWN))
    detail = (
        "bahn.de is rate-limiting requests; please retry shortly"
        if isinstance(e, bahn_api.UpstreamRateLimited)
        else "bahn.de is temporarily unavailable; please retry shortly"
    )
    return HTTPException(503, detail, headers={"Retry-After": str(retry_in)})


_rows = 0


def _row_count() -> int:
    return _rows


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _rows
    delays.init()
    # counted once here: COUNT(*) over 19.7M rows must not run per /health call
    _rows = delays.row_count()
    yield
    await bahn_api.close()
    await live_delays.close()
    await feedback.close()
    await umami.aclose()


app = FastAPI(lifespan=lifespan)

# 1 MiB fits the worst-case submission (512 KiB screenshot as base64, plus text)
FEEDBACK_BODY_MAX = 1024 * 1024


class FeedbackBodyLimit:
    """Refuse oversized POSTs to /api/feedback before their body is read.

    Pydantic's max_length only fires after Starlette has buffered the whole
    body in memory, and Cloudflare forwards bodies up to 100 MB. Content-Length
    is trustworthy here because h11 frames the body by it; length-less
    (chunked) posts get 411 - browsers always send a length for string bodies.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"].rstrip("/") == "/api/feedback"
        ):
            length = next(
                (v for k, v in scope["headers"] if k == b"content-length"), None
            )
            status = 0
            if length is None or not length.isdigit():
                status = 411
            elif int(length) > FEEDBACK_BODY_MAX:
                status = 413
            if status:
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [(b"content-length", b"0")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


app.add_middleware(FeedbackBodyLimit)


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
    except bahn_api.UpstreamError:
        # Autocomplete is a suggestion, not an answer: an empty list degrades to
        # "no match for this typo", which is what the user sees anyway, while an
        # error status paints the search mask red mid-typing. The station index
        # above already answers everything with delay data, so this only affects
        # rural stops, POIs and misspellings.
        response.headers["Cache-Control"] = "no-store"
        return []
    return [
        {"id": r["id"], "extId": r["extId"], "name": r["name"]}
        for r in results
        if r.get("id") and r.get("extId") and r.get("name")
    ]


# How many stations the "current position" shortcut answers with. The frontend
# fills the first one in; the rest are there for a caller that wants a choice.
NEARBY_LIMIT = 5

# Everything the delay data covers — DE/AT/CH/FR/NL/IT and their neighbours —
# with room to spare. A coordinate outside it can only be a mistake or a sweep,
# and answering it locally keeps both off bahn.de.
NEARBY_BOUNDS = (35.0, 56.5, -6.0, 20.0)  # lat min/max, lon min/max


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = radians(lat1), radians(lat2)
    a = (sin((p2 - p1) / 2) ** 2
         + cos(p1) * cos(p2) * sin(radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000 * asin(sqrt(a))


@app.get("/api/locations/nearby")
async def locations_nearby(
    request: Request,
    response: Response,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    """Stations around a coordinate, nearest first, for the search mask's
    "current position" shortcut."""
    # ~110 m is all a nearest-station lookup needs. The frontend already rounds;
    # repeating it here makes the cache key a grid cell whatever a caller sends.
    lat, lon = round(lat, 3), round(lon, 3)
    response.headers["Cache-Control"] = "public, max-age=600"
    lat_min, lat_max, lon_min, lon_max = NEARBY_BOUNDS
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        # an empty list is the truth out there — we hold no stations — and the
        # frontend already words it as "no station found nearby"
        return []
    # our own limit on this client, distinct from bahn.de throttling us (503)
    wait = _nearby_limiter.retry_after(client_ip(request))
    if wait is not None:
        bahn_api.metrics["client_rate_limited"] += 1
        # the injected response is discarded once an exception propagates, so a
        # header meant for an error status has to travel on the exception itself
        raise HTTPException(
            429, "too many location lookups; please slow down",
            headers={"Retry-After": str(wait), "Cache-Control": "no-store"},
        )
    try:
        results = await bahn_api.nearby(lat, lon)
    except bahn_api.UpstreamError as e:
        # Unlike autocomplete this answers a deliberate tap, so a failure is worth
        # saying out loud rather than degrading to "no station near you".
        error = _upstream_http_error(e)
        error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
        raise error

    ranked = []
    for r in results:
        ext = r.get("extId")
        if not (r.get("id") and ext and r.get("name")):
            continue
        # most of what bahn.de returns around a coordinate are municipal bus
        # stops; only rail ones can ever carry a delay statistic
        if not set(r.get("products") or []) - UNTRACKED_PRODUCTS:
            continue
        far = r.get("lat") is None or r.get("lon") is None
        # a stop we hold data for beats a closer one we know nothing about:
        # bahn.de labels some bus stops REGIONAL, and their 6-digit municipal
        # ids never appear in the delay data
        ranked.append((
            0 if delays.has_delay_data(ext) else 1,
            float("inf") if far else _distance_m(lat, lon, r["lat"], r["lon"]),
            {"id": r["id"], "extId": ext, "name": r["name"]},
        ))
    ranked.sort(key=lambda x: x[:2])
    return [station for *_, station in ranked[:NEARBY_LIMIT]]


def normalize_leg(abschnitt: dict, window: int, past: bool = False, live: bool = False) -> dict:
    vm = abschnitt.get("verkehrsmittel") or {}
    abfahrt = abschnitt.get("abfahrt") or {}
    ankunft = abschnitt.get("ankunft") or {}
    leg = {
        "walking": vm.get("typ") != "PUBLICTRANSPORT",
        "line": {
            "name": vm.get("mittelText") or vm.get("name"),
            "fahrtNr": vm.get("nummer"),
            "product": vm.get("produktGattung"),
            # BEF = Beförderer, the operating company ("DB Fernverkehr AG", "FlixTrain")
            "operator": next((z.get("value") for z in vm.get("zugattribute") or []
                              if z.get("key") == "BEF"), None),
        },
        "origin": {"id": abschnitt.get("abfahrtsOrtExtId"), "name": abschnitt.get("abfahrtsOrt")},
        "destination": {"id": abschnitt.get("ankunftsOrtExtId"), "name": abschnitt.get("ankunftsOrt")},
        "plannedDeparture": abfahrt.get("sollzeit"),
        "plannedArrival": ankunft.get("sollzeit"),
    }
    # bahn.de plans today's journeys with live (echtzeit) times — a connection can be
    # feasible only because of a delay. Passed on where they deviate from the schedule.
    if abfahrt.get("echtzeit") and abfahrt["echtzeit"] != abfahrt.get("sollzeit"):
        leg["departure"] = abfahrt["echtzeit"]
    if ankunft.get("echtzeit") and ankunft["echtzeit"] != ankunft.get("sollzeit"):
        leg["arrival"] = ankunft["echtzeit"]

    fahrt_nr = leg["line"]["fahrtNr"]
    tracked = leg["line"]["product"] not in UNTRACKED_PRODUCTS
    if not leg["walking"] and tracked and fahrt_nr and leg["plannedArrival"] and leg["destination"]["id"]:
        train = str(fahrt_nr).replace(" ", "")
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
        if (vm.get("typ") != "PUBLICTRANSPORT" or not vm.get("nummer")
                or vm.get("produktGattung") in UNTRACKED_PRODUCTS):
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
    transfer; walking legs in between eat into the buffer. Live (echtzeit) times win over
    the schedule: on today's connections bahn.de plans with them, and a transfer that
    looks impossible on paper can be fine because the next train is itself delayed."""
    train_idx = [i for i, leg in enumerate(legs) if not leg["walking"]]
    for a, b in zip(train_idx, train_idx[1:]):
        prev, nxt = legs[a], legs[b]
        if not prev["plannedArrival"] or not nxt["plannedDeparture"]:
            continue
        gap_min = (
            delays.to_berlin_naive(nxt.get("departure") or nxt["plannedDeparture"])
            - delays.to_berlin_naive(prev.get("arrival") or prev["plannedArrival"])
        ).total_seconds() / 60
        yield a, b, gap_min - _walk_minutes(legs, a, b)


def tight_transfers(legs: list[dict]) -> list[dict]:
    """Transfers where the arriving leg's median delay leaves <= TRANSFER_TOLERANCE_MIN
    minutes to reach the next train."""
    out = []
    for a, b, transfer_min in _transfer_pairs(legs):
        stats = legs[a].get("delayStats")
        if not stats or stats["medianDelay"] is None:
            continue
        if transfer_min - stats["medianDelay"] <= TRANSFER_TOLERANCE_MIN:
            out.append({
                "station": legs[a]["destination"]["name"],
                "legIndex": a,  # index of the arriving leg in `legs`
                "depLegIndex": b,  # index of the departing leg that would be missed
                "transferMinutes": max(0, round(transfer_min)),
                "medianDelay": stats["medianDelay"],
                "unlikely": stats["medianDelay"] - transfer_min > UNLIKELY_EXCESS_MIN,
            })
    return out


# how often a past journey may be re-planned after missed connections
MAX_REPLANS = 3

# ceiling on the optional "if you miss this transfer" lookups one future-mode
# search may trigger; each is an extra bahn.de call, and the first few cover the
# transfers a reader actually opens
MAX_IF_MISSED_REPLANS = env_int("BAHN_MAX_IF_MISSED_REPLANS", 3)

# Holds raw bahn.de journey payloads (100-500KB each). The previous wholesale clear at
# 5000 entries allowed ~1-2GB of transient growth on top of a ~6GB process, so evict LRU.
REPLAN_CACHE_MAX = 500

_replan_cache: "OrderedDict[tuple, dict]" = OrderedDict()


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
    if (leg["walking"] or not nr or dep is None or not leg["origin"]["id"]
            or leg["line"]["product"] in UNTRACKED_PRODUCTS):
        return None
    train, eva = str(nr).replace(" ", ""), delays.pad_eva(str(leg["origin"]["id"]))
    hit = live_delays.leg_departure_on_date(train, eva, dep) if live else None
    return hit if hit is not None else delays.leg_departure_on_date(train, eva, dep)


def _flix(leg: dict) -> bool:
    """FlixTrain runs its own tariff: a DB ticket is not valid there and the trains
    are reservation-bound, so a passenger stranded by a missed DB connection cannot
    board one. bahn.de lists them among the results all the same, which is why every
    replacement connection has to drop them."""
    line = leg["line"]
    return ("flix" in (line.get("operator") or "").lower()
            or str(line.get("name") or "").upper().startswith("FLX"))


async def _replan(origin: dict, dest: dict, ready) -> dict | None:
    """Next connections origin -> dest from `ready` on, cached per request minute."""
    key = (origin["id"], dest["id"], ready.strftime("%Y-%m-%dT%H:%M"))
    if key in _replan_cache:
        _replan_cache.move_to_end(key)
        return _replan_cache[key]
    try:
        # a stale fallback answer (age ignored) beats no replan at all
        data, _ = await bahn_api.journeys(
            f"A=1@O={origin['name']}@L={origin['id']}@",
            f"A=1@O={dest['name']}@L={dest['id']}@",
            ready.strftime("%Y-%m-%dT%H:%M:%S"),
        )
    except bahn_api.UpstreamError:
        return None  # transient: don't cache failures
    _replan_cache[key] = data
    _replan_cache.move_to_end(key)
    while len(_replan_cache) > REPLAN_CACHE_MAX:
        _replan_cache.popitem(last=False)
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
            if not rtrain or any(_flix(l) for l in rtrain):
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


async def _if_missed_connection(legs: list[dict], tt: dict, window: int) -> dict | None:
    """Future mode: the next realistic connection to the journey's destination if the
    tight transfer `tt` is missed. The passenger is assumed to reach the departure
    point at planned arrival + the arriving leg's median delay; a connection counts
    as catchable when its planned departure leaves more than TRANSFER_TOLERANCE_MIN
    minutes after that."""
    a, b = tt["legIndex"], tt["depLegIndex"]
    arr = _planned_dt(legs[a], "plannedArrival")
    origin = legs[b]["origin"]
    dest = [l for l in legs if not l["walking"]][-1]["destination"]
    if arr is None or not origin["id"] or not dest["id"]:
        return None
    ready = arr + timedelta(minutes=_walk_minutes(legs, a, b) + tt["medianDelay"])
    data = await _replan(origin, dest, ready)
    best_arrival, best_legs = None, None
    for verbindung in (data or {}).get("verbindungen", []):
        rlegs = [normalize_leg(x, window) for x in verbindung.get("verbindungsAbschnitte", [])]
        rtrain = [l for l in rlegs if not l["walking"]]
        if not rtrain or any(_flix(l) for l in rtrain):
            continue
        first_dep = _planned_dt(rtrain[0], "plannedDeparture")
        last_arr = _planned_dt(rtrain[-1], "plannedArrival")
        if first_dep is None or last_arr is None:
            continue
        # excludes the missed train itself: its slack is <= the tolerance by definition
        if (first_dep - ready).total_seconds() / 60 <= TRANSFER_TOLERANCE_MIN:
            continue
        if best_arrival is None or last_arr < best_arrival:
            best_arrival, best_legs = last_arr, rlegs
    if best_legs is None:
        return None
    return {"legs": best_legs, "arrival": best_arrival.isoformat()}


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
    request: Request,
    response: Response,
    from_id: str = Query(alias="from"),
    to_id: str = Query(alias="to"),
    departure: str = Query(),
    window: int = Query(7),
    paging_ref: str | None = Query(None, alias="pagingRef"),
    mode: str = Query("future"),
    dticket: bool = Query(False),
):
    if window not in (7, 15, 30):
        raise HTTPException(422, "window must be 7, 15 or 30")
    if mode not in ("future", "past"):
        raise HTTPException(422, "mode must be future or past")
    # our own limit on this client, distinct from bahn.de throttling us (503)
    wait = _search_limiter.retry_after(client_ip(request))
    if wait is not None:
        bahn_api.metrics["client_rate_limited"] += 1
        raise HTTPException(
            429, "too many searches; please slow down",
            headers={"Retry-After": str(wait)},
        )
    response.headers["Cache-Control"] = "public, max-age=120"
    past = mode == "past"
    # the D-Ticket is excluded from Fahrgastrechte compensation, so the filter
    # has no place in the past-journey compensation check
    dticket = dticket and not past
    try:
        data, stale_age = await bahn_api.journeys(from_id, to_id, departure, paging_ref, dticket)
    except bahn_api.UpstreamError as e:
        raise _upstream_http_error(e)
    if stale_age:
        # a degraded answer must not be cached by the edge or the browser: the
        # fresh one can be a single upstream recovery away
        response.headers["Cache-Control"] = "no-store"

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
        ez_duration = verbindung.get("ezVerbindungsDauerInSeconds")
        if ez_duration and ez_duration != journey["durationSeconds"]:
            journey["ezDurationSeconds"] = ez_duration
        if past:
            final_d = train_legs[-1].get("delayOnDate")
            sim = await _simulate_walk(legs, window, MAX_REPLANS, live)
            if live:
                # on a live day a missing observation means "not reported yet",
                # which is a different message than "we have no data for this train";
                # untracked products (tram, bus, ...) never report, so they must not
                # hold the journey pending forever
                journey["pending"] = any(
                    leg.get("delayOnDate") is None
                    for leg in train_legs
                    if leg["line"]["product"] not in UNTRACKED_PRODUCTS
                )
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
            # slack of each transfer after the arriving leg's median delay; the
            # riskiest one ranks the journey in the risk sort - covers all
            # transfers, not just tight ones, so no-risk journeys rank too
            transfer_margins = [
                transfer_min - stats["medianDelay"]
                for a, _b, transfer_min in _transfer_pairs(legs)
                if (stats := legs[a].get("delayStats")) and stats["medianDelay"] is not None
            ]
            journey.update({
                # headline: median arrival delay at the passenger's destination (final leg)
                "delayScore": final_stats["medianDelay"] if final_stats and final_stats["medianDelay"] is not None else None,
                "maxLegMedianDelay": max(leg_medians) if leg_medians else None,
                "tightTransfers": tight_transfers(legs),
                "minTransferMargin": round(min(transfer_margins), 1) if transfer_margins else None,
            })
        journeys_out.append(journey)

    # One extra bahn.de replan per tight transfer, on top of the search itself —
    # the biggest multiplier on our upstream rate, and only an enrichment (the
    # disclosure simply stays closed without it). So it is skipped whenever
    # bahn.de is already straining, and capped per search either way; _replan and
    # the bahn_api task cache dedupe identical (station, destination, minute)
    # lookups on top of that.
    if not past and not stale_age and bahn_api.healthy():
        async def fill(j: dict, tt: dict) -> None:
            tt["ifMissed"] = await _if_missed_connection(j["legs"], tt, window)

        pending = [(j, tt) for j in journeys_out for tt in j["tightTransfers"]]
        await asyncio.gather(*(fill(j, tt) for j, tt in pending[:MAX_IF_MISSED_REPLANS]))

    ref = data.get("verbindungReference") or {}
    out = {"journeys": journeys_out, "earlierRef": ref.get("earlier"), "laterRef": ref.get("later")}
    if stale_age:
        out["staleSeconds"] = stale_age
    return out


def live_max_day() -> date | None:
    """Last day answerable live from IRIS, or None when no credentials are configured."""
    return datetime.now(ZoneInfo("Europe/Berlin")).date() if live_delays.configured() else None


@app.get("/health")
async def health():
    """Liveness probe and event-loop canary: pure in-memory, no I/O, so a slow response
    means the loop is blocked rather than that this endpoint is expensive."""
    return {
        "ok": True,
        "rows": _row_count(),
        "caches": {
            "bahn": len(bahn_api._cache),
            "replan": len(_replan_cache),
            "delayStats": len(delays._cache),
            "delayOnDate": len(delays._date_cache),
            "departureOnDate": len(delays._dep_date_cache),
        },
        "upstream": bahn_api.status(),
    }


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


# Every (mode, language) pair is its own indexable URL, so Google can rank the
# German and English versions separately instead of seeing one page whose text a
# JS toggle rewrites. German keeps the existing URLs; English lives under /en/.
# static/index.html stays the single source of truth for the markup — it *is* the
# German homepage, and the other three variants are that same file with its head
# tags, body class and language-dependent links rewritten per request, so the
# pages can never drift apart.
SITE = "https://delaybahn.com"

PAGE_PATHS = {
    ("future", "de"): "/",
    ("future", "en"): "/en/",
    ("past", "de"): "/entschaedigung",
    ("past", "en"): "/en/compensation",
}

OG_LOCALE = {"de": "de_DE", "en": "en_US"}

# (title, meta description, og:description) per variant. The titles mirror
# I18N.pageTitle / pageTitlePast in static/app.js, which retitles the tab on load.
# The German homepage is served straight from index.html rather than rendered, so
# its entry is what that file's head has to say — keep the two in step.
PAGE_META = {
    ("future", "de"): (
        "DelayBahn – DB Verbindungssuche mit Verspätungsstatistik",
        "DelayBahn zeigt vor der Buchung, wie verspätet deine DB-Verbindung in den "
        "letzten Wochen wirklich war – damit du auf den Zug mit der besseren Bilanz "
        "setzen kannst. Den Zug buchen, nicht die Verspätung.",
        "DB-Verbindungen suchen und vorab sehen, wie pünktlich die Züge in den letzten "
        "Wochen wirklich waren. Den Zug buchen, nicht die Verspätung.",
    ),
    ("future", "en"): (
        "DelayBahn – DB Connection Search with Delay Statistics",
        "DelayBahn shows before you book how delayed your DB connection really was "
        "over the past weeks, so that you can choose the train with the better track "
        "record. Book the train, not the delay.",
        "Search DB connections and see up front how punctual the trains really were. "
        "Book the train, not the delay.",
    ),
    ("past", "de"): (
        "Bahn-Entschädigung prüfen – Verspätungs-Check für vergangene Reisen | DelayBahn",
        "Vergangene DB-Reise bei DelayBahn eingeben und sehen, wie sie wirklich verlief: "
        "Verspätungen, verpasste Anschlüsse und Entschädigung nach EU-Fahrgastrechten – "
        "25 % ab 60 min, 50 % ab 120 min Verspätung am Ziel.",
        "Vergangene DB-Reise eingeben und sehen, wie sie wirklich verlief: Verspätungen, "
        "verpasste Anschlüsse und Entschädigung nach EU-Fahrgastrechten – "
        "25 % ab 60 min, 50 % ab 120 min Verspätung am Ziel.",
    ),
    ("past", "en"): (
        "Check DB delay compensation – delay check for past journeys | DelayBahn",
        "Enter a past Deutsche Bahn journey on DelayBahn and see how it actually went: "
        "delays, missed connections and compensation under EU passenger rights – "
        "25% from 60 min, 50% from 120 min delay on arrival.",
        "Enter a past Deutsche Bahn journey and see how it actually went: delays, missed "
        "connections and compensation under EU passenger rights – 25% from 60 min, "
        "50% from 120 min delay on arrival.",
    ),
}

EN_APP_DESCRIPTION = (
    "DB connection search with delay statistics: shows for every connection how "
    "punctual the trains were over the past weeks, and checks past journeys for "
    "delays and compensation claims."
)


# One `key: "text",` entry of I18N.en in static/app.js. Parameterised entries are
# arrow functions and deliberately don't match — they need runtime values anyway.
_EN_ENTRY = re.compile(r'^    ([A-Za-z0-9_]+): "((?:[^"\\]|\\.)*)",$', re.M)
# an element carrying data-i18n; its content is plain text (no nested markup)
_I18N_EL = re.compile(
    r'(?P<open><(?P<tag>\w+)[^<>]*\sdata-i18n="(?P<key>[A-Za-z0-9_]+)"[^<>]*>)'
    r"[^<>]*(?P<close></(?P=tag)>)"
)


@lru_cache(maxsize=1)
def _en_strings() -> dict[str, str]:
    """The plain English strings from I18N.en in static/app.js.

    app.js translates the page on load, but then the markup a crawler fetches is
    German on an English URL until it renders the JS. Reusing the same table
    server-side means /en/ ships English text without a second copy of it.
    Anything not parsed here just stays German until app.js runs.
    """
    src = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    try:
        start = src.index("\n  en: {", src.index("const I18N = {"))
        block = src[start : src.index("\n  },\n", start)]
    except ValueError:
        return {}
    return {
        key: json.loads(f'"{raw}"')  # the entry is a JS string literal: unescape it
        for key, raw in _EN_ENTRY.findall(block)
    }


def _translate(html: str) -> str:
    strings = _en_strings()

    def sub(m: re.Match[str]) -> str:
        text = strings.get(m["key"])
        return m[0] if text is None else m["open"] + escape(text) + m["close"]

    return _I18N_EL.sub(sub, html)


def _page_html(mode: str, lang: str) -> str:
    """Render one (mode, language) variant of the single-page app from index.html."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    title, description, og_description = PAGE_META[(mode, lang)]
    url = SITE + PAGE_PATHS[(mode, lang)]
    home = PAGE_PATHS[("future", lang)]
    past = PAGE_PATHS[("past", lang)]
    subs = [
        (r'<html lang="[^"]*"', f'<html lang="{lang}"'),
        (r"<title>[^<]*</title>", f"<title>{title}</title>"),
        (r'(<meta name="description" content=")[^"]*', rf"\g<1>{description}"),
        (r'(<link rel="canonical" href=")[^"]*', rf"\g<1>{url}"),
        # both languages of *this* page; x-default sends unmatched locales to German
        (r'(<link rel="alternate" hreflang="de" href=")[^"]*',
         rf"\g<1>{SITE}{PAGE_PATHS[(mode, 'de')]}"),
        (r'(<link rel="alternate" hreflang="en" href=")[^"]*',
         rf"\g<1>{SITE}{PAGE_PATHS[(mode, 'en')]}"),
        (r'(<link rel="alternate" hreflang="x-default" href=")[^"]*',
         rf"\g<1>{SITE}{PAGE_PATHS[(mode, 'de')]}"),
        (r'(<meta property="og:url" content=")[^"]*', rf"\g<1>{url}"),
        (r'(<meta property="og:title" content=")[^"]*', rf"\g<1>{title}"),
        (r'(<meta property="og:description" content=")[^"]*', rf"\g<1>{og_description}"),
        (r'(<meta property="og:locale" content=")[^"]*', rf"\g<1>{OG_LOCALE[lang]}"),
        (r'(<meta property="og:locale:alternate" content=")[^"]*',
         rf"\g<1>{OG_LOCALE['de' if lang == 'en' else 'en']}"),
        # the toggle is a pair of real links, one per language URL of this page
        (r'(<a href=")[^"]*(" hreflang="de")', rf"\g<1>{PAGE_PATHS[(mode, 'de')]}\g<2>"),
        (r'(<a href=")[^"]*(" hreflang="en")', rf"\g<1>{PAGE_PATHS[(mode, 'en')]}\g<2>"),
        # in-page navigation must stay inside the current language
        (r'(<a class="logo-link" href=")[^"]*', rf"\g<1>{home}"),
        (r'(<a id="refund-nav" class="refund-nav" href=")[^"]*', rf"\g<1>{past}"),
        (r'(<a id="refund-cta" class="refund-cta" href=")[^"]*', rf"\g<1>{past}"),
        (r'(<a id="past-exit" class="past-exit" href=")[^"]*', rf"\g<1>{home}"),
    ]
    if lang == "en":
        subs += [
            (r'(<link rel="manifest" href=")[^"]*', r"\g<1>/en/manifest.json"),
            (r'("description": ")DB-Verbindungssuche[^"]*', rf"\g<1>{EN_APP_DESCRIPTION}"),
        ]
    if mode == "past":
        subs += [
            (r"<body>", '<body class="past-mode">'),
            # the sub-page's heading is the past banner title
            (r'<strong data-i18n="pastTitle">([^<]*)</strong>',
             r'<h1 data-i18n="pastTitle">\1</h1>'),
        ]
    for pattern, repl in subs:
        html = re.sub(pattern, repl, html, count=1)
    if lang == "en":
        # the JSON-LD "url" sits on both the WebSite and the WebApplication node
        html = html.replace('"url": "https://delaybahn.com/"', f'"url": "{SITE}/en/"')
        html = html.replace('data-lang="en" class="lang-btn"', 'data-lang="en" class="lang-btn active"')
        html = html.replace('data-lang="de" class="lang-btn active"', 'data-lang="de" class="lang-btn"')
        html = _translate(html)
    return html


def _page_response(mode: str, lang: str) -> HTMLResponse:
    # no-cache for the same reason HtmlNoCacheStatic sets it on the German homepage:
    # these documents carry no ?v= buster, so heuristic freshness would otherwise
    # keep serving a returning visitor stale markup for days
    return HTMLResponse(_page_html(mode, lang), headers={"Cache-Control": "no-cache"})


@app.get("/entschaedigung")
async def entschaedigung_page() -> HTMLResponse:
    return _page_response("past", "de")


@app.get("/entschaedigung/")
async def entschaedigung_slash() -> RedirectResponse:
    # one canonical spelling per page, so the slashed form never gets indexed too
    return RedirectResponse("/entschaedigung", status_code=301)


@app.get("/en/")
async def en_home() -> HTMLResponse:
    return _page_response("future", "en")


@app.get("/en")
async def en_home_no_slash() -> RedirectResponse:
    return RedirectResponse("/en/", status_code=301)


@app.get("/en/compensation")
async def en_compensation_page() -> HTMLResponse:
    return _page_response("past", "en")


@app.get("/en/compensation/")
async def en_compensation_slash() -> RedirectResponse:
    return RedirectResponse("/en/compensation", status_code=301)


# German has no /de/ prefix, and the static mount would otherwise answer on
# /index.html too — send both to the one canonical German homepage.
@app.get("/de")
@app.get("/de/")
@app.get("/index.html")
async def de_home() -> RedirectResponse:
    return RedirectResponse("/", status_code=301)


@app.get("/en/manifest.json")
async def en_manifest() -> Response:
    """English install target: same app, but it opens on the English URL."""
    data = json.loads((STATIC_DIR / "manifest.json").read_text(encoding="utf-8"))
    data |= {
        "id": "/en/",
        "start_url": "/en/",
        "lang": "en",
        "name": "DelayBahn – DB Delay Check",
        "description": EN_APP_DESCRIPTION,
        "shortcuts": [
            {
                "name": "Check compensation",
                "short_name": "Compensation",
                "description": "Check a past journey for delays and compensation",
                "url": "/en/compensation",
            }
        ],
    }
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json")


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


class Feedback(BaseModel):
    # sid is generated per prompt by the browser: the vote lands first and the
    # optional comment follows under the same id, so the two become one row
    sid: str = Field(min_length=8, max_length=64)
    vote: Literal["up", "down"]
    text: str = Field("", max_length=1000)
    # optional screenshot as a base64 image data URL; the length bound is the
    # 512 KiB binary cap in base64 clothing plus header slack
    shot: str = Field("", max_length=720_000)
    lang: Literal["de", "en"] = "de"
    context: Literal["future", "past"] = "future"


_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    # a bare create_task can be garbage-collected mid-flight
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@app.post("/api/feedback", status_code=204)
async def submit_feedback(fb: Feedback, request: Request) -> Response:
    if feedback.throttled(client_ip(request)):
        raise HTTPException(429, "too many submissions")
    text = fb.text.strip()
    try:
        shot = feedback.decode_shot(fb.shot)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    # sqlite3 blocks and /health doubles as an event-loop canary: keep the write off it
    dropped = await anyio.to_thread.run_sync(
        feedback.save, fb.sid, fb.vote, text, fb.lang, fb.context,
        shot[0] if shot else None,
    )
    if dropped:
        # budget full: don't forward the image to ntfy either - under a flood
        # the image pushes would just move the spam to the phone
        shot = None
        if feedback.budget_warn_due():
            _spawn(feedback.notify_budget())
    # only a comment or screenshot is worth a phone buzz, and never on the request's clock
    if text or shot:
        _spawn(feedback.notify(fb.vote, text, fb.lang, fb.context, shot))
    return Response(status_code=204)


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # The service worker has a fixed URL, so it can't be ?v=-cache-busted like
    # the other static assets. Without this, Cloudflare edge-caches it (default
    # ~4 h) and a new SHELL_VERSION can't roll out. no-cache forces the browser
    # and Cloudflare to revalidate every time, so updates go live immediately.
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


class HtmlNoCacheStatic(StaticFiles):
    """The HTML documents carry no ?v= buster, and StaticFiles sends them with a
    Last-Modified but no Cache-Control. Browsers then fall back to heuristic
    freshness (~10 % of the file's age), so a page untouched for weeks can be
    served from the local cache for days and never picks up markup changes.
    no-cache only forces revalidation — Last-Modified still yields cheap 304s."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        if (response.media_type or "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", HtmlNoCacheStatic(directory=STATIC_DIR, html=True), name="static")
