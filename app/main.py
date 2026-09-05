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

from app import (
    auth, bahn_api, delays, feedback, leaderboard, live_delays, mailer, ratelimit, reports,
    stories, trips,
)
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


async def _warm_leaderboard() -> None:
    try:
        await asyncio.to_thread(leaderboard.get)
    except Exception:
        log.exception("leaderboard warm-up failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _rows
    delays.init()
    # counted once here: COUNT(*) over 19.7M rows must not run per /health call
    _rows = delays.row_count()
    # the leaderboard's one pass over the table runs off the loop: the first
    # visitor never waits for it, and startup stays sub-second
    asyncio.create_task(_warm_leaderboard())
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


async def _replan(origin: dict, dest: dict, ready, source: str,
                  dticket: str = "off", products: tuple[str, ...] | None = None) -> dict | None:
    """Next connections origin -> dest from `ready` on, cached per request minute.
    `source` only tags the upstream call for the log; it is deliberately absent from
    the cache key, so a walk replan and an if-missed lookup still share an answer.
    dticket/products carry the search's own restrictions when the replacement must
    be boardable under them; they do fragment the cache."""
    key = (origin["id"], dest["id"], ready.strftime("%Y-%m-%dT%H:%M"), dticket, products)
    if key in _replan_cache:
        _replan_cache.move_to_end(key)
        return _replan_cache[key]
    try:
        # a stale fallback answer (age ignored) beats no replan at all
        data, _ = await bahn_api.journeys(
            f"A=1@O={origin['name']}@L={origin['id']}@",
            f"A=1@O={dest['name']}@L={dest['id']}@",
            ready.strftime("%Y-%m-%dT%H:%M:%S"),
            dticket=dticket,
            products=products,
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


async def _if_missed_connection(legs: list[dict], tt: dict, window: int,
                                dticket: str = "off",
                                products: tuple[str, ...] | None = None) -> dict | None:
    """Future mode: the next realistic connection to the journey's destination if the
    tight transfer `tt` is missed. The passenger is assumed to reach the departure
    point at planned arrival + the arriving leg's median delay; a connection counts
    as catchable when its planned departure leaves more than TRANSFER_TOLERANCE_MIN
    minutes after that.

    A "D-Ticket only" passenger cannot board an ICE (heavily discounted tickets are
    excluded from the Fahrgastrechte right to switch to long-distance trains), and a
    narrowed product set states which trains the passenger will consider at all — so
    both restrict the replan like the search itself. dticket "all" means the paid
    trains are acceptable, which is the unrestricted replan already."""
    dticket = "only" if dticket == "only" else "off"
    a, b = tt["legIndex"], tt["depLegIndex"]
    arr = _planned_dt(legs[a], "plannedArrival")
    origin = legs[b]["origin"]
    dest = [l for l in legs if not l["walking"]][-1]["destination"]
    if arr is None or not origin["id"] or not dest["id"]:
        return None
    ready = arr + timedelta(minutes=_walk_minutes(legs, a, b) + tt["medianDelay"])
    # `window` belongs in the key: it decides the span leg_delay_stats summarises
    key = (origin["id"], dest["id"], ready.strftime("%Y-%m-%dT%H:%M"), window, dticket, products)
    if key in _if_missed_cache:
        _if_missed_cache.move_to_end(key)
        _note_if_missed(True)
        # the entry is serialized straight into the response and never mutated on
        # the way out, so callers may share one object
        return _if_missed_cache[key]
    _note_if_missed(False)
    data = await _replan(origin, dest, ready, "if-missed", dticket, products)
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


async def _past_verdict(legs: list[dict], window: int, live: bool) -> dict:
    """What a journey's actual day adds to it once every leg carries its
    delayOnDate: the arrival delay at the destination (ridden through missed
    connections and their replacements by _simulate_walk), the compensation
    it amounts to, and on a live day whether some leg is still unreported.
    Shared by the past-mode search and the trips page's in-place check."""
    train_legs = [leg for leg in legs if not leg["walking"]]
    final_d = train_legs[-1].get("delayOnDate")
    sim = await _simulate_walk(legs, window, MAX_REPLANS, live)
    out = {}
    if live:
        # on a live day a missing observation means "not reported yet",
        # which is a different message than "we have no data for this train";
        # untracked products (tram, bus, ...) never report, so they must not
        # hold the journey pending forever
        out["pending"] = any(
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
        out.update({
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
        out.update({
            "arrivalDelay": arrival_delay,
            "arrivalCanceled": bool(final_d and final_d["canceled"]),
            "missedTransfers": [],
            "compensationPct": compensation_pct(arrival_delay),
        })
    return out


def _parse_travellers(raw: str | None, age: str) -> tuple[tuple[str, int, str], ...]:
    """The search mask's traveler list: "<age>:<count>:<discount>" per entry,
    comma-separated, e.g. "adult:2:bc25-2,child:1:none". Links and cached
    frontends from before the list existed carry a single `age` instead.

    Entries sharing an age and a discount are merged so that equivalent parties
    share one cache entry, the way the product filter is normalized."""
    if not raw:
        return ((age, 1, "none"),)
    merged: dict[tuple[str, str], int] = {}
    for entry in raw.split(","):
        parts = entry.split(":")
        if len(parts) != 3:
            raise HTTPException(422, "travellers entries must be <age>:<count>:<discount>")
        t_age, count, discount = parts
        if t_age not in bahn_api.TRAVELLER_TYPES:
            raise HTTPException(422, f"unknown traveller age: {t_age}")
        if discount not in bahn_api.DISCOUNTS:
            raise HTTPException(422, f"unknown discount: {discount}")
        if not count.isdigit() or not 1 <= int(count) <= bahn_api.MAX_TRAVELLERS:
            raise HTTPException(422, f"traveller count must be 1-{bahn_api.MAX_TRAVELLERS}")
        merged[(t_age, discount)] = merged.get((t_age, discount), 0) + int(count)
    if sum(merged.values()) > bahn_api.MAX_TRAVELLERS:
        raise HTTPException(422, f"at most {bahn_api.MAX_TRAVELLERS} travellers")
    return tuple((t_age, count, discount) for (t_age, discount), count in merged.items())


def _search_throttle(request: Request) -> None:
    wait = _search_limiter.retry_after(client_ip(request))
    if wait is not None:
        bahn_api.metrics["client_rate_limited"] += 1
        raise HTTPException(
            429, "too many searches; please slow down",
            headers={"Retry-After": str(wait)},
        )


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
    travellers_raw: str | None = Query(None, alias="travellers"),
    transfer: int = Query(0),
    via1: str | None = Query(None),
    via1_stay: int = Query(0, alias="via1Stay", ge=0, le=1439),
    via2: str | None = Query(None),
    via2_stay: int = Query(0, alias="via2Stay", ge=0, le=1439),
    products: str | None = None,
):
    if window not in (7, 15, 30):
        raise HTTPException(422, "window must be 7, 15 or 30")
    if transfer not in (0, 10, 15, 20, 25, 30, 35, 40, 45):
        raise HTTPException(422, "transfer must be 0 or 10-45 in steps of 5")
    if mode not in ("future", "past"):
        raise HTTPException(422, "mode must be future or past")
    if age not in bahn_api.TRAVELLER_TYPES:
        raise HTTPException(422, "age must be adult, senior, young, child or toddler")
    travellers = _parse_travellers(travellers_raw, age)
    # bahn.de's "Verkehrsmittel" filter: a comma-separated subset of the product
    # list. Normalized to canonical order so equivalent selections share a cache
    # entry; the full set (or none) means unfiltered.
    product_filter = None
    if products:
        requested = set(products.split(","))
        unknown = requested.difference(bahn_api.ALL_PRODUCTS)
        if unknown:
            raise HTTPException(422, f"unknown products: {', '.join(sorted(unknown))}")
        if len(requested) < len(bahn_api.ALL_PRODUCTS):
            product_filter = tuple(p for p in bahn_api.ALL_PRODUCTS if p in requested)
    # "1" is the legacy value from before the "all trains" mode existed; links
    # and cached frontends still send it
    dticket = {"1": "only", "only": "only", "all": "all"}.get(dticket, "off")
    # our own limit on this client, distinct from bahn.de throttling us (503)
    _search_throttle(request)
    response.headers["Cache-Control"] = "public, max-age=120"
    past = mode == "past"
    # the D-Ticket is excluded from Fahrgastrechte compensation, so the filter
    # has no place in the past-journey compensation check; a minimum transfer
    # time would hide the tight connection someone actually took
    if past:
        dticket = "off"
        # prices play no part in the compensation check, so who is travelling
        # doesn't either; pinning it keeps past searches on one cache entry
        travellers = bahn_api.DEFAULT_TRAVELLERS
        transfer = 0
    # stopovers are a planning tool; the past check inspects one journey that
    # already happened, so the frontend hides them there like the return trip
    vias = () if past else tuple(
        (via, stay) for via, stay in ((via1, via1_stay), (via2, via2_stay)) if via)
    try:
        data, stale_age = await bahn_api.journeys(
            from_id, to_id, departure, paging_ref, dticket, travellers, transfer, vias, product_filter)
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
            journey.update(await _past_verdict(legs, window, live))
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
            tt["ifMissed"] = await _if_missed_connection(j["legs"], tt, window, dticket, product_filter)

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
        "auth": auth.status(),
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


@app.get("/api/leaderboard")
async def leaderboard_api(response: Response):
    """Country punctuality ranking over the last day / 7 / 30 days of the data
    window, plus the per-day series behind it. Computed once per data version
    (app/leaderboard.py): a hit is a dict lookup, a miss a ~0.2 s scan run off
    the event loop. The data only moves with the nightly restart, so the edge
    may hold it for a while."""
    response.headers["Cache-Control"] = "public, max-age=600"
    return await asyncio.to_thread(leaderboard.get)


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
LEADERBOARD_PATHS = {"de": "/rangliste", "en": "/leaderboard"}
STORIES_LOGO = {"de": "/logo_delay_stories_square_german.png",
                "en": "/logo_delay_stories_square.png"}
STORIES_WORDMARK = {"de": "/logo_delay_stories_wide_german_transparent.png",
                    "en": "/logo_delay_stories_wide_transparent.png"}
STORIES_ALT = {"de": "Delay Geschichten", "en": "Delay Stories"}
# the account's booked trips; private, so no meta beyond the title
TRIPS_PATHS = {"de": "/meine-fahrten", "en": "/en/my-trips"}
TRIPS_TITLE = {"de": "Meine Fahrten – DelayBahn", "en": "My Trips – DelayBahn"}

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


@lru_cache(maxsize=8)
def _en_strings(script: str = "app.js", lang: str = "en") -> dict[str, str]:
    """The plain English strings from I18N.en in a static script (app.js, or
    stories.js for the stories page) - or another language's block, for the
    server-rendered embed card.

    The script translates the page on load, but then the markup a crawler fetches
    is German on an English URL until it renders the JS. Reusing the same table
    server-side means the English URL ships English text without a second copy
    of it. Anything not parsed here just stays German until the script runs.
    """
    src = (STATIC_DIR / script).read_text(encoding="utf-8")
    try:
        start = src.index(f"\n  {lang}: {{", src.index("const I18N = {"))
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
        (r'(<a href=")[^"]*(" data-i18n="footerLeaderboard")', rf"\g<1>{LEADERBOARD_PATHS[lang]}\g<2>"),
        (r'(<a id="stories-cta" class="stories-cta" href=")[^"]*', rf"\g<1>{STORIES_PATHS[lang]}"),
        (r'(<img id="stories-cta-logo" src=")[^"]*(" alt=")[^"]*',
         rf"\g<1>{STORIES_LOGO[lang]}\g<2>{STORIES_ALT[lang]}"),
        (r'(<a class="stories-banner-link" href=")[^"]*',
         rf"\g<1>{STORIES_PATHS[lang]}"),
        (r'(<img id="stories-banner-logo" class="stories-banner-logo" src=")[^"]*(" alt=")[^"]*',
         rf"\g<1>{STORIES_WORDMARK[lang]}\g<2>{STORIES_ALT[lang]}"),
        # the account corner and the ordered-report receipt both point at the trips page
        (r'(<a id="auth-name" class="auth-name" href=")[^"]*', rf"\g<1>{TRIPS_PATHS[lang]}"),
        (r'(<a id="report-trip-saved" class="trip-saved hidden" href=")[^"]*',
         rf"\g<1>{TRIPS_PATHS[lang]}"),
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


def _excerpt(text: str, limit: int = 160) -> str:
    """The opening of a story for its description meta: whitespace collapsed,
    cut at a word boundary."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + "…"


def _stories_html(lang: str, story: dict | None = None) -> str:
    """Render one language of the stories page from stories.html: same page at
    /geschichten and /stories, only the instruction language differs. With a
    story it is that story's permalink: the same page, the story pinned on top
    by the script, and its title and text in the meta so a shared link unfurls
    as the story rather than as the board."""
    html = (STATIC_DIR / "stories.html").read_text(encoding="utf-8")
    other = "de" if lang == "en" else "en"
    paths = {l: STORIES_PATHS[l] + (f"/{story['id']}" if story else "") for l in ("de", "en")}
    if story:
        # user text: escaped for the markup, and backslashes doubled so re.sub
        # reads them as characters rather than as group references
        title, description = (
            escape(s, quote=True).replace("\\", "\\\\")
            for s in (f"{story['title']} – {STORIES_ALT[lang]}", _excerpt(story["text"]))
        )
        og_description = description
    else:
        title, description, og_description = STORIES_META[lang]
    url = SITE + paths[lang]
    logo = "/logo_delay_stories_tall_transparent.png" if lang == "en" else (
        "/logo_delay_stories_tall_german_transparent.png")
    subs = [
        (r'<html lang="[^"]*"', f'<html lang="{lang}"'),
        (r"<title>[^<]*</title>", f"<title>{title}</title>"),
        (r'(<meta name="description" content=")[^"]*', rf"\g<1>{description}"),
        (r'(<link rel="canonical" href=")[^"]*', rf"\g<1>{url}"),
        (r'(<link rel="alternate" hreflang="de" href=")[^"]*', rf"\g<1>{SITE}{paths['de']}"),
        (r'(<link rel="alternate" hreflang="en" href=")[^"]*', rf"\g<1>{SITE}{paths['en']}"),
        (r'(<link rel="alternate" hreflang="x-default" href=")[^"]*', rf"\g<1>{SITE}{paths['de']}"),
        (r'(<meta property="og:url" content=")[^"]*', rf"\g<1>{url}"),
        (r'(<meta property="og:title" content=")[^"]*', rf"\g<1>{title}"),
        (r'(<meta property="og:description" content=")[^"]*', rf"\g<1>{og_description}"),
        (r'(<meta property="og:image" content=")[^"]*', rf"\g<1>{SITE}{logo}"),
        (r'(<meta property="og:locale" content=")[^"]*', rf"\g<1>{OG_LOCALE[lang]}"),
        (r'(<meta property="og:locale:alternate" content=")[^"]*', rf"\g<1>{OG_LOCALE[other]}"),
        (r'(<a href=")[^"]*(" hreflang="de")', rf"\g<1>{paths['de']}\g<2>"),
        (r'(<a href=")[^"]*(" hreflang="en")', rf"\g<1>{paths['en']}\g<2>"),
        # in-page navigation stays inside the current language
        (r'(<a class="logo-link" href=")[^"]*', rf"\g<1>{PAGE_PATHS[('future', lang)]}"),
        (r'(<a class="stories-mark" href=")[^"]*', rf"\g<1>{STORIES_PATHS[lang]}"),
        (r'(<img id="site-logo" src=")[^"]*(" alt=")[^"]*',
         rf"\g<1>{STORIES_WORDMARK[lang]}\g<2>{STORIES_ALT[lang]}"),
        (r'(<a href=")[^"]*(" data-i18n="footerBack")', rf"\g<1>{PAGE_PATHS[('future', lang)]}\g<2>"),
        (r'(<a id="auth-name" class="auth-name" href=")[^"]*', rf"\g<1>{TRIPS_PATHS[lang]}"),
    ]
    if story:
        subs.append((r'(<meta property="og:type" content=")[^"]*', r"\g<1>article"))
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


async def _story_page(story_id: int, lang: str) -> HTMLResponse:
    story = await anyio.to_thread.run_sync(stories.get_story, story_id)
    # a dead link still gets the board, with "that story is gone" where the
    # story would sit (the script asks the API and hears the 404 itself) -
    # a JSON error is no page to land on. A tombstone keeps its thread, so it
    # renders as a normal permalink; only the meta stays generic.
    live = story if story and not story["deleted"] else None
    return HTMLResponse(
        _stories_html(lang, live),
        status_code=200 if story else 404,
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/geschichten/{story_id:int}")
async def story_page_de(story_id: int) -> HTMLResponse:
    return await _story_page(story_id, "de")


@app.get("/stories/{story_id:int}")
async def story_page_en(story_id: int) -> HTMLResponse:
    return await _story_page(story_id, "en")


def _embed_html(story: dict, lang: str) -> str:
    """A story as a self-contained card for other sites' iframes: no page
    chrome, its own few lines of CSS, every link opening in the parent. The
    labels come from the same I18N table the board uses."""
    strings = _en_strings("stories.js", lang)
    leg = story["from_station"]
    if story["to_station"]:
        leg += f" → {story['to_station']}"
    posted = datetime.fromisoformat(story["ts"])
    labels = [
        story["problem_other"] if code == "other" and story["problem_other"]
        else strings.get("problem_" + code, code)
        for code in story["problems"]
    ]
    tags = "".join(f'<span class="tag">{escape(label)}</span>' for label in labels)
    n = story["comments"]
    comments = (strings.get("comments1", "1") if n == 1
                else strings.get("commentsN", "{n}").replace("{n}", str(n)))
    values = {
        "lang": lang,
        "title": escape(story["title"]),
        "url": SITE + STORIES_PATHS[lang] + f"/{story['id']}",
        "board_url": SITE + STORIES_PATHS[lang],
        "board_name": STORIES_ALT[lang],
        "logo": STORIES_LOGO[lang],
        "leg": escape(leg),
        "author": escape(story["author"] or strings.get("anon", "")),
        "date": posted.strftime("%d.%m.%Y" if lang == "de" else "%b %d, %Y"),
        "tags": f'<div class="tags">{tags}</div>' if tags else "",
        "text": escape(story["text"]),
        "score": str(story["score"]),
        "comments": escape(comments),
        "read_more": escape(strings.get("embedRead", "")),
    }
    html = (STATIC_DIR / "embed.html").read_text(encoding="utf-8")
    for key, value in values.items():
        html = html.replace("{{" + key + "}}", value)
    return html


async def _story_embed(story_id: int, lang: str) -> HTMLResponse:
    story = await anyio.to_thread.run_sync(stories.get_story, story_id)
    if story is None or story["deleted"]:
        raise HTTPException(404, "story not found")
    return HTMLResponse(_embed_html(story, lang), headers={"Cache-Control": "no-cache"})


@app.get("/embed/geschichten/{story_id:int}")
async def story_embed_de(story_id: int) -> HTMLResponse:
    return await _story_embed(story_id, "de")


@app.get("/embed/stories/{story_id:int}")
async def story_embed_en(story_id: int) -> HTMLResponse:
    return await _story_embed(story_id, "en")


# (title, description) per language; og:description reuses the description
LEADERBOARD_META = {
    "de": (
        "Europas Bahn-Rangliste – welches Land fährt am pünktlichsten? | DelayBahn",
        "Deutschland, Österreich, Schweiz, Frankreich, Niederlande und Italien im "
        "Pünktlichkeits-Vergleich – täglich, wöchentlich, monatlich, mit Verspätungs-"
        "Karte. Jeden Morgen automatisch neu berechnet.",
    ),
    "en": (
        "Europe's rail leaderboard – which country runs the most punctual trains? | DelayBahn",
        "Germany, Austria, Switzerland, France, the Netherlands and Italy compared on "
        "punctuality – daily, weekly, monthly, with a delay map. Recomputed "
        "automatically every morning.",
    ),
}


def _leaderboard_html(lang: str) -> str:
    """Render one language of the country leaderboard from leaderboard.html:
    the same page at /rangliste and /leaderboard, only the text language differs."""
    html = (STATIC_DIR / "leaderboard.html").read_text(encoding="utf-8")
    other = "de" if lang == "en" else "en"
    title, description = LEADERBOARD_META[lang]
    url = SITE + LEADERBOARD_PATHS[lang]
    home = PAGE_PATHS[("future", lang)]
    subs = [
        (r'<html lang="[^"]*"', f'<html lang="{lang}"'),
        (r"<title>[^<]*</title>", f"<title>{title}</title>"),
        (r'(<meta name="description" content=")[^"]*', rf"\g<1>{description}"),
        (r'(<link rel="canonical" href=")[^"]*', rf"\g<1>{url}"),
        (r'(<link rel="alternate" hreflang="de" href=")[^"]*', rf"\g<1>{SITE}{LEADERBOARD_PATHS['de']}"),
        (r'(<link rel="alternate" hreflang="en" href=")[^"]*', rf"\g<1>{SITE}{LEADERBOARD_PATHS['en']}"),
        (r'(<link rel="alternate" hreflang="x-default" href=")[^"]*', rf"\g<1>{SITE}{LEADERBOARD_PATHS['de']}"),
        (r'(<meta property="og:url" content=")[^"]*', rf"\g<1>{url}"),
        (r'(<meta property="og:title" content=")[^"]*', rf"\g<1>{title}"),
        (r'(<meta property="og:description" content=")[^"]*', rf"\g<1>{description}"),
        (r'(<meta property="og:locale" content=")[^"]*', rf"\g<1>{OG_LOCALE[lang]}"),
        (r'(<meta property="og:locale:alternate" content=")[^"]*', rf"\g<1>{OG_LOCALE[other]}"),
        (r'(<a href=")[^"]*(" hreflang="de")', rf"\g<1>{LEADERBOARD_PATHS['de']}\g<2>"),
        (r'(<a href=")[^"]*(" hreflang="en")', rf"\g<1>{LEADERBOARD_PATHS['en']}\g<2>"),
        # in-page navigation stays inside the current language
        (r'(<a class="logo-link" href=")[^"]*', rf"\g<1>{home}"),
        (r'(<a href=")[^"]*(" data-i18n="footerBack")', rf"\g<1>{home}\g<2>"),
        (r'(<a href=")[^"]*(" data-i18n="footerStories")', rf"\g<1>{STORIES_PATHS[lang]}\g<2>"),
    ]
    for pattern, repl in subs:
        html = re.sub(pattern, repl, html, count=1)
    if lang == "en":
        html = html.replace('data-lang="en" class="lang-btn"', 'data-lang="en" class="lang-btn active"')
        html = html.replace('data-lang="de" class="lang-btn active"', 'data-lang="de" class="lang-btn"')
        html = _translate(html, "leaderboard.js")
    return html


@app.get("/rangliste")
async def leaderboard_page_de() -> HTMLResponse:
    # no-cache like the other HTML documents (no ?v= buster on the document)
    return HTMLResponse(_leaderboard_html("de"), headers={"Cache-Control": "no-cache"})


@app.get("/leaderboard")
async def leaderboard_page_en() -> HTMLResponse:
    return HTMLResponse(_leaderboard_html("en"), headers={"Cache-Control": "no-cache"})


@app.get("/rangliste/")
@app.get("/rangliste.html")
async def leaderboard_alias_de() -> RedirectResponse:
    return RedirectResponse("/rangliste", status_code=301)


@app.get("/leaderboard/")
@app.get("/leaderboard.html")
async def leaderboard_alias_en() -> RedirectResponse:
    return RedirectResponse("/leaderboard", status_code=301)


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "login.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/login/")
@app.get("/login.html")
async def login_alias() -> RedirectResponse:
    return RedirectResponse("/login", status_code=301)


def _trips_html(lang: str) -> str:
    """Render one language of the trips page from trips.html: the same page
    at /meine-fahrten and /en/my-trips. The list itself is fetched by the
    script with the account's token, so nothing personal is in the markup."""
    html = (STATIC_DIR / "trips.html").read_text(encoding="utf-8")
    home = PAGE_PATHS[("future", lang)]
    subs = [
        (r'<html lang="[^"]*"', f'<html lang="{lang}"'),
        (r"<title>[^<]*</title>", f"<title>{TRIPS_TITLE[lang]}</title>"),
        (r'(<a href=")[^"]*(" hreflang="de")', rf"\g<1>{TRIPS_PATHS['de']}\g<2>"),
        (r'(<a href=")[^"]*(" hreflang="en")', rf"\g<1>{TRIPS_PATHS['en']}\g<2>"),
        # in-page navigation stays inside the current language
        (r'(<a class="logo-link" href=")[^"]*', rf"\g<1>{home}"),
        (r'(<a id="auth-name" class="auth-name" href=")[^"]*', rf"\g<1>{TRIPS_PATHS[lang]}"),
        (r'(<a id="trips-search-link" class="trips-search-link" href=")[^"]*',
         rf"\g<1>{home}"),
        (r'(<a id="trips-onboard-btn" class="report-submit trips-login-btn" href=")[^"]*',
         rf"\g<1>{home}"),
        (r'(<a href=")[^"]*(" data-i18n="footerBack")', rf"\g<1>{home}\g<2>"),
        (r'(<a href=")[^"]*(" data-i18n="footerStories")', rf"\g<1>{STORIES_PATHS[lang]}\g<2>"),
    ]
    for pattern, repl in subs:
        html = re.sub(pattern, repl, html, count=1)
    if lang == "en":
        html = html.replace('data-lang="en" class="lang-btn"', 'data-lang="en" class="lang-btn active"')
        html = html.replace('data-lang="de" class="lang-btn active"', 'data-lang="de" class="lang-btn"')
        html = _translate(html, "trips.js")
    return html


@app.get("/meine-fahrten")
async def trips_page_de() -> HTMLResponse:
    return HTMLResponse(_trips_html("de"), headers={"Cache-Control": "no-cache"})


@app.get("/en/my-trips")
async def trips_page_en() -> HTMLResponse:
    return HTMLResponse(_trips_html("en"), headers={"Cache-Control": "no-cache"})


@app.get("/meine-fahrten/")
@app.get("/meine-fahrten.html")
async def trips_alias_de() -> RedirectResponse:
    return RedirectResponse(TRIPS_PATHS["de"], status_code=301)


@app.get("/en/my-trips/")
async def trips_alias_en() -> RedirectResponse:
    return RedirectResponse(TRIPS_PATHS["en"], status_code=301)


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
    # Reddit-style: 1 up, -1 down, 0 clears. Booleans from a pre-downvote
    # cached script coerce to 1/0, which is exactly what they used to mean.
    vote: Literal[-1, 0, 1, True, False] = 1


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


class HandleIn(BaseModel):
    # HN-style handles: short, ASCII, no spaces - what makes a name recognizable
    # across posts. The stricter charset also keeps names trivially safe to echo.
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{2,25}$")


# Addresses arrive pasted, so surrounding whitespace is trimmed before the
# shape check rather than rejected by it. The check is only a shape check -
# the emailed code is what actually proves the address exists.
_EmailField = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, max_length=254,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]


class EmailCodeIn(BaseModel):
    email: _EmailField
    lang: Literal["de", "en"] = "de"


class VerifyCodeIn(BaseModel):
    email: _EmailField
    code: str = Field(pattern=r"^[0-9]{6}$")


# Identity is a Firebase ID token in the Authorization header, never a cookie:
# nothing about a login is kept on this server, and a bearer is not sent by a
# cross-site form, which is the whole CSRF story.
def _bearer(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def _auth_down(exc: Exception) -> HTTPException:
    log.warning("firebase unavailable: %s", exc)
    return HTTPException(503, "accounts are temporarily unavailable")


async def _optional_user(request: Request) -> dict | None:
    """The account behind the request's bearer, or None without one. A token
    that is present but bad is a 401 rather than "anonymous": the SDK
    refreshes tokens on its own, so a bad one is a stale page or a forgery,
    and both should hear about it rather than quietly lose their votes."""
    token = _bearer(request)
    if token is None:
        return None
    try:
        user = await anyio.to_thread.run_sync(auth.account, token)
    except auth.AuthUnavailable as exc:
        raise _auth_down(exc)
    if user is None:
        raise HTTPException(401, "invalid or expired login token")
    return user


async def _require_user(request: Request) -> dict:
    """An account that may write: signed in, contact proven, name claimed.
    The two 403s name the missing step so the page can send the visitor
    there rather than to a generic error."""
    user = await _optional_user(request)
    if user is None:
        raise HTTPException(401, "login required")
    if not user["verified"]:
        raise HTTPException(403, "unverified")
    if not user["name"]:
        raise HTTPException(403, "unnamed")
    return user


def _stories_throttle(limiter: ratelimit.SlidingWindowLimiter, request: Request) -> None:
    wait = limiter.retry_after(client_ip(request))
    if wait is not None:
        raise HTTPException(
            429, "too many submissions; please slow down",
            headers={"Retry-After": str(wait)},
        )


@app.post("/api/auth/email-code", status_code=202)
async def auth_email_code(body: EmailCodeIn, request: Request) -> dict:
    """Step one of the email path: mail a six-digit code. Always 202, whether
    or not a mail actually went out - an address's spent cooldown is not
    something to report, and the code already in the mailbox still works.
    Whether the address has an account is likewise never said here: both
    cases answer identically, and the wording of the mail is the only place
    the difference shows."""
    _stories_throttle(auth.email_limiter, request)
    try:
        issued = await anyio.to_thread.run_sync(auth.issue_email_code, body.email)
    except auth.AuthUnavailable as exc:
        raise _auth_down(exc)
    # a spent per-address budget yields no code: nothing is wrong, there is
    # simply no new mail, and the answer stays the same 202 either way
    if issued is not None:
        code, kind = issued
        # on the request's clock - a second or so of SMTP - so a relay refusal
        # (out of Brevo credits, most likely) reaches the user as a 503
        # instead of a "check your inbox" for a mail that will not come
        sent = await anyio.to_thread.run_sync(
            mailer.send_login_code, auth.normalize_email(body.email),
            code, body.lang, kind,
        )
        if not sent:
            # hand back the cooldown and the daily slot the failed send spent,
            # so the retry we just asked for is not swallowed by it
            await anyio.to_thread.run_sync(auth.refund_code, body.email)
            raise HTTPException(503, "email could not be sent; please try again later")
    return {"resend_after": auth.RESEND_COOLDOWN_SECONDS}


@app.post("/api/auth/email-code/verify")
async def auth_email_code_verify(body: VerifyCodeIn, request: Request):
    """Step two: the code buys a Firebase custom token, which the browser
    signs in with - so from here on this is an ordinary Firebase session and
    nothing about the login stays on this server. One 401 for every failure:
    which of wrong/expired/used-up applies is not something to spell out."""
    _stories_throttle(auth.code_limiter, request)
    try:
        token = await anyio.to_thread.run_sync(
            auth.verify_email_code, body.email, body.code
        )
    except auth.AuthUnavailable as exc:
        raise _auth_down(exc)
    if token is None:
        raise HTTPException(401, "code invalid or expired")
    return {"token": token}


@app.post("/api/auth/handle", status_code=201)
async def auth_handle(body: HandleIn, request: Request) -> dict:
    """The one write a fresh account makes here: its public name, once. It
    needs a proven contact first (see auth.account), so a squatted name
    always has a reachable person behind it. The 409 says which of the two
    conflicts it is: "taken" wants another name, "named" means this account
    already has one and the page merely holds a token from before it did."""
    user = await _optional_user(request)
    if user is None:
        raise HTTPException(401, "login required")
    if not user["verified"]:
        raise HTTPException(403, "unverified")
    if user["name"]:
        raise HTTPException(409, "named")
    _stories_throttle(auth.register_limiter, request)
    try:
        result = await anyio.to_thread.run_sync(
            auth.claim_handle, user["uid"], body.name
        )
    except auth.AuthUnavailable as exc:
        raise _auth_down(exc)
    if result != "ok":
        raise HTTPException(409, result)
    return {"name": body.name}


@app.get("/api/auth/suggest-name")
async def auth_suggest_name(
    request: Request, response: Response, lang: Literal["de", "en"] = "de"
):
    """A free username to offer someone who has not thought of one. The client
    cannot name what it wants checked, which is the point: an availability
    endpoint taking a name would be a handle-enumeration oracle, and this
    answers the same question without being one."""
    _stories_throttle(auth.suggest_limiter, request)
    try:
        name = await anyio.to_thread.run_sync(auth.suggest_name, lang)
    except auth.AuthUnavailable as exc:
        raise _auth_down(exc)
    if name is None:
        raise HTTPException(503, "no free name found")
    # every caller must get its own name; an edge cache serving one twice
    # would hand two people the same suggestion
    response.headers["Cache-Control"] = "no-store"
    return {"name": name}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """What the server reads from the token. The page learns the same from
    the SDK's own claims without a round trip; this is the wiring check."""
    user = await _optional_user(request)
    if user is None:
        return {"name": None}
    return {"name": user["name"], "uid": user["uid"], "verified": user["verified"]}


@app.get("/api/stories")
async def stories_index(
    request: Request,
    sort: Literal["new", "top", "liked", "commented"] = "new",
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    user = await _optional_user(request)
    return await anyio.to_thread.run_sync(
        stories.list_stories, sort, limit, offset, user["uid"] if user else None
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


def _board(span: str, uid: str | None) -> dict:
    """Counts over the span plus the codes the viewer tapped today - the
    tiles render both from one answer, and a tap is answered with the same
    shape so it needs no second round trip."""
    return {
        "counts": stories.count_problems(span),
        "mine": stories.my_reports(uid) if uid is not None else [],
    }


# public and anonymous, like reading the stories themselves; a login only
# adds which tiles are the viewer's own
@app.get("/api/stories/problems")
async def stories_problems(request: Request, span: BoardSpan = "month"):
    user = await _optional_user(request)
    return await anyio.to_thread.run_sync(_board, span, user["uid"] if user else None)


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
        stories.set_report, user["uid"], code, report.vote,
        report.from_station, report.to_station, report.departure, report.train,
        report.problem_other,
    )
    if result is None:
        raise HTTPException(404, "unknown problem")
    return await anyio.to_thread.run_sync(_board, span, user["uid"])


@app.post("/api/stories/{story_id}/vote")
async def stories_vote(story_id: int, vote: StoryVoteIn, request: Request):
    user = await _require_user(request)
    _stories_throttle(stories.vote_limiter, request)
    result = await anyio.to_thread.run_sync(
        stories.set_vote, story_id, user["uid"], int(vote.vote)
    )
    if result is None:
        raise HTTPException(404, "story not found")
    return result


@app.get("/api/stories/{story_id}/comments")
async def stories_comments(story_id: int, request: Request):
    user = await _optional_user(request)
    result = await anyio.to_thread.run_sync(
        stories.list_comments, story_id, user["uid"] if user else None
    )
    if result is None:
        raise HTTPException(404, "story not found")
    return result


@app.get("/api/stories/{story_id}")
async def stories_show(story_id: int, request: Request):
    # the fixed /api/stories/problems path is registered earlier and keeps winning
    user = await _optional_user(request)
    story = await anyio.to_thread.run_sync(
        stories.get_story, story_id, user["uid"] if user else None
    )
    if story is None:
        raise HTTPException(404, "story not found")
    return story


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
        stories.set_comment_vote, comment_id, user["uid"], int(vote.vote)
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


# --- journey reports: the bell orders a post-journey forecast-vs-actual email ---
# The order is tied to the Delay Stories account (app/auth.py): the account
# already proved its address, so the bell needs no email form and no double
# opt-in of its own - one signed-in press is the order, a second one withdraws
# it. The unsubscribe link in the mail itself keeps working without a login.


class ReportOrder(BaseModel):
    lang: Literal["de", "en"] = "de"
    journey: dict
    search: dict = Field(default_factory=dict)


async def _report_user(request: Request) -> dict:
    """An account a report can be mailed to: signed in, address proven. No
    username is needed - the report goes to the inbox, not the board - so
    this is deliberately looser than _require_user."""
    user = await _optional_user(request)
    if user is None:
        raise HTTPException(401, "login required")
    if not user["verified"] or not user["email"]:
        raise HTTPException(403, "unverified")
    return user


@app.post("/api/reports/subscribe")
async def report_subscribe(order: ReportOrder, request: Request) -> dict:
    user = await _report_user(request)
    _stories_throttle(reports.subscribe_limiter, request)
    try:
        return await anyio.to_thread.run_sync(
            reports.subscribe, user, order.lang, order.journey, order.search
        )
    except reports.TooManyOpenReports as exc:
        # its own status so the page can name the limit instead of refusing the
        # journey; the count travels with it, the copy lives in the page
        raise HTTPException(409, {"error": "too_many_open_reports", "limit": exc.limit})
    except reports.SnapshotError as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/reports/mine")
async def report_mine(request: Request) -> dict:
    """The account's open orders, so the page can light the bells of the
    journeys it already ordered a report for."""
    user = await _report_user(request)
    subs = await anyio.to_thread.run_sync(reports.mine, user["uid"])
    return {"email": user["email"], "subscriptions": subs}


@app.delete("/api/reports/{sub_id}", status_code=204)
async def report_cancel(sub_id: int, request: Request) -> Response:
    user = await _report_user(request)
    if not await anyio.to_thread.run_sync(reports.cancel, user["uid"], sub_id):
        raise HTTPException(404, "no such open report")
    return Response(status_code=204)


_R_STRINGS = {
    "de": {
        "unsubTitle": "Abmelden & Daten löschen",
        "unsubLead": "Damit werden alle offenen Verspätungs-Reports dieses Kontos storniert und"
        " E-Mail-Adresse, Benutzername und Konto-Kennung aus den Report-Einträgen gelöscht.",
        "unsubBtn": "Jetzt abmelden & Daten löschen",
        "unsubbedTitle": "Abgemeldet",
        "unsubbedLead": "Alle offenen Reports wurden storniert und deine personenbezogenen"
        " Daten aus den Report-Einträgen gelöscht.",
        "deadTitle": "Link ungültig",
        "deadLead": "Dieser Link ist nicht (mehr) gültig.",
        "back": "← Zur Verbindungssuche",
    },
    "en": {
        "unsubTitle": "Unsubscribe & delete data",
        "unsubLead": "This cancels every open delay report of this account and removes the"
        " email address, username and account id from the report entries.",
        "unsubBtn": "Unsubscribe & delete my data now",
        "unsubbedTitle": "Unsubscribed",
        "unsubbedLead": "All open reports were cancelled and your personal data was removed"
        " from the report entries.",
        "deadTitle": "Invalid link",
        "deadLead": "This link is not (or no longer) valid.",
        "back": "← Back to connection search",
    },
}


def _r_page(lang: str, title: str, lead: str, form_html: str = "", status: int = 200) -> HTMLResponse:
    s = _R_STRINGS[lang]
    return HTMLResponse(
        "<!DOCTYPE html>"
        f'<html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(title)} – DelayBahn</title>"
        '<link rel="icon" type="image/png" href="/favicon.png">'
        '<link rel="stylesheet" href="/style.css">'
        '</head><body><header><div class="header-inner">'
        '<a class="logo-link" href="/"><img class="logo" src="/logo.png" alt="DelayBahn"></a>'
        f'<span class="header-title">{escape(title)}</span></div></header>'
        f'<main><div class="legal-card"><p>{escape(lead)}</p>{form_html}'
        f'<p style="margin-top:18px;"><a href="/">{escape(s["back"])}</a></p>'
        "</div></main></body></html>",
        status_code=status,
        headers={"Cache-Control": "no-cache"},
    )


def _r_dead_page() -> HTMLResponse:
    s = _R_STRINGS["de"]
    return _r_page("de", s["deadTitle"], s["deadLead"], status=404)


@app.get("/r/unsubscribe")
async def report_unsubscribe_page(token: str = Query("")) -> HTMLResponse:
    lang = await anyio.to_thread.run_sync(reports.token_lang, token)
    if lang is None:
        return _r_dead_page()
    s = _R_STRINGS[lang]
    # a deliberate click, never auto-submitted: this deletes data
    form = (
        f'<form method="post" action="/r/unsubscribe?token={escape(token)}">'
        f'<button class="search-btn" type="submit">{escape(s["unsubBtn"])}</button></form>'
    )
    return _r_page(lang, s["unsubTitle"], s["unsubLead"], form)


@app.post("/r/unsubscribe")
async def report_unsubscribe_submit(token: str = Query("")) -> HTMLResponse:
    """Form target and RFC 8058 one-click target (List-Unsubscribe-Post) in one;
    the one-click POST body is ignored."""
    lang = (await anyio.to_thread.run_sync(reports.token_lang, token)) or "de"
    ok = await anyio.to_thread.run_sync(reports.unsubscribe, token)
    if not ok:
        return _r_dead_page()
    s = _R_STRINGS[lang]
    return _r_page(lang, s["unsubbedTitle"], s["unsubbedLead"])


# --- booked trips: the "Meine Fahrten" page ------------------------------------
# The bookmark beside a booking button files the journey, and so does a
# signed-in press on the booking button itself; the page lists them and lets
# the account drop what it did not book.


class TripPress(BaseModel):
    lang: Literal["de", "en"] = "de"
    via: Literal["card", "summary", "report-modal", "add"] = "card"
    url: str
    journeys: list[dict] = Field(min_length=1, max_length=trips.MAX_JOURNEYS_PER_PRESS)
    search: dict = Field(default_factory=dict)


async def _trips_user(request: Request) -> dict:
    """An account with a proven contact; no username or email address is
    needed to keep a list of one's own trips."""
    user = await _optional_user(request)
    if user is None:
        raise HTTPException(401, "login required")
    if not user["verified"]:
        raise HTTPException(403, "unverified")
    return user


@app.post("/api/trips")
async def trips_record(press: TripPress, request: Request) -> dict:
    user = await _trips_user(request)
    _stories_throttle(trips.record_limiter, request)
    try:
        saved = await anyio.to_thread.run_sync(
            trips.record, user["uid"], press.lang, press.via, press.url,
            press.journeys, press.search,
        )
    except trips.TripError as exc:
        raise HTTPException(422, str(exc))
    return {"trips": saved}


# how many past trips one list load may still run the check for; the rest
# follow on later loads or when their button is pressed
TRIP_VERDICTS_PER_LOAD = 5


@app.get("/api/trips")
async def trips_mine(request: Request) -> dict:
    """Every trip of the account plus the Berlin clock the page splits them
    on, so a visitor abroad still sees today's trip under "next". Past trips
    whose check has not run yet get it here, a few per load, so the tally
    counts what a cancellation really cost rather than the final leg alone."""
    user = await _trips_user(request)
    mine = await anyio.to_thread.run_sync(trips.mine, user["uid"])
    now = trips.berlin_now()
    todo = [t for t in mine if t["arrival"] <= now and not t["resolved"]]
    for t in todo[:TRIP_VERDICTS_PER_LOAD]:
        try:
            verdict = await _trip_verdict(user["uid"], t["id"])
        except bahn_api.UpstreamError:
            break  # bahn.de is the missing piece: leave the rest for a later load
        if verdict is None:
            continue
        t["delay"], t["canceled"] = trips.verdict_outcome(verdict)
        t["resolved"] = verdict["final"]
    return {"now": now, "trips": mine}


@app.delete("/api/trips/{trip_id}", status_code=204)
async def trips_remove(trip_id: int, request: Request) -> Response:
    user = await _trips_user(request)
    if not await anyio.to_thread.run_sync(trips.remove, user["uid"], trip_id):
        raise HTTPException(404, "no such trip")
    return Response(status_code=204)


def _tracked_train(leg: dict) -> bool:
    line = leg["line"]
    return (not leg["walking"] and line["product"] not in UNTRACKED_PRODUCTS
            and bool(line["fahrtNr"]) and bool(leg["destination"]["id"]))


def _leg_stops(legs: list[dict]) -> set[tuple[str, datetime]]:
    """Every (station, planned time) a stored itinerary touches - _live_stops
    over normalized legs rather than bahn.de's raw sections."""
    stops = set()
    for leg in legs:
        if not _tracked_train(leg):
            continue
        for stop, key in ((leg["origin"], "plannedDeparture"), (leg["destination"], "plannedArrival")):
            if stop["id"] and leg.get(key):
                stops.add((delays.pad_eva(str(stop["id"])), delays.to_berlin_naive(leg[key])))
    return stops


def _attach_day_delays(legs: list[dict], live: bool) -> None:
    """Give stored legs the day's actual arrival delay, exactly as normalize_leg
    does for a past search: IRIS on a day the nightly parquet has not reached,
    the parquet everywhere else. Blocking - run it off the event loop."""
    for leg in legs:
        if not _tracked_train(leg) or not leg.get("plannedArrival"):
            continue
        train = str(leg["line"]["fahrtNr"]).replace(" ", "")
        eva = delays.pad_eva(str(leg["destination"]["id"]))
        arrival = delays.to_berlin_naive(leg["plannedArrival"])
        hit = live_delays.leg_delay_on_date(train, eva, arrival) if live else None
        leg["delayOnDate"] = hit if hit is not None else delays.leg_delay_on_date(train, eva, arrival)


async def _trip_verdict(uid: str, trip_id: int) -> dict | None:
    """The compensation page's verdict for one filed trip: each leg's delay
    on the day, missed connections and the onward journey the simulation
    rides instead, and what that adds up to - the same lookups and the same
    simulation as a past-mode search, on the itinerary on file. `final` says
    the day is in the nightly data, so the answer cannot change: only then is
    it stored with the trip; a live day, or one nobody has data for yet, is
    answered fresh each time. None when the trip is not this account's, not
    over yet, or has no train leg. Raises bahn_api.UpstreamError."""
    stored = await anyio.to_thread.run_sync(trips.stored_verdict, uid, trip_id)
    if stored is not None:
        return stored
    filed = await anyio.to_thread.run_sync(trips.journey_legs, uid, trip_id)
    if filed is None or filed["arrival"] > trips.berlin_now():
        return None
    legs = filed["legs"]
    if not any(not leg["walking"] for leg in legs):
        return None
    # days the nightly parquet hasn't reached yet are answered live from IRIS
    parquet_max = delays.coverage()[1]
    covered = parquet_max is not None and filed["departure"][:10] <= parquet_max.isoformat()
    live = not covered and live_max_day() is not None
    if live:
        await live_delays.warm(_leg_stops(legs))
    await anyio.to_thread.run_sync(_attach_day_delays, legs, live)
    verdict = {"legs": legs, "liveDay": live, "final": covered, **await _past_verdict(legs, 7, live)}
    if covered:
        await anyio.to_thread.run_sync(trips.store_verdict, uid, trip_id, verdict)
    return verdict


@app.get("/api/trips/{trip_id}/check")
async def trips_check(trip_id: int, request: Request) -> dict:
    """The verdict for one trip, shown in place on the trips page. A stored
    one is free; running it costs the search budget, since a missed
    connection can send the simulation to bahn.de for a replacement."""
    user = await _trips_user(request)
    stored = await anyio.to_thread.run_sync(trips.stored_verdict, user["uid"], trip_id)
    if stored is not None:
        return stored
    _search_throttle(request)
    filed = await anyio.to_thread.run_sync(trips.journey_legs, user["uid"], trip_id)
    if filed is None:
        raise HTTPException(404, "no such trip")
    if filed["arrival"] > trips.berlin_now():
        raise HTTPException(409, "journey not over yet")
    try:
        verdict = await _trip_verdict(user["uid"], trip_id)
    except bahn_api.UpstreamError as e:
        raise _upstream_http_error(e)
    if verdict is None:
        raise HTTPException(422, "no train legs")
    return verdict


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
