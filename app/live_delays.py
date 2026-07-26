"""On-demand delay lookup straight from the DB Timetables API (IRIS).

The nightly pipeline stores IRIS `fchg` change times; this module reads the same
values live, so a journey finished minutes ago answers with exactly the number
tomorrow's parquet will hold. Covers only what IRIS still reports (a few hours
back) - anything older stays the parquet's job.

Two calls per station are needed: `plan` maps (train number, planned time) to the
IRIS stop id, `fchg` carries the change times keyed by that id.
"""

import asyncio
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from lxml import etree

log = logging.getLogger(__name__)

BASE_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
# IRIS timestamps are Europe/Berlin local, like the parquet's
BERLIN = ZoneInfo("Europe/Berlin")

# a published plan hour never changes; change messages keep arriving while a train runs
PLAN_TTL = 6 * 3600
FCHG_TTL = 60
CACHE_MAX = 2048
CONCURRENCY = 10
# ceiling on API calls one search may trigger; legs left unresolved render as pending
MAX_CALLS_PER_WARM = 150
# a stop this close to the hour boundary may be filed under either hour
BOUNDARY_MIN = 5
# planned times from bahn.de and from IRIS can differ by a rounding minute
MATCH_TOLERANCE_MIN = 2

_client: httpx.AsyncClient | None = None
_sem: asyncio.Semaphore | None = None

# key -> (expires_at, task); the task is shared so concurrent searches over the
# same station ride one upstream request, as in bahn_api
_cache: OrderedDict[tuple, tuple[float, "asyncio.Task"]] = OrderedDict()


def _load_dotenv() -> None:
    """Local dev convenience; in production systemd passes the credentials in."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        key, _, value = line.partition("=")
        key = key.strip()
        if key and not key.startswith("#") and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


_load_dotenv()


def configured() -> bool:
    return bool(os.environ.get("DB_API_KEY") and os.environ.get("DB_CLIENT_ID"))


def _iris_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%y%m%d%H%M")
    except ValueError:
        return None


def _session() -> httpx.AsyncClient:
    global _client, _sem
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=10,
            headers={
                "DB-Api-Key": os.environ["DB_API_KEY"],
                "DB-Client-Id": os.environ["DB_CLIENT_ID"],
                "Accept": "application/xml",
            },
        )
        _sem = asyncio.Semaphore(CONCURRENCY)
    return _client


async def close() -> None:
    global _client, _sem
    if _client is not None:
        await _client.aclose()
    _client, _sem = None, None
    _cache.clear()


async def _get_xml(path: str):
    client = _session()  # also creates the semaphore, so do it before acquiring
    async with _sem:
        resp = await client.get(path)
    if resp.status_code == 404:
        return etree.Element("timetable")  # station or hour unknown to IRIS: no data, not an error
    resp.raise_for_status()
    return etree.fromstring(resp.content)


async def _fetch_plan(eva: str, day: str, hour: int) -> list[dict]:
    """Planned stops at one station in one hour: train number and planned times per stop id."""
    root = await _get_xml(f"/plan/{eva}/{day}/{hour:02d}")
    stops = []
    for s in root.findall("s"):
        tl = s.find("tl")
        if tl is None or not s.get("id"):
            continue
        ar, dp = s.find("ar"), s.find("dp")
        stops.append({
            "id": s.get("id"),
            "train": (tl.get("n") or "").lstrip("0"),
            "ar_pt": _iris_dt(ar.get("pt") if ar is not None else None),
            "dp_pt": _iris_dt(dp.get("pt") if dp is not None else None),
        })
    return stops


async def _fetch_fchg(eva: str) -> dict[str, dict]:
    """All known changes at one station, keyed by IRIS stop id."""
    root = await _get_xml(f"/fchg/{eva}")
    changes = {}
    for s in root.findall("s"):
        if not s.get("id"):
            continue
        ar, dp = s.find("ar"), s.find("dp")
        ar_clt = ar.get("clt") if ar is not None else None
        dp_clt = dp.get("clt") if dp is not None else None
        # latest delay-cause message (<m t="d" c="43"/>) anywhere on the stop;
        # ts is yymmddhhmm, so string comparison orders chronologically
        reason, reason_ts = None, ""
        for m in s.iter("m"):
            code = m.get("c")
            if m.get("t") == "d" and code and code.isdigit() and (m.get("ts") or "") >= reason_ts:
                reason, reason_ts = int(code), m.get("ts") or ""
        changes[s.get("id")] = {
            "ar_ct": _iris_dt(ar.get("ct") if ar is not None else None),
            "dp_ct": _iris_dt(dp.get("ct") if dp is not None else None),
            "canceled": bool(ar_clt or dp_clt),
            "reason": reason,
        }
    return changes


def _task(key: tuple, ttl: int, coro_factory) -> "asyncio.Task":
    hit = _cache.get(key)
    if hit and (time.monotonic() < hit[0] or not hit[1].done()):
        _cache.move_to_end(key)
        return hit[1]

    task = asyncio.ensure_future(coro_factory())
    _cache[key] = (time.monotonic() + ttl, task)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)

    def drop_if_failed(done: "asyncio.Task") -> None:
        # a transient API error must not be served for the whole TTL
        if done.cancelled() or done.exception() is not None:
            entry = _cache.get(key)
            if entry and entry[1] is done:
                del _cache[key]

    task.add_done_callback(drop_if_failed)
    return task


def _plan_hours(planned: datetime) -> list[tuple[str, int]]:
    """Hour buckets a stop may be filed under, nearest first."""
    day = planned.strftime("%y%m%d")
    hours = [(day, planned.hour)]
    if planned.minute >= 60 - BOUNDARY_MIN:
        nxt = planned + timedelta(hours=1)
        hours.append((nxt.strftime("%y%m%d"), nxt.hour))
    elif planned.minute < BOUNDARY_MIN:
        prev = planned - timedelta(hours=1)
        hours.append((prev.strftime("%y%m%d"), prev.hour))
    return hours


async def warm(stops: set[tuple[str, datetime]]) -> None:
    """Prefetch everything the sync lookups below will need for one search.

    `stops` is the set of (padded eva, planned local time) the itineraries touch.
    Failures are swallowed: an unresolved leg reads as unknown, never as an error.
    """
    if not configured() or not stops:
        return
    keys = []
    for eva, planned in stops:
        keys.append(("fchg", eva))
        for day, hour in _plan_hours(planned):
            keys.append(("plan", eva, day, hour))

    seen, tasks = set(), []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        if len(tasks) >= MAX_CALLS_PER_WARM:
            log.warning("live_delays: warm capped at %d calls, %d keys dropped",
                        MAX_CALLS_PER_WARM, len(set(keys)) - len(tasks))
            break
        if key[0] == "fchg":
            tasks.append(_task(key, FCHG_TTL, lambda e=key[1]: _fetch_fchg(e)))
        else:
            _, eva, day, hour = key
            tasks.append(_task(key, PLAN_TTL, lambda e=eva, d=day, h=hour: _fetch_plan(e, d, h)))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        log.warning("live_delays: %d/%d calls failed, first: %r", len(errors), len(results), errors[0])


def _ready(key: tuple):
    """Result of an already-warmed call, or None if missing, pending or failed."""
    hit = _cache.get(key)
    if not hit or not hit[1].done() or hit[1].cancelled() or hit[1].exception() is not None:
        return None
    return hit[1].result()


def _lookup(train_number: str, eva_padded: str, planned: datetime, kind: str) -> dict | None:
    """Delay of one train's arrival/departure at one station, from warmed IRIS data."""
    if not configured():
        return None
    train_number = train_number.lstrip("0")
    pt_key = "ar_pt" if kind == "ar" else "dp_pt"

    stop_id, stop_pt, best = None, None, None
    for day, hour in _plan_hours(planned):
        for stop in _ready(("plan", eva_padded, day, hour)) or ():
            if stop["train"] != train_number or stop[pt_key] is None:
                continue
            off = abs((stop[pt_key] - planned).total_seconds()) / 60
            if off <= MATCH_TOLERANCE_MIN and (best is None or off < best):
                best, stop_id, stop_pt = off, stop["id"], stop[pt_key]
    if stop_id is None:
        return None  # IRIS doesn't know this stop (yet): unknown, not on time

    changes = _ready(("fchg", eva_padded))
    if changes is None:
        return None
    change = changes.get(stop_id)
    reason = change["reason"] if change else None
    if change and change["canceled"]:
        return {"delayMin": None, "canceled": True, "reason": reason}  # known ahead of time, and a fact
    ct = (change["ar_ct"] if kind == "ar" else change["dp_ct"]) if change else None
    # "no change reported" means on time only once the stop is behind us; for one
    # still ahead it is a prognosis, and claiming punctuality would be wrong
    if (ct or stop_pt) > datetime.now(BERLIN).replace(tzinfo=None):
        return None
    if ct is None:
        return {"delayMin": 0, "canceled": False, "reason": reason}
    # measure against IRIS's own planned time, exactly as the parquet build does
    return {"delayMin": round((ct - stop_pt).total_seconds() / 60), "canceled": False, "reason": reason}


def leg_delay_on_date(train_number: str, eva_padded: str, planned_arrival_local: datetime) -> dict | None:
    return _lookup(train_number, eva_padded, planned_arrival_local, "ar")


def leg_departure_on_date(train_number: str, eva_padded: str, planned_departure_local: datetime) -> dict | None:
    return _lookup(train_number, eva_padded, planned_departure_local, "dp")
