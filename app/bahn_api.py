import asyncio
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable

from curl_cffi import requests
from curl_cffi.requests.exceptions import HTTPError

log = logging.getLogger(__name__)

# bahn.de sits behind Akamai Bot Manager, which fingerprints the TLS/HTTP2
# client (not just cookies) and returns 403 OPS_BLOCKED to plain HTTP stacks
# like httpx/requests. curl_cffi's impersonate=... reproduces a real browser
# ClientHello, which passes the check with no cookie warmup needed.
BASE_URL = "https://www.bahn.de/web/api"

# Akamai flags one fingerprint at a time: on 2026-07-23 every chrome profile
# started getting 403 OPS_BLOCKED (fresh sessions too, so it was the
# fingerprint rather than cookies or rate) while firefox and safari passed.
# So keep spares and rotate to the next profile whenever one gets blocked.
PROFILES = ["firefox135", "safari17_0", "chrome"]

JOURNEYS_TTL = 120
LOCATIONS_TTL = 600
CACHE_MAX = 512

# how long to stop calling bahn.de after every profile has been blocked
BLOCK_COOLDOWN = 30

_sessions: dict[str, requests.AsyncSession] = {}
_profile_idx = 0
_rotate_lock = asyncio.Lock()
_blocked_until = 0.0


def _trip_breaker() -> None:
    global _blocked_until
    _blocked_until = time.monotonic() + BLOCK_COOLDOWN
    log.warning("bahn.de blocked every profile; pausing upstream calls for %ss", BLOCK_COOLDOWN)

# key -> (expires_at, task). The task is shared, so concurrent callers asking
# for the same thing during a traffic spike ride one upstream request.
_cache: OrderedDict[tuple, tuple[float, "asyncio.Task"]] = OrderedDict()

ALL_PRODUCTS = ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"]


def _session(profile: str) -> requests.AsyncSession:
    if profile not in _sessions:
        _sessions[profile] = requests.AsyncSession(
            impersonate=profile,
            timeout=20,
            headers={"Accept": "application/json"},
        )
    return _sessions[profile]


async def _rotate(blocked: str) -> None:
    global _profile_idx
    async with _rotate_lock:
        if PROFILES[_profile_idx] != blocked:
            return  # another request already rotated away from this profile
        _profile_idx = (_profile_idx + 1) % len(PROFILES)
        log.warning("bahn.de blocked impersonate=%s, switching to %s", blocked, PROFILES[_profile_idx])


async def _request(method: str, path: str, **kwargs):
    # Every profile 403'd recently: Akamai is blocking us wholesale, and retrying all
    # three profiles per request would triple our outbound rate at exactly the moment
    # that risks a lasting IP ban. Fail fast until the cooldown expires.
    if time.monotonic() < _blocked_until:
        raise HTTPError("bahn.de blocked all profiles; backing off")

    for attempt in range(len(PROFILES)):
        profile = PROFILES[_profile_idx]
        resp = await getattr(_session(profile), method)(f"{BASE_URL}{path}", **kwargs)
        if resp.status_code == 403 and attempt < len(PROFILES) - 1:
            await _rotate(profile)
            continue
        if resp.status_code == 403:
            _trip_breaker()
        resp.raise_for_status()
        return resp.json()


def _cached(key: tuple, ttl: int, call: Callable[[], Awaitable]) -> "asyncio.Task":
    hit = _cache.get(key)
    if hit and (time.monotonic() < hit[0] or not hit[1].done()):
        _cache.move_to_end(key)
        return hit[1]

    task = asyncio.ensure_future(call())
    _cache[key] = (time.monotonic() + ttl, task)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)

    def drop_if_failed(done: "asyncio.Task") -> None:
        # never serve a cached error: a block clears as soon as we rotate
        if done.cancelled() or done.exception() is not None:
            entry = _cache.get(key)
            if entry and entry[1] is done:
                del _cache[key]

    task.add_done_callback(drop_if_failed)
    return task


async def close() -> None:
    for session in _sessions.values():
        await session.close()
    _sessions.clear()


async def locations(query: str) -> list[dict]:
    return await _cached(
        ("locations", query.strip().lower()),
        LOCATIONS_TTL,
        lambda: _request("get", "/reiseloesung/orte", params={"suchbegriff": query, "typ": "ALL", "limit": 8}),
    )


async def journeys(from_id: str, to_id: str, departure_iso: str, paging_ref: str | None = None) -> dict:
    """from_id/to_id are full HAFAS location ids (A=1@O=...@L=...@) from locations().

    paging_ref is a verbindungReference.earlier/later token from a previous response;
    when set, the API returns the adjacent result page instead of the requested time.
    """
    body = {
        "abfahrtsHalt": from_id,
        "ankunftsHalt": to_id,
        "anfrageZeitpunkt": departure_iso,
        "ankunftSuche": "ABFAHRT",
        "klasse": "KLASSE_2",
        "produktgattungen": ALL_PRODUCTS,
        "reisende": [{
            "typ": "ERWACHSENER",
            "ermaessigungen": [{"art": "KEINE_ERMAESSIGUNG", "klasse": "KLASSENLOS"}],
            "alter": [],
            "anzahl": 1,
        }],
        "schnelleVerbindungen": True,
        "sitzplatzOnly": False,
        "bikeCarriage": False,
        "reservierungsKontingenteVorhanden": False,
    }
    if paging_ref:
        body["pagingReference"] = paging_ref
    return await _cached(
        ("journeys", from_id, to_id, departure_iso, paging_ref),
        JOURNEYS_TTL,
        lambda: _request("post", "/angebote/fahrplan", json=body),
    )
