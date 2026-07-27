"""ASGI entry point that serves the real app with every external dependency stubbed.

Load testing the real app would send thousands of requests to bahn.de (an unofficial
API behind Akamai that already blocks fingerprints) and to the DB Timetables API, and
would write a row into Umami's postgres for every simulated session. So this module
imports app.main and rebinds the three network boundaries to replay recorded fixtures.

The stubs sit *below* the cache layer, so the bahn.de LRU, its 120s TTL, the
single-flight asyncio.Task sharing and the IRIS semaphore all still run for real --
only the socket is replaced. DuckDB is untouched and answers at full cost, which is
the thing being measured.

Run:
    .venv/bin/uvicorn pipeline.loadtest_stub:app --host 127.0.0.1 --port 8001

Record fixtures first with: uv run python pipeline/loadtest.py --record
"""

import asyncio
import contextvars
import json
import os
from pathlib import Path

import httpx
from lxml import etree

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(os.environ.get("LOADTEST_FIXTURES", PROJECT_ROOT / "data" / "loadtest" / "fixtures"))

# Simulated upstream round-trip. A zero-latency stub understates how many requests sit
# parked awaiting bahn.de, which is exactly the state that holds memory under load.
UPSTREAM_LATENCY_MS = float(os.environ.get("LOADTEST_UPSTREAM_LATENCY_MS", "250"))


def _load_fixtures() -> tuple[dict, list, dict]:
    if not FIXTURES_DIR.is_dir():
        raise RuntimeError(
            f"{FIXTURES_DIR} not found - run: uv run python pipeline/loadtest.py --record"
        )
    journeys_dir = FIXTURES_DIR / "journeys"
    by_key, pool = {}, []
    for path in sorted(journeys_dir.glob("*.json")):
        entry = json.loads(path.read_text())
        req, resp = entry["request"], entry["response"]
        key = (req["from_id"], req["to_id"], req["departure"], req.get("paging_ref"))
        by_key[key] = resp
        pool.append(resp)
    if not pool:
        raise RuntimeError(f"no journey fixtures in {journeys_dir} - re-run --record")

    locations_path = FIXTURES_DIR / "locations.json"
    locations = json.loads(locations_path.read_text()) if locations_path.exists() else {}
    return by_key, pool, locations


_JOURNEYS_BY_KEY, _JOURNEY_POOL, _LOCATIONS = _load_fixtures()

# observability for the driver: how often a generated request missed the recorded set
_stats = {"journey_hit": 0, "journey_fallback": 0, "locations_hit": 0, "locations_fallback": 0}


# ---------------------------------------------------------------- cache-bust variants

# Only 12 fixtures exist, so their trains warm delays._cache within seconds and every
# later request is a dict lookup rather than a DuckDB scan -- which would report a
# capacity number several times too high. A variant is the same itinerary with each
# leg's train number swapped for a different real train that genuinely calls at that
# leg's arrival station, so cache keys stay cold while results stay truthful.
# Variants are pre-generated at startup: building them per request would charge the
# copy cost to the server process being measured.
VARIANTS = int(os.environ.get("LOADTEST_VARIANTS", "0"))

_variant_ctx: "contextvars.ContextVar[int | None]" = contextvars.ContextVar("loadtest_variant", default=None)
_VARIANTS_BY_KEY: dict[tuple, list[dict]] = {}
_VARIANT_POOL: list[list[dict]] = []


def _leg_evas(resp: dict) -> set[str]:
    from app import delays

    evas = set()
    for verbindung in resp.get("verbindungen", []):
        for leg in verbindung.get("verbindungsAbschnitte", []):
            ext = leg.get("ankunftsOrtExtId")
            if ext and (leg.get("verkehrsmittel") or {}).get("nummer"):
                evas.add(delays.pad_eva(str(ext)))
    return evas


def _trains_by_eva_hour(evas: set[str]) -> dict[tuple[str, int], list[str]]:
    """Real trains calling at each station, bucketed by arrival hour.

    leg_delay_stats rejects candidates more than 120 minutes from the leg's planned
    time-of-day, so a train picked at random for a station almost always returns no
    rows. Keying on the hour keeps the substituted train inside that window, which is
    what makes the variant produce genuine delay stats rather than an empty result.
    """
    import duckdb

    parquet = PROJECT_ROOT / "data" / "delays.parquet"
    rows = duckdb.connect().execute(
        "SELECT eva, hour(arrival_planned_time) h, ltrim(train_number,'0') t, count(*) c"
        f" FROM read_parquet('{parquet}')"
        " WHERE eva IN ({}) AND arrival_planned_time IS NOT NULL"
        " GROUP BY 1,2,3 HAVING c >= 5".format(",".join("?" * len(evas))),
        sorted(evas),
    ).fetchall()
    out: dict[tuple[str, int], list[str]] = {}
    for eva, hour, train, _ in rows:
        out.setdefault((eva, int(hour)), []).append(train)
    return out


def _build_variants() -> None:
    import copy
    import random

    from app import delays

    evas: set[str] = set()
    for resp in _JOURNEY_POOL:
        evas |= _leg_evas(resp)
    if not evas:
        return
    by_eva_hour = _trains_by_eva_hour(evas)
    rng = random.Random(42)

    def variant_of(resp: dict, n: int) -> dict:
        clone = copy.deepcopy(resp)
        for verbindung in clone.get("verbindungen", []):
            for leg in verbindung.get("verbindungsAbschnitte", []):
                vm = leg.get("verkehrsmittel") or {}
                ext = leg.get("ankunftsOrtExtId")
                arrival = (leg.get("ankunft") or {}).get("sollzeit")
                if not ext or not vm.get("nummer") or not arrival:
                    continue
                hour = delays.to_berlin_naive(arrival).hour
                pool = by_eva_hour.get((delays.pad_eva(str(ext)), hour))
                if pool:  # no substitute in that hour: keep the recorded train
                    vm["nummer"] = rng.choice(pool)
        return clone

    # _JOURNEYS_BY_KEY and _JOURNEY_POOL hold the *same* response objects, so build one
    # variant list per fixture and reference it from both -- generating two sets would
    # double the ~500KB-per-variant memory for no benefit.
    for resp in _JOURNEY_POOL:
        _VARIANT_POOL.append([variant_of(resp, n) for n in range(VARIANTS)])
    for key, resp in _JOURNEYS_BY_KEY.items():
        _VARIANTS_BY_KEY[key] = _VARIANT_POOL[_JOURNEY_POOL.index(resp)]


def _stable_pick(key: tuple) -> dict:
    """Deterministic fallback for un-recorded requests.

    Unknown keys must still return a full journey payload rather than an error, so the
    replan recursion in main._simulate_walk keeps descending to MAX_REPLANS and keeps
    hitting DuckDB. That recursion is the worst case worth measuring, and an empty
    response would silently skip it.
    """
    digest = sum(hash(part) for part in key if part is not None)
    idx = digest % len(_JOURNEY_POOL)
    variant = _variant_ctx.get()
    if variant is not None and _VARIANT_POOL:
        return _VARIANT_POOL[idx][variant % VARIANTS]
    return _JOURNEY_POOL[idx]


async def _stub_request(method: str, path: str, **kwargs):
    """Stands in for app.bahn_api._request."""
    await asyncio.sleep(UPSTREAM_LATENCY_MS / 1000)

    if path == "/angebote/fahrplan":
        body = kwargs.get("json") or {}
        key = (
            body.get("abfahrtsHalt"),
            body.get("ankunftsHalt"),
            body.get("anfrageZeitpunkt"),
            body.get("pagingReference"),
        )
        hit = _JOURNEYS_BY_KEY.get(key)
        if hit is None:
            _stats["journey_fallback"] += 1
            return _stable_pick(key)
        _stats["journey_hit"] += 1
        variant = _variant_ctx.get()
        if variant is not None and _VARIANTS_BY_KEY.get(key):
            return _VARIANTS_BY_KEY[key][variant % VARIANTS]
        return hit

    if path == "/reiseloesung/orte":
        query = (kwargs.get("params") or {}).get("suchbegriff", "")
        hit = _LOCATIONS.get(query.strip().lower())
        if hit is None:
            _stats["locations_fallback"] += 1
            return []
        _stats["locations_hit"] += 1
        return hit

    raise RuntimeError(f"unstubbed bahn.de path: {path}")


async def _stub_get_xml(path: str):
    """Stands in for app.live_delays._get_xml. Never reached while configured() is False,
    but rebound anyway so a code path that starts calling IRIS can't leak to the network."""
    return etree.Element("timetable")


def _umami_handler(request: httpx.Request) -> httpx.Response:
    """Analytics beacons fire on ~95% of sessions; without this every simulated visit
    would write a row into the real Umami postgres and pollute the dashboard."""
    if request.url.path.endswith("/script.js"):
        return httpx.Response(200, text="/* stubbed umami tracker */",
                              headers={"content-type": "application/javascript"})
    return httpx.Response(200, text="ok")


from app import bahn_api, live_delays, main  # noqa: E402  (import after fixtures load so a missing set fails fast)

bahn_api._request = _stub_request
live_delays._get_xml = _stub_get_xml
live_delays.configured = lambda: False
main.umami = httpx.AsyncClient(
    base_url="http://127.0.0.1:3001", timeout=5, transport=httpx.MockTransport(_umami_handler)
)

app = main.app

if VARIANTS:
    _build_variants()


@app.middleware("http")
async def _variant_middleware(request, call_next):
    """The driver tags each session with X-Loadtest-Variant so the stub can hand back a
    differently-trained itinerary, keeping the delays caches cold."""
    raw = request.headers.get("x-loadtest-variant")
    if raw is not None and VARIANTS:
        try:
            _variant_ctx.set(int(raw))
        except ValueError:
            pass
    return await call_next(request)


async def loadtest_stats():
    """Fixture hit rates and cache sizes, so a run can report how much of it was real work."""
    from app import delays

    return {
        "fixtures": dict(_stats),
        "upstreamLatencyMs": UPSTREAM_LATENCY_MS,
        "variants": VARIANTS,
        "caches": {
            "bahn": len(bahn_api._cache),
            "replan": len(main._replan_cache),
            "delayStats": len(delays._cache),
            "delayOnDate": len(delays._date_cache),
            "departureOnDate": len(delays._dep_date_cache),
        },
    }


# app.mount("/", StaticFiles(...)) in main.py catches every path, and Starlette matches
# routes in registration order, so anything appended after it is unreachable. Insert first.
app.add_api_route("/_loadtest/stats", loadtest_stats, methods=["GET"])
app.router.routes.insert(0, app.router.routes.pop())
