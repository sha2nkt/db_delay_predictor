import asyncio
import logging
import os
import random
import time
from collections import Counter, OrderedDict, deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable

import httpx
from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from app.config import env_int

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

# All tunables below read the environment once at import (systemd Environment=
# in production, .env locally via app.config) and fall back to safe defaults.
# Every piece of state in this module — caches, breaker, counters — is
# per-process; the app runs as a single uvicorn worker, so in practice these
# limits are global. With multiple workers each process would keep its own.
JOURNEYS_TTL = env_int("BAHN_CACHE_TTL_SECONDS", 300)
LOCATIONS_TTL = 600
CACHE_MAX = 512

# Nearby-stop lookup. bahn.de answers 422 for a radius past 10 km, and it ranks
# purely by distance — in a town the rail stops sit behind dozens of bus stops,
# so ask for a large page and pick the rail ones out of it ourselves.
NEARBY_RADIUS_M = 9999
NEARBY_MAX_RESULTS = 50

# how long to stop calling bahn.de after every profile has been blocked
BLOCK_COOLDOWN = 30

# spike insurance: bound how many requests we fan out to bahn.de at once
MAX_UPSTREAM_CONCURRENCY = env_int("BAHN_MAX_CONCURRENCY", 4)

# When bahn.de refuses (429/403/timeouts), a search that has an earlier answer
# should degrade to it instead of erroring: journeys are kept for an hour past
# their cache TTL and served with their age, which the frontend discloses.
STALE_TTL = env_int("BAHN_STALE_CACHE_TTL_SECONDS", 3600)
STALE_MAX = 256

# A route-level fallback answers a *different* departure time than the one asked
# for, so it may only stand in for a near-enough one: a 22:00 return served from
# a 17:00 search reads as the site ignoring the entered time, which is worse
# than an honest error.
STALE_ROUTE_MAX_SKEW = env_int("BAHN_STALE_ROUTE_SKEW_SECONDS", 1800)

# Circuit breaker: this many upstream failures (429/5xx/timeouts) within the
# window open it; while open every call fails fast (stale data still serves).
# The cooldown starts at the base, doubles per consecutive open, is capped at
# the max, and a bahn.de Retry-After header raises it to at least that value.
CIRCUIT_FAILURE_THRESHOLD = env_int("BAHN_CIRCUIT_FAILURE_THRESHOLD", 5)
CIRCUIT_FAILURE_WINDOW = env_int("BAHN_CIRCUIT_FAILURE_WINDOW_SECONDS", 60)
RATE_BASE_COOLDOWN = env_int("BAHN_429_BASE_COOLDOWN_SECONDS", 30)
RATE_MAX_COOLDOWN = env_int("BAHN_MAX_COOLDOWN_SECONDS", 300)
HALF_OPEN_PROBES = env_int("BAHN_HALF_OPEN_PROBES", 1)

# a 429 whose Retry-After promises relief within this many seconds is worth
# waiting out once inside the same request; anything longer fails over to stale
QUICK_RETRY_MAX_WAIT = 2.0

# page via ntfy when 429s keep coming: that means the demand-side mitigations
# are exhausted and users are seeing degraded results
ALERT_THRESHOLD = 8
ALERT_WINDOW = 300
ALERT_COOLDOWN = 1800

# One long-lived AsyncSession per impersonation profile: keeps TLS/HTTP2
# connection pooling and stays on one upstream identity per profile for the
# process lifetime — every app user deliberately shares it. Load is managed by
# caching, coalescing, the semaphore and the circuit breaker; a 429 never swaps
# the cookie jar or fingerprint (that would be rate-limit evasion).
_sessions: dict[str, requests.AsyncSession] = {}
_profile_idx = 0
_rotate_lock = asyncio.Lock()
_upstream_sem = asyncio.Semaphore(MAX_UPSTREAM_CONCURRENCY)
_stale: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()  # key -> (stored_at, data)
# "now" searches roll their 5-min departure bucket, so the exact key above misses
# minutes after a success; this second index answers by route + travel day instead:
# (from, to, dticket) -> (stored_at, departure_iso, data)
_stale_route: OrderedDict[tuple, tuple[float, str, dict]] = OrderedDict()
_rate_events: deque[float] = deque()
_last_alert = float("-inf")

# Pipeline counters, exposed via status() on /health. Keys are a small fixed
# set (no station names, IPs or full journey keys).
metrics: Counter[str] = Counter()


class UpstreamError(Exception):
    """Base for every failure talking to bahn.de. retry_after is a hint, in
    seconds, for how long callers should wait before trying again."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class UpstreamRateLimited(UpstreamError):
    """bahn.de answered 429 Too Many Requests."""


class UpstreamBlocked(UpstreamError):
    """Akamai 403-blocked every impersonation profile."""


class UpstreamUnavailable(UpstreamError):
    """5xx, network failure/timeout, or the circuit breaker is open."""


class UpstreamProtocolError(UpstreamError):
    """bahn.de answered with something that is not a usable API response."""


class CircuitBreaker:
    """CLOSED -> OPEN after `threshold` failures within `window` seconds (or
    immediately when bahn.de sends an explicit Retry-After); OPEN fails fast
    for a cooldown that doubles per consecutive open, jittered and capped;
    HALF-OPEN then admits `probes` trial calls — a success closes the circuit,
    a failure reopens it with a longer cooldown.

    Safe for a single event loop without locks: every method mutates state
    synchronously, so no await point can interleave a transition."""

    def __init__(self, threshold: int, window: float, base_cooldown: float,
                 max_cooldown: float, probes: int,
                 clock: Callable[[], float] = time.monotonic):
        self._threshold = threshold
        self._window = window
        self._base = base_cooldown
        self._max = max_cooldown
        self._probes = probes
        self._clock = clock
        self.state = "closed"
        self._failures: deque[float] = deque()
        self._until = 0.0
        self._streak = 0  # consecutive opens without a successful close
        self._probes_left = 0
        self._cooldown = 0.0

    def acquire(self) -> bool:
        """Reserve one upstream call; True means it is a half-open probe.
        Raises UpstreamUnavailable while the circuit is open."""
        now = self._clock()
        if self.state == "open":
            if now < self._until:
                metrics["circuit_rejected"] += 1
                raise UpstreamUnavailable(
                    "bahn.de calls paused by circuit breaker",
                    retry_after=self._until - now,
                )
            self._transition("half-open")
            self._probes_left = self._probes
        if self.state == "half-open":
            if self._probes_left <= 0:
                metrics["circuit_rejected"] += 1
                raise UpstreamUnavailable(
                    "bahn.de probe already in flight", retry_after=self._base
                )
            self._probes_left -= 1
            return True
        return False

    def release(self, probe: bool) -> None:
        """Return a reserved call unused (cancelled mid-flight): no evidence
        either way, so only the probe slot is given back."""
        if probe and self.state == "half-open":
            self._probes_left += 1

    def record_success(self, probe: bool) -> None:
        if probe and self.state == "half-open":
            self._streak = 0
            self._failures.clear()
            self._transition("closed")

    def record_failure(self, probe: bool, cooldown_floor: float | None = None) -> None:
        """cooldown_floor is an explicit upstream Retry-After: authoritative,
        so it opens the circuit immediately rather than counting toward the
        threshold."""
        if cooldown_floor is not None and cooldown_floor <= 0:
            cooldown_floor = None  # "retry now" carries no pause request
        if probe and self.state == "half-open":
            self._open(cooldown_floor)
            return
        now = self._clock()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()
        if self.state == "closed" and (
            len(self._failures) >= self._threshold or cooldown_floor is not None
        ):
            self._open(cooldown_floor)

    def force_open(self, cooldown: float) -> None:
        """Open for a fixed cooldown regardless of failure counts (the 403
        every-profile-blocked path)."""
        self._cooldown = cooldown
        self._until = self._clock() + cooldown
        metrics["circuit_opened"] += 1
        self._transition("open")

    def _open(self, floor: float | None) -> None:
        if floor is not None:
            # bahn.de named a wait: that number is better information than any
            # backoff we could guess, so use it as-is (capped) and don't let the
            # escalation ladder stretch a 45 s ask into minutes. The streak is
            # left alone for the same reason — an answered "wait 45 s" is not
            # evidence that the next cooldown should be longer.
            cooldown = min(floor, self._max)
        else:
            cooldown = min(self._max, self._base * (2 ** self._streak) * random.uniform(1.0, 1.25))
            self._streak += 1
        self._cooldown = cooldown
        self._until = self._clock() + cooldown
        metrics["circuit_opened"] += 1
        self._transition("open")

    def _transition(self, new: str) -> None:
        if new != self.state:
            detail = f" for {self._cooldown:.0f}s" if new == "open" else ""
            log.warning("bahn.de circuit breaker: %s -> %s%s", self.state, new, detail)
            self.state = new

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "retryIn": max(0, round(self._until - self._clock())) if self.state == "open" else 0,
            "recentFailures": len(self._failures),
        }


_breaker = CircuitBreaker(
    threshold=CIRCUIT_FAILURE_THRESHOLD,
    window=CIRCUIT_FAILURE_WINDOW,
    base_cooldown=RATE_BASE_COOLDOWN,
    max_cooldown=RATE_MAX_COOLDOWN,
    probes=HALF_OPEN_PROBES,
)

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


async def _alert(text: str) -> None:
    """Never raises — an unreachable notifier must not affect a search."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{base}/{topic}",
                content=text.encode("utf-8"),
                headers={"Title": "DelayBahn upstream alert", "Tags": "warning"},
            )
    except httpx.HTTPError as exc:
        log.warning("ntfy alert failed: %s", exc)


def _note_429(profile: str, path: str, retry_after: float | None) -> None:
    """Log every upstream 429 distinctly from other failures and page via ntfy
    when they keep coming. Never logs cookies or response bodies."""
    global _last_alert
    now = time.monotonic()
    _rate_events.append(now)
    while _rate_events and now - _rate_events[0] > ALERT_WINDOW:
        _rate_events.popleft()
    log.warning(
        "bahn.de 429: profile=%s path=%s retry_after=%s recent=%d/%ds",
        profile, path, retry_after, len(_rate_events), ALERT_WINDOW,
    )
    if len(_rate_events) >= ALERT_THRESHOLD and now - _last_alert > ALERT_COOLDOWN:
        _last_alert = now
        asyncio.ensure_future(_alert(
            f"bahn.de is rate-limiting: {len(_rate_events)} HTTP 429s in the last "
            f"{ALERT_WINDOW // 60} min. Searches are degrading to cached/stale results."
        ))


def _retry_after_seconds(resp) -> float | None:
    """Seconds bahn.de asks us to wait, from either Retry-After form
    (delta-seconds or HTTP-date); None when absent or unparsable."""
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


async def _request(method: str, path: str, **kwargs) -> dict:
    """One upstream call under the circuit breaker: fails fast while the
    circuit is open, otherwise issues the request and feeds the outcome back."""
    probe = _breaker.acquire()
    try:
        data = await _issue(method, path, **kwargs)
    except UpstreamRateLimited as e:
        _breaker.record_failure(probe, cooldown_floor=e.retry_after)
        raise
    except UpstreamBlocked:
        # wholesale Akamai block: not a rolling-window signal, pause outright
        _breaker.force_open(BLOCK_COOLDOWN)
        raise
    except (UpstreamUnavailable, UpstreamProtocolError):
        _breaker.record_failure(probe)
        raise
    except BaseException:
        # cancellation or an unexpected bug: no upstream evidence either way
        _breaker.release(probe)
        raise
    _breaker.record_success(probe)
    return data


async def _issue(method: str, path: str, **kwargs) -> dict:
    """Issue one request to bahn.de, rotating impersonation profiles on 403 as
    before, and translate every failure mode into the UpstreamError taxonomy.
    The only retry is a single same-session wait when a 429 carries a
    Retry-After of at most QUICK_RETRY_MAX_WAIT seconds."""
    rotations = 0
    quick_retried = False
    while True:
        profile = PROFILES[_profile_idx]
        metrics["upstream_requests"] += 1
        try:
            async with _upstream_sem:
                resp = await getattr(_session(profile), method)(f"{BASE_URL}{path}", **kwargs)
        except RequestException as e:
            # DNS/connect failures and timeouts land here
            metrics["upstream_network_errors"] += 1
            log.warning("bahn.de unreachable: profile=%s path=%s error=%s",
                        profile, path, type(e).__name__)
            raise UpstreamUnavailable(f"bahn.de unreachable: {type(e).__name__}") from e

        status = resp.status_code
        if status == 403:
            metrics["upstream_403"] += 1
            rotations += 1
            if rotations < len(PROFILES):
                await _rotate(profile)
                continue
            raise UpstreamBlocked("bahn.de blocked every impersonation profile")
        if status == 429:
            metrics["upstream_429"] += 1
            retry_after = _retry_after_seconds(resp)
            _note_429(profile, path, retry_after)
            if not quick_retried and retry_after is not None and 0 < retry_after <= QUICK_RETRY_MAX_WAIT:
                # honor a short advertised wait once, on the same session —
                # never a retry loop, never a fresh identity
                quick_retried = True
                await asyncio.sleep(retry_after + random.uniform(0, 0.5))
                continue
            raise UpstreamRateLimited("bahn.de rate limit", retry_after=retry_after)
        if status >= 500:
            metrics["upstream_5xx"] += 1
            log.warning("bahn.de %d: path=%s profile=%s", status, path, profile)
            raise UpstreamUnavailable(f"bahn.de responded {status}")
        if status >= 400:
            metrics["upstream_4xx"] += 1
            log.warning("bahn.de %d: path=%s profile=%s", status, path, profile)
            raise UpstreamProtocolError(f"bahn.de responded {status}")
        try:
            data = resp.json()
        except Exception as e:
            metrics["upstream_malformed"] += 1
            log.warning("bahn.de malformed response: path=%s profile=%s error=%s",
                        path, profile, type(e).__name__)
            raise UpstreamProtocolError("bahn.de returned a malformed response") from e
        metrics["upstream_200"] += 1
        return data


def _cached(key: tuple, ttl: int, call: Callable[[], Awaitable],
            on_success: Callable[[dict], None] | None = None) -> "asyncio.Task":
    hit = _cache.get(key)
    if hit and (time.monotonic() < hit[0] or not hit[1].done()):
        # a finished task is a fresh hit; an unfinished one means this caller
        # coalesces onto an upstream request already in flight
        metrics["cache_hits" if hit[1].done() else "cache_coalesced"] += 1
        _cache.move_to_end(key)
        return hit[1]

    metrics["cache_misses"] += 1
    task = asyncio.ensure_future(call())
    _cache[key] = (time.monotonic() + ttl, task)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)

    def settle(done: "asyncio.Task") -> None:
        # never serve a cached error: a block clears as soon as we rotate
        if done.cancelled() or done.exception() is not None:
            entry = _cache.get(key)
            if entry and entry[1] is done:
                del _cache[key]
        elif on_success is not None:
            # invoked here, at fetch completion, so a remembered age is the
            # data's real age rather than the age of the newest cache hit
            on_success(done.result())

    task.add_done_callback(settle)
    return task


async def close() -> None:
    for session in _sessions.values():
        await session.close()
    _sessions.clear()


async def locations(query: str) -> list[dict]:
    # shield: the fetch task is shared with concurrent identical callers, so
    # one waiter's disconnect must not cancel it for everyone else
    return await asyncio.shield(_cached(
        ("locations", query.strip().lower()),
        LOCATIONS_TTL,
        lambda: _request("get", "/reiseloesung/orte", params={"suchbegriff": query, "typ": "ALL", "limit": 8}),
    ))


async def nearby(lat: float, lon: float) -> list[dict]:
    """Stops around a coordinate as bahn.de ranks them — nearest first, every
    mode of transport, each entry carrying its own lat/lon and product list."""
    # shield for the same reason locations() does. Callers round the coordinates,
    # so a cache entry covers a grid cell rather than one visitor's exact position
    return await asyncio.shield(_cached(
        ("nearby", lat, lon),
        LOCATIONS_TTL,
        lambda: _request("get", "/reiseloesung/orte/nearby",
                         params={"lat": lat, "long": lon,
                                 "radius": NEARBY_RADIUS_M, "maxNo": NEARBY_MAX_RESULTS}),
    ))


def _departure_skew(a: str, b: str) -> float:
    """Seconds between two departure timestamps; inf when either is unparsable,
    so an unusable value can never qualify as a near-enough fallback."""
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds())
    except ValueError:
        return float("inf")


async def journeys(from_id: str, to_id: str, departure_iso: str, paging_ref: str | None = None,
                   dticket: bool = False) -> tuple[dict, int]:
    """Returns (data, stale_age_seconds); age is 0 for a fresh answer, else how
    old the served fallback is. from_id/to_id are full HAFAS location ids
    (A=1@O=...@L=...@) from locations().

    paging_ref is a verbindungReference.earlier/later token from a previous response;
    when set, the API returns the adjacent result page instead of the requested time.

    dticket=True restricts results to Deutschland-Ticket-valid connections — the same
    two flags the bahn.de search mask toggle sends.
    """
    # Searches default to "now", so the departure minute fragments the cache: the
    # same route searched a minute apart misses every time. Floor to 5-minute
    # buckets — results then start at most 4 minutes earlier than asked for.
    if len(departure_iso) >= 16 and departure_iso[14:16].isdigit():
        minute = int(departure_iso[14:16])
        departure_iso = f"{departure_iso[:14]}{minute - minute % 5:02d}:00"
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
        "deutschlandTicketVorhanden": dticket,
        "nurDeutschlandTicketVerbindungen": dticket,
    }
    if paging_ref:
        body["pagingReference"] = paging_ref
    key = ("journeys", from_id, to_id, departure_iso, paging_ref, dticket)
    # paged responses are offsets into a result list, so they only ever stand in
    # for the same page (exact key), never for the route's primary answer
    route = (from_id, to_id, dticket) if paging_ref is None else None

    def keep(data: dict) -> None:
        now = time.monotonic()
        _stale[key] = (now, data)
        _stale.move_to_end(key)
        while len(_stale) > STALE_MAX:
            _stale.popitem(last=False)
        if route is not None:
            _stale_route[route] = (now, departure_iso, data)
            _stale_route.move_to_end(route)
            while len(_stale_route) > STALE_MAX:
                _stale_route.popitem(last=False)

    metrics["searches"] += 1
    try:
        # shield: the fetch task is shared with concurrent identical callers,
        # so one waiter's disconnect must not cancel it for everyone else
        data = await asyncio.shield(_cached(
            key, JOURNEYS_TTL,
            lambda: _request("post", "/angebote/fahrplan", json=body),
            on_success=keep,
        ))
    except UpstreamError:
        now = time.monotonic()
        hit = _stale.get(key)
        if hit and now - hit[0] <= STALE_TTL:
            metrics["stale_hits"] += 1
            age = int(now - hit[0])
            log.warning("bahn.de unavailable; serving %ss-old journeys for %s -> %s", age, from_id, to_id)
            return hit[1], age
        # same route and a near-enough departure bucket: still a useful answer,
        # and never across days — a past-mode compensation check must not get
        # another day's journeys
        r = _stale_route.get(route) if route is not None else None
        if (r and now - r[0] <= STALE_TTL and r[1][:10] == departure_iso[:10]
                and _departure_skew(r[1], departure_iso) <= STALE_ROUTE_MAX_SKEW):
            metrics["stale_hits_route"] += 1
            age = int(now - r[0])
            log.warning("bahn.de unavailable; serving %ss-old journeys (route match) for %s -> %s",
                        age, from_id, to_id)
            return r[2], age
        metrics["stale_misses"] += 1
        raise
    return data, 0


def healthy() -> bool:
    """True when bahn.de is answering normally. Optional enrichment calls check
    this first so they don't spend a strained upstream budget that the searches
    users are actually waiting on need."""
    return _breaker.state == "closed"


def status() -> dict:
    """Circuit state and pipeline counters for /health."""
    return {"circuit": _breaker.snapshot(), "counters": dict(metrics)}
