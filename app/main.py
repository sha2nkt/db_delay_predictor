import asyncio
import json
import logging
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from functools import lru_cache
from html import escape
from math import ceil
from pathlib import Path
from typing import Annotated, Literal, get_args
from zoneinfo import ZoneInfo

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

from app import auth, bahn_api, delays, feedback, live_delays, mailer, ratelimit, stories
from app.config import env_int

log = logging.getLogger(__name__)

# uvicorn configures only its own loggers, so records from app.* fall through to
# logging.lastResort, which drops everything below WARNING — operational INFO could
# never reach the journal. Give the package a handler of its own. The bare
# "%(message)s" is what lastResort already emits, so existing warning lines keep
# the exact shape anything grepping the journal expects.
_app_log = logging.getLogger("app")
if not _app_log.handlers:
    _app_handler = logging.StreamHandler()
    _app_handler.setFormatter(logging.Formatter("%(message)s"))
    _app_log.addHandler(_app_handler)
    _app_log.setLevel(logging.INFO)

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
    credits_watch = asyncio.create_task(mailer.watch_credits())
    yield
    credits_watch.cancel()
    await bahn_api.close()
    await live_delays.close()
    await feedback.close()
    await stories.close()
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

# The answer _if_missed_connection derives from such a payload is ~1KB, so caching it
# instead of re-deriving it costs a few hundredths of what the payload above does and
# earns a far larger cap. It also caches much better: the key below is timetable-derived
# (planned arrival plus a median delay off the local parquet), never the searcher's
# clock, so two people searching the same route hours apart share one entry — where a
# journey search key carries a departure bucket that rolls every 5 minutes.
IF_MISSED_CACHE_MAX = env_int("BAHN_IF_MISSED_CACHE_MAX", 10000)

# One rollup line per this many lookups, rather than a line each: there are hundreds
# of lookups an hour, and what anyone reading the journal wants is the hit rate moving
# over the day, next to the 429s it is meant to reduce.
IF_MISSED_LOG_EVERY = env_int("BAHN_IF_MISSED_LOG_EVERY", 200)

_if_missed_cache: "OrderedDict[tuple, dict | None]" = OrderedDict()
_if_missed_logged = (0, 0)  # (hits, misses) as of the last rollup line


def _note_if_missed(hit: bool) -> None:
    """Count an if-missed cache lookup, and roll the tally into the log periodically."""
    global _if_missed_logged
    bahn_api.metrics["if_missed_hits" if hit else "if_missed_misses"] += 1
    hits = bahn_api.metrics["if_missed_hits"]
    misses = bahn_api.metrics["if_missed_misses"]
    was_hits, was_misses = _if_missed_logged
    span = (hits - was_hits) + (misses - was_misses)
    if span < IF_MISSED_LOG_EVERY:
        return
    _if_missed_logged = (hits, misses)
    log.info(
        "if-missed cache: hit_rate=%.0f%% over last %d, hits=%d misses=%d size=%d/%d",
        100 * (hits - was_hits) / span, span, hits, misses,
        len(_if_missed_cache), IF_MISSED_CACHE_MAX,
    )


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


async def _replan(origin: dict, dest: dict, ready, source: str) -> dict | None:
    """Next connections origin -> dest from `ready` on, cached per request minute.
    `source` only tags the upstream call for the log; it is deliberately absent from
    the cache key, so a walk replan and an if-missed lookup still share an answer."""
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
            source=source,
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
        data = await _replan(origin, dest, probe, "walk")
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
    # `window` belongs in the key: it decides the span leg_delay_stats summarises
    key = (origin["id"], dest["id"], ready.strftime("%Y-%m-%dT%H:%M"), window)
    if key in _if_missed_cache:
        _if_missed_cache.move_to_end(key)
        _note_if_missed(True)
        # the entry is serialized straight into the response and never mutated on
        # the way out, so callers may share one object
        return _if_missed_cache[key]
    _note_if_missed(False)
    data = await _replan(origin, dest, ready, "if-missed")
    if data is None:
        # upstream refused: transient, and caching it would suppress the panel for
        # everyone on this route until the process restarts
        return None
    best_arrival, best_legs = None, None
    for verbindung in data.get("verbindungen", []):
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
    # "no catchable connection" is a real answer off a real payload, not a failure,
    # so it is cached too — otherwise every such transfer re-asks bahn.de forever
    result = None if best_legs is None else {"legs": best_legs, "arrival": best_arrival.isoformat()}
    _if_missed_cache[key] = result
    _if_missed_cache.move_to_end(key)
    while len(_if_missed_cache) > IF_MISSED_CACHE_MAX:
        _if_missed_cache.popitem(last=False)
    return result


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
    dticket: str = Query("0"),
    age: str = Query("adult"),
    transfer: int = Query(0),
):
    if window not in (7, 15, 30):
        raise HTTPException(422, "window must be 7, 15 or 30")
    if transfer not in (0, 10, 15, 20, 25, 30, 35, 40, 45):
        raise HTTPException(422, "transfer must be 0 or 10-45 in steps of 5")
    if mode not in ("future", "past"):
        raise HTTPException(422, "mode must be future or past")
    if age not in bahn_api.TRAVELLER_TYPES:
        raise HTTPException(422, "age must be adult, senior, young, child or toddler")
    # "1" is the legacy value from before the "all trains" mode existed; links
    # and cached frontends still send it
    dticket = {"1": "only", "only": "only", "all": "all"}.get(dticket, "off")
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
    # has no place in the past-journey compensation check; a minimum transfer
    # time would hide the tight connection someone actually took
    if past:
        dticket = "off"
        # prices play no part in the compensation check, so the traveler's age
        # bracket doesn't either; pinning it keeps past searches on one cache entry
        age = "adult"
        transfer = 0
    try:
        data, stale_age = await bahn_api.journeys(from_id, to_id, departure, paging_ref, dticket, age, transfer)
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
        # MDA-NUR-DT marks a connection fully covered by the Deutschland-Ticket
        # (only sent when the search declared one); such rows carry no price
        if any(m.get("code") == "MDA-NUR-DT" for m in verbindung.get("meldungenAsObject") or []):
            journey["dticketCovered"] = True
        elif dticket == "all" and verbindung.get("hasTeilpreis"):
            # partly covered: bahn.de already dropped the D-Ticket legs from the
            # price, so what is left is the fare for the remaining trains only
            journey["pricePartial"] = True
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
            "ifMissed": len(_if_missed_cache),
            "delayStats": len(delays._cache),
            "delayOnDate": len(delays._date_cache),
            "departureOnDate": len(delays._dep_date_cache),
        },
        "upstream": bahn_api.status(),
        "mail": mailer.status(),
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

STORIES_PATHS = {"de": "/geschichten", "en": "/stories"}
STORIES_LOGO = {"de": "/logo_delay_stories_square_german.png",
                "en": "/logo_delay_stories_square.png"}
STORIES_ALT = {"de": "Delay Geschichten", "en": "Delay Stories"}

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


@lru_cache(maxsize=4)
def _en_strings(script: str = "app.js") -> dict[str, str]:
    """The plain English strings from I18N.en in a static script (app.js, or
    stories.js for the stories page).

    The script translates the page on load, but then the markup a crawler fetches
    is German on an English URL until it renders the JS. Reusing the same table
    server-side means the English URL ships English text without a second copy
    of it. Anything not parsed here just stays German until the script runs.
    """
    src = (STATIC_DIR / script).read_text(encoding="utf-8")
    try:
        start = src.index("\n  en: {", src.index("const I18N = {"))
        block = src[start : src.index("\n  },\n", start)]
    except ValueError:
        return {}
    return {
        key: json.loads(f'"{raw}"')  # the entry is a JS string literal: unescape it
        for key, raw in _EN_ENTRY.findall(block)
    }


def _translate(html: str, script: str = "app.js") -> str:
    strings = _en_strings(script)

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
        (r'(<a href=")[^"]*(" data-i18n="footerStories")', rf"\g<1>{STORIES_PATHS[lang]}\g<2>"),
        (r'(<a id="stories-cta" class="stories-cta" href=")[^"]*', rf"\g<1>{STORIES_PATHS[lang]}"),
        (r'(<img id="stories-cta-logo" src=")[^"]*(" alt=")[^"]*',
         rf"\g<1>{STORIES_LOGO[lang]}\g<2>{STORIES_ALT[lang]}"),
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


# (title, meta description, og:description) per language; the titles mirror
# I18N.docTitle in static/stories.js, which retitles the tab on load
STORIES_META = {
    "de": (
        "Delay Geschichten – DelayBahn",
        "Horror-Geschichten von deutschen Bahnhöfen: verpasste Anschlüsse, Nächte "
        "auf dem Bahnsteig, Ansagen zum Verzweifeln. Lies mit, stimm ab oder erzähl "
        "deine eigene.",
        "Horror-Geschichten von deutschen Bahnhöfen – erzählt von denen, die dort "
        "gestrandet sind.",
    ),
    "en": (
        "Delay Stories – DelayBahn",
        "Horror stories from German train stations: missed connections, nights on "
        "the platform, announcements to despair at. Read along, vote, or tell your "
        "own.",
        "Horror stories from German train stations – told by the people stranded "
        "there.",
    ),
}


def _stories_html(lang: str) -> str:
    """Render one language of the stories page from stories.html: same page at
    /geschichten and /stories, only the instruction language differs."""
    html = (STATIC_DIR / "stories.html").read_text(encoding="utf-8")
    title, description, og_description = STORIES_META[lang]
    url = SITE + STORIES_PATHS[lang]
    other = "de" if lang == "en" else "en"
    logo = "/logo_delay_stories_tall_transparent.png" if lang == "en" else (
        "/logo_delay_stories_tall_german_transparent.png")
    subs = [
        (r'<html lang="[^"]*"', f'<html lang="{lang}"'),
        (r"<title>[^<]*</title>", f"<title>{title}</title>"),
        (r'(<meta name="description" content=")[^"]*', rf"\g<1>{description}"),
        (r'(<link rel="canonical" href=")[^"]*', rf"\g<1>{url}"),
        (r'(<link rel="alternate" hreflang="de" href=")[^"]*', rf"\g<1>{SITE}{STORIES_PATHS['de']}"),
        (r'(<link rel="alternate" hreflang="en" href=")[^"]*', rf"\g<1>{SITE}{STORIES_PATHS['en']}"),
        (r'(<link rel="alternate" hreflang="x-default" href=")[^"]*', rf"\g<1>{SITE}{STORIES_PATHS['de']}"),
        (r'(<meta property="og:url" content=")[^"]*', rf"\g<1>{url}"),
        (r'(<meta property="og:title" content=")[^"]*', rf"\g<1>{title}"),
        (r'(<meta property="og:description" content=")[^"]*', rf"\g<1>{og_description}"),
        (r'(<meta property="og:image" content=")[^"]*', rf"\g<1>{SITE}{logo}"),
        (r'(<meta property="og:locale" content=")[^"]*', rf"\g<1>{OG_LOCALE[lang]}"),
        (r'(<meta property="og:locale:alternate" content=")[^"]*', rf"\g<1>{OG_LOCALE[other]}"),
        (r'(<a href=")[^"]*(" hreflang="de")', rf"\g<1>{STORIES_PATHS['de']}\g<2>"),
        (r'(<a href=")[^"]*(" hreflang="en")', rf"\g<1>{STORIES_PATHS['en']}\g<2>"),
        # in-page navigation stays inside the current language
        (r'(<a class="logo-link" href=")[^"]*', rf"\g<1>{STORIES_PATHS[lang]}"),
        (r'(<a href=")[^"]*(" data-i18n="footerBack")', rf"\g<1>{PAGE_PATHS[('future', lang)]}\g<2>"),
    ]
    for pattern, repl in subs:
        html = re.sub(pattern, repl, html, count=1)
    if lang == "en":
        html = html.replace('data-lang="en" class="lang-btn"', 'data-lang="en" class="lang-btn active"')
        html = html.replace('data-lang="de" class="lang-btn active"', 'data-lang="de" class="lang-btn"')
        html = _translate(html, "stories.js")
    return html


@app.get("/geschichten")
async def stories_page_de() -> HTMLResponse:
    # no-cache like the other HTML documents (no ?v= buster on the document)
    return HTMLResponse(_stories_html("de"), headers={"Cache-Control": "no-cache"})


@app.get("/stories")
async def stories_page_en() -> HTMLResponse:
    return HTMLResponse(_stories_html("en"), headers={"Cache-Control": "no-cache"})


@app.get("/geschichten/")
@app.get("/geschichten.html")
async def stories_alias_de() -> RedirectResponse:
    return RedirectResponse("/geschichten", status_code=301)


@app.get("/stories/")
@app.get("/stories.html")
async def stories_alias_en() -> RedirectResponse:
    return RedirectResponse("/stories", status_code=301)


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "login.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/login/")
@app.get("/login.html")
async def login_alias() -> RedirectResponse:
    return RedirectResponse("/login", status_code=301)


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
    context: Literal["future", "past", "stories"] = "future"


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


# strip_whitespace runs before the length checks, so an all-spaces field fails
# min_length instead of slipping through as visually empty content
def _text_field(min_length: int, max_length: int):
    return Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=min_length, max_length=max_length
        ),
    ]


# The journey a story hangs off. Only the origin is required - "stranded at
# Hannover with nothing leaving" is a story too - and the departure is a local
# wall-clock stamp from the compose form's date and time inputs, not an
# instant: it is the time printed on the ticket, which is what the story is
# about. Empty string means "not given", so the column stays NOT NULL.
_StationField = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=80)
]
_DepartureField = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, pattern=r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})?$"
    ),
]


# What went wrong on the leg. A Literal rather than a free string, so an
# unknown code is a 422 at the edge instead of a row nothing can label; it
# mirrors stories.PROBLEMS, and test_stories.py pins the two together.
ProblemCode = Literal[
    "delay", "cancelled", "missed", "ac", "wc", "crowding", "wifi", "other"
]


class StoryIn(BaseModel):
    from_station: _text_field(2, 80)
    to_station: _StationField = ""
    departure: _DepartureField = ""
    # free text, not a pattern: "ICE 574", "RE 1", "S3", "IC2027" and the
    # replacement bus with no number at all are all things a story is about
    train: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=20)
    ] = ""
    # max_length caps the list, not the strings: every code may appear once,
    # so anything longer is a client that lost the plot
    problems: Annotated[list[ProblemCode], Field(max_length=len(get_args(ProblemCode)))] = []
    problem_other: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=80)
    ] = ""
    title: _text_field(3, 120)
    text: _text_field(10, 5000)


class StoryCommentIn(BaseModel):
    parent_id: int | None = None
    text: _text_field(1, 2000)


class StoryVoteIn(BaseModel):
    vote: bool = True


# a tap on a board tile: the story's leg fields without the story. Origin is
# optional at the edge only because clearing the tap (vote=false) names no
# leg; the endpoint insists on it when one is being recorded.
class ProblemReportIn(BaseModel):
    vote: bool = True
    from_station: _StationField = ""
    to_station: _StationField = ""
    departure: _DepartureField = ""
    train: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=20)
    ] = ""
    problem_other: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=80)
    ] = ""


# editing touches what the author wrote, never the journey or the problem
# codes: those are a claim about a train that ran, and a story whose leg can
# be swapped after the fact is not evidence of anything
class StoryEditIn(BaseModel):
    title: _text_field(3, 120)
    text: _text_field(10, 5000)


class CommentEditIn(BaseModel):
    text: _text_field(1, 2000)


# Addresses arrive pasted, so surrounding whitespace is trimmed before the
# shape check rather than rejected by it. The check is only a shape check -
# the emailed link is what actually proves the address exists.
_EmailField = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, max_length=254,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]


class RegisterIn(BaseModel):
    # HN-style handles: short, ASCII, no spaces - what makes a name recognizable
    # across posts. The stricter charset also keeps names trivially safe to echo.
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{2,25}$")
    email: _EmailField
    lang: Literal["de", "en"] = "de"


class RequestLinkIn(BaseModel):
    email: _EmailField
    lang: Literal["de", "en"] = "de"


class ConsumeIn(BaseModel):
    token: str = Field(min_length=1, max_length=128)


class ConsumeCodeIn(BaseModel):
    email: _EmailField
    code: str = Field(pattern=r"^[0-9]{6}$")


SESSION_COOKIE = "db_session"


def _set_session_cookie(response: Response, token: str) -> None:
    # Lax + JSON-only POST bodies double as the CSRF story: a cross-site form
    # can neither send the cookie nor produce application/json
    response.set_cookie(
        SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=True, path="/",
    )


async def _session_user(request: Request) -> dict | None:
    return await anyio.to_thread.run_sync(
        auth.session_user, request.cookies.get(SESSION_COOKIE)
    )


async def _require_user(request: Request) -> dict:
    user = await _session_user(request)
    if user is None:
        raise HTTPException(401, "login required")
    return user


def _stories_throttle(limiter: ratelimit.SlidingWindowLimiter, request: Request) -> None:
    wait = limiter.retry_after(client_ip(request))
    if wait is not None:
        raise HTTPException(
            429, "too many submissions; please slow down",
            headers={"Retry-After": str(wait)},
        )


async def _send_link(
    email: str, name: str, token: str, code: str, lang: str, kind: str
) -> None:
    """Send the magic link on the request's clock - a second or so of SMTP -
    so a relay refusal (out of Brevo credits, most likely) reaches the user as
    a 503 instead of a "check your inbox" for a mail that will not come. The
    budget the failed send spent is handed back, so the retry we just asked
    for is not swallowed by the cooldown."""
    sent = await anyio.to_thread.run_sync(
        mailer.send_magic_link, email, name, token, code, lang, kind
    )
    if not sent:
        await anyio.to_thread.run_sync(auth.refund_link, email)
        raise HTTPException(503, "email could not be sent; please try again later")


def _resend_hint() -> dict:
    """How long the login page must wait before offering "resend" again. The
    same constant for every caller - an account's real remaining cooldown
    would say when it last asked for a login, which is not something to hand
    out. Read per request so tests can move it."""
    return {"resend_after": auth.RESEND_COOLDOWN_SECONDS}


@app.post("/api/auth/register", status_code=202)
async def auth_register(reg: RegisterIn, request: Request) -> dict:
    """No session yet - that starts when the emailed link is consumed. An
    email that already has an account gets a login link to it instead of a
    second account, so a sign-up with an address already in use still ends in
    a working login."""
    _stories_throttle(auth.register_limiter, request)
    result = await anyio.to_thread.run_sync(auth.register, reg.name, reg.email)
    if result is None:
        raise HTTPException(409, "name already taken")
    kind, stored_name, magic_token, code = result
    # a spent per-account budget yields no token: the account is fine, there is
    # simply no new mail, and the answer stays the same 202 either way
    if magic_token is not None:
        await _send_link(
            auth.normalize_email(reg.email), stored_name, magic_token, code,
            reg.lang, "welcome" if kind == "new" else "login",
        )
    return _resend_hint()


@app.get("/api/auth/suggest-name")
async def auth_suggest_name(
    request: Request, response: Response, lang: Literal["de", "en"] = "de"
):
    """A free username to offer someone who has not thought of one. The client
    cannot name what it wants checked, which is the point: an availability
    endpoint taking a name would be a handle-enumeration oracle, and this
    answers the same question without being one."""
    _stories_throttle(auth.suggest_limiter, request)
    name = await anyio.to_thread.run_sync(auth.suggest_name, lang)
    if name is None:
        raise HTTPException(503, "no free name found")
    # every caller must get its own name; an edge cache serving one twice
    # would hand two people the same suggestion
    response.headers["Cache-Control"] = "no-store"
    return {"name": name}


@app.post("/api/auth/request-link", status_code=202)
async def auth_request_link(req: RequestLinkIn, request: Request) -> dict:
    """Login step one. 404 when the address has no account, so the page can
    say so and offer to create one - a login form that answers "check your
    inbox" for an address that will never receive anything is a dead end.
    That does make this an "is this address registered?" oracle; login_limiter
    is what keeps it to a trickle rather than a scrape. A spent resend budget
    still answers 202 - when an account last logged in stays private."""
    _stories_throttle(auth.login_limiter, request)
    result = await anyio.to_thread.run_sync(auth.request_link, req.email)
    if result is None:
        raise HTTPException(404, "no account for this address")
    stored_name, magic_token, code = result
    if magic_token is not None:
        await _send_link(
            auth.normalize_email(req.email), stored_name, magic_token, code,
            req.lang, "login",
        )
    return _resend_hint()


@app.post("/api/auth/consume")
async def auth_consume(body: ConsumeIn, response: Response):
    """Login step two, POSTed by the /verify landing page so a mail scanner
    prefetching the GET can't burn the single-use token."""
    result = await anyio.to_thread.run_sync(auth.consume, body.token)
    if result is None:
        raise HTTPException(401, "link invalid or expired")
    user, token = result
    _set_session_cookie(response, token)
    return {"name": user["name"]}


@app.post("/api/auth/consume-code")
async def auth_consume_code(body: ConsumeCodeIn, request: Request, response: Response):
    """The typed-code half of the same login, for when the mail was opened on
    another device. One 401 for every failure - which of wrong/expired/used-up
    applies is not something the response should spell out."""
    _stories_throttle(auth.code_limiter, request)
    result = await anyio.to_thread.run_sync(auth.consume_code, body.email, body.code)
    if result is None:
        raise HTTPException(401, "code invalid or expired")
    user, token = result
    _set_session_cookie(response, token)
    return {"name": user["name"]}


@app.post("/api/auth/logout", status_code=204)
async def auth_logout(request: Request) -> Response:
    await anyio.to_thread.run_sync(
        auth.logout, request.cookies.get(SESSION_COOKIE)
    )
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = await _session_user(request)
    return {"name": user["name"] if user else None}


@app.get("/verify")
async def verify_page() -> FileResponse:
    """Magic-link landing page: it reads ?token= client-side and redeems it
    via POST /api/auth/consume on a button click, never on the GET itself."""
    return FileResponse(
        STATIC_DIR / "verify.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/api/stories")
async def stories_index(
    request: Request,
    sort: Literal["new", "top", "liked", "commented"] = "new",
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    user = await _session_user(request)
    return await anyio.to_thread.run_sync(
        stories.list_stories, sort, limit, offset, user["id"] if user else None
    )


@app.post("/api/stories", status_code=201)
async def stories_create(story: StoryIn, request: Request):
    user = await _require_user(request)
    _stories_throttle(stories.write_limiter, request)
    created = await anyio.to_thread.run_sync(
        stories.create_story, story.from_station, story.to_station,
        story.departure, story.train, story.problems, story.problem_other,
        story.title, story.text, user["name"]
    )
    leg = (f"{story.from_station} → {story.to_station}" if story.to_station
           else story.from_station)
    # every story is public UGC on the spot: worth a phone buzz, off the request's clock
    _spawn(stories.notify(
        f"DelayBahn story: {leg} ({user['name']})",
        f"{story.title}\n\n{story.text[:500]}",
    ))
    return created


BoardSpan = Literal["week", "month", "year", "all"]


def _board(span: str, user_id: int | None) -> dict:
    """Counts over the span plus the codes the viewer tapped today - the
    tiles render both from one answer, and a tap is answered with the same
    shape so it needs no second round trip."""
    return {
        "counts": stories.count_problems(span),
        "mine": stories.my_reports(user_id) if user_id is not None else [],
    }


# public and anonymous, like reading the stories themselves; a session only
# adds which tiles are the viewer's own
@app.get("/api/stories/problems")
async def stories_problems(request: Request, span: BoardSpan = "month"):
    user = await _session_user(request)
    return await anyio.to_thread.run_sync(_board, span, user["id"] if user else None)


@app.post("/api/stories/problems/{code}")
async def stories_report(
    code: str, report: ProblemReportIn, request: Request, span: BoardSpan = "month"
):
    """A tap on a board tile: "this happened to me today, on this leg",
    story optional. Once per account, code and day, toggled like a story
    upvote; recording one needs at least the origin."""
    user = await _require_user(request)
    if report.vote and len(report.from_station) < 2:
        raise HTTPException(422, "from_station required")
    # "other" with nothing said is a number nobody could act on
    if report.vote and code == "other" and not report.problem_other:
        raise HTTPException(422, "problem_other required")
    _stories_throttle(stories.vote_limiter, request)
    result = await anyio.to_thread.run_sync(
        stories.set_report, user["id"], code, report.vote,
        report.from_station, report.to_station, report.departure, report.train,
        report.problem_other,
    )
    if result is None:
        raise HTTPException(404, "unknown problem")
    return await anyio.to_thread.run_sync(_board, span, user["id"])


@app.post("/api/stories/{story_id}/vote")
async def stories_vote(story_id: int, vote: StoryVoteIn, request: Request):
    user = await _require_user(request)
    _stories_throttle(stories.vote_limiter, request)
    result = await anyio.to_thread.run_sync(
        stories.set_vote, story_id, user["id"], vote.vote
    )
    if result is None:
        raise HTTPException(404, "story not found")
    return result


@app.get("/api/stories/{story_id}/comments")
async def stories_comments(story_id: int, request: Request):
    user = await _session_user(request)
    result = await anyio.to_thread.run_sync(
        stories.list_comments, story_id, user["id"] if user else None
    )
    if result is None:
        raise HTTPException(404, "story not found")
    return result


@app.patch("/api/stories/{story_id}")
async def stories_edit(story_id: int, edit: StoryEditIn, request: Request):
    user = await _require_user(request)
    _stories_throttle(stories.write_limiter, request)
    updated = await anyio.to_thread.run_sync(
        stories.edit_story, story_id, user["name"], edit.title, edit.text
    )
    if updated is None:
        raise HTTPException(403, "not your story, or it is already removed")
    return updated


@app.delete("/api/stories/{story_id}", status_code=204)
async def stories_delete(story_id: int, request: Request) -> Response:
    user = await _require_user(request)
    ok = await anyio.to_thread.run_sync(stories.delete_story, story_id, user["name"])
    if not ok:
        raise HTTPException(403, "not your story, or it is already removed")
    return Response(status_code=204)


# Comments are addressed by their own id rather than under their story: the id
# is unique on its own, and an edit that had to name the right story could be
# pointed at the wrong one.
@app.post("/api/comments/{comment_id}/vote")
async def comment_vote(comment_id: int, vote: StoryVoteIn, request: Request):
    user = await _require_user(request)
    _stories_throttle(stories.vote_limiter, request)
    result = await anyio.to_thread.run_sync(
        stories.set_comment_vote, comment_id, user["id"], vote.vote
    )
    if result is None:
        raise HTTPException(404, "comment not found")
    return result


@app.patch("/api/comments/{comment_id}")
async def comment_edit(comment_id: int, edit: CommentEditIn, request: Request):
    user = await _require_user(request)
    _stories_throttle(stories.write_limiter, request)
    updated = await anyio.to_thread.run_sync(
        stories.edit_comment, comment_id, user["name"], edit.text
    )
    if updated is None:
        raise HTTPException(403, "not your comment, or it is already removed")
    return updated


@app.delete("/api/comments/{comment_id}", status_code=204)
async def comment_delete(comment_id: int, request: Request) -> Response:
    user = await _require_user(request)
    ok = await anyio.to_thread.run_sync(
        stories.delete_comment, comment_id, user["name"]
    )
    if not ok:
        raise HTTPException(403, "not your comment, or it is already removed")
    return Response(status_code=204)


@app.post("/api/stories/{story_id}/comments", status_code=201)
async def stories_comment_create(
    story_id: int, comment: StoryCommentIn, request: Request
):
    user = await _require_user(request)
    _stories_throttle(stories.write_limiter, request)
    try:
        created = await anyio.to_thread.run_sync(
            stories.add_comment, story_id, comment.parent_id, user["name"], comment.text
        )
    except ValueError:
        raise HTTPException(400, "parent comment not on this story")
    if created is None:
        raise HTTPException(404, "story not found")
    _spawn(stories.notify(
        f"DelayBahn comment on story #{story_id} ({user['name']})", comment.text[:500]
    ))
    return created


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
