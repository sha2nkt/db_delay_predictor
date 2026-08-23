import asyncio
import logging
import os
import random
import time
from collections import Counter, OrderedDict, deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from typing import Awaitable, Callable

import httpx
from curl_cffi import requests
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
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
# So keep spares and move to the next profile — on a fresh session — whenever
# one gets blocked.
PROFILES = ["firefox135", "safari17_0", "chrome"]

# All tunables below read the environment once at import (systemd Environment=
# in production, .env locally via app.config) and fall back to safe defaults.
# Every piece of state in this module — caches, breaker, counters — is
# per-process; the app runs as a single uvicorn worker, so in practice these
# limits are global. With multiple workers each process would keep its own.
JOURNEYS_TTL = env_int("BAHN_CACHE_TTL_SECONDS", 300)
LOCATIONS_TTL = 600
CACHE_MAX = 512

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

# A connection that dies mid-request carries no verdict on bahn.de: the socket
# never completed an exchange, and libcurl drops the dead one from the pool, so a
# second attempt opens a fresh connection and usually lands. Without this, every
# transport blip reached the user as a "servers are busy" 503, indistinguishable
# from real rate limiting.
NET_RETRIES = env_int("BAHN_NETWORK_RETRIES", 1)
NET_RETRY_BACKOFF = 0.25

# Only connection-level failures are worth a second attempt. Timeout is
# deliberately absent: the connection stood and bahn.de simply took too long, so
# retrying would double the user's wait for the same likely outcome.
RETRYABLE_NETWORK_ERRORS = (CurlConnectionError,)

# page via ntfy when 429s keep coming: that means the demand-side mitigations
# are exhausted and users are seeing degraded results
ALERT_THRESHOLD = 8
ALERT_WINDOW = 300
ALERT_COOLDOWN = 1800

# One rollup line per this many calls actually sent to bahn.de, so the journal carries
# a time series of our real upstream volume — the quantity the 429s answer — instead of
# only the running totals /health reports. Piggybacked on the calls themselves, so a
# quiet night logs nothing at all.
UPSTREAM_LOG_EVERY = env_int("BAHN_UPSTREAM_LOG_EVERY", 100)

# Session hygiene: a connection held for the process lifetime accumulates a
# cookie jar and a request history that Akamai scores as one continuous
# client, so cap how much any single session carries. The request budget is
# drawn per session and jittered so recycles don't land on a metronome; the
# age cap covers quiet hours, when the budget alone would take far too long
# to spend.
SESSION_MIN_REQUESTS = env_int("BAHN_SESSION_MIN_REQUESTS", 50)
SESSION_MAX_REQUESTS = env_int("BAHN_SESSION_MAX_REQUESTS", 100)
SESSION_MAX_AGE = env_int("BAHN_SESSION_MAX_AGE_SECONDS", 600)
# longer than the 20s request timeout: nothing can still be in flight on a
# retired session by the time its deferred close fires
SESSION_CLOSE_DELAY = 30

# One live identity at a time: one impersonation profile + one implicit cookie
# jar + one connection, created and discarded as a unit. Recycled once its
# request budget or age is spent, so no single session accumulates enough
# history to be scored as a heavy client, and burned outright on a 403 —
# swapping the fingerprint in place on a jar Akamai has already flagged is
# itself a bot tell. Load is managed by caching, coalescing, the semaphore and
# the circuit breaker; a 429 never *causes* a session or fingerprint swap
# (that would be rate-limit evasion) — count/age recycling is scheduled
# hygiene, not a reaction to refusals.
_ident: SimpleNamespace | None = None  # session, profile, created_at, used, budget
_retiring: list = []  # swapped-out sessions awaiting their deferred close
_close_tasks: set = set()  # pending _close_later timers, cancelled by close()
_profile_idx = 0
# serializes 403 burns so concurrent failures on one identity burn it once
_rotate_lock = asyncio.Lock()
_upstream_sem = asyncio.Semaphore(MAX_UPSTREAM_CONCURRENCY)
_stale: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()  # key -> (stored_at, data)
# "now" searches roll their 5-min departure bucket, so the exact key above misses
# minutes after a success; this second index answers by route + travel day instead:
# (from, to, dticket) -> (stored_at, departure_iso, data)
_stale_route: OrderedDict[tuple, tuple[float, str, dict]] = OrderedDict()
_rate_events: deque[float] = deque()
_last_alert = float("-inf")
_upstream_since: Counter[str] = Counter()  # per-source calls since the last rollup line
_upstream_logged_at = time.monotonic()
_upstream_logged_429 = 0

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
    # raw constructor only; identity lifecycle lives in _acquire_identity
    return requests.AsyncSession(
        impersonate=profile,
        timeout=20,
        headers={"Accept": "application/json"},
    )


def _acquire_identity() -> SimpleNamespace:
    """Return the current identity, first recycling it if its request budget
    or age is spent. Sync on purpose: called inside _upstream_sem with no
    await points, so no interleaving is possible."""
    global _ident
    if _ident is not None:
        age = time.monotonic() - _ident.created_at
        if _ident.used >= _ident.budget:
            _retire(_ident, f"count ({_ident.used} requests)")
        elif age >= SESSION_MAX_AGE:
            _retire(_ident, f"age ({age:.0f}s)")
    if _ident is None:
        profile = PROFILES[_profile_idx]
        _ident = SimpleNamespace(
            session=_session(profile),
            profile=profile,
            created_at=time.monotonic(),
            used=0,
            budget=random.randint(SESSION_MIN_REQUESTS, SESSION_MAX_REQUESTS),
        )
    _ident.used += 1
    return _ident


def _retire(ident: SimpleNamespace, reason: str) -> None:
    """Take an identity out of service and schedule its close. Sync, no
    awaits: the next _acquire_identity builds a replacement with a fresh
    connection and an empty cookie jar."""
    global _ident
    _ident = None
    _retiring.append(ident.session)
    metrics["session_recycled"] += 1
    log.info("bahn.de session recycled (%s) after %d requests, %.0fs on profile=%s",
             reason, ident.used, time.monotonic() - ident.created_at, ident.profile)
    task = asyncio.ensure_future(_close_later(ident.session))
    _close_tasks.add(task)
    task.add_done_callback(_close_tasks.discard)


async def _close_later(session) -> None:
    """Close a retired session once anything still in flight on it has
    finished (bounded by the 20s request timeout). Never raises: a close
    failure must not surface into a request path."""
    try:
        await asyncio.sleep(SESSION_CLOSE_DELAY)
        close_fn = getattr(session, "close", None)  # test fakes may lack close()
        if close_fn is not None:
            await close_fn()
    except Exception as e:
        log.warning("closing retired bahn.de session failed: %s", type(e).__name__)
    finally:
        if session in _retiring:
            _retiring.remove(session)


async def _burn(ident: SimpleNamespace) -> None:
    """403: Akamai flagged this session, and the fingerprint and cookie jar
    are one identity to it — discard both together and move to the next
    profile, never swapping the fingerprint in place on a session it has
    already seen."""
    global _profile_idx
    async with _rotate_lock:
        if _ident is not ident:
            return  # another request already burned this identity
        _profile_idx = (_profile_idx + 1) % len(PROFILES)
        _retire(ident, "403")
        log.warning("bahn.de blocked impersonate=%s: discarded the session, "
                    "next identity uses %s", ident.profile, PROFILES[_profile_idx])


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


def _note_upstream(source: str) -> None:
    """Count one request actually put on the wire to bahn.de and roll the tally into
    the log every UPSTREAM_LOG_EVERY calls.

    Counted here, at the one point requests are issued, so everything that spares
    bahn.de a call — cache hits, coalesced waiters, circuit-rejected calls, stale
    fallbacks — is correctly absent from the total. `source` attributes the call to
    what wanted it: a user's own search, the if-missed enrichment, a past-mode walk
    replan, or autocomplete falling past the local station index.
    """
    global _upstream_logged_at, _upstream_logged_429
    metrics["upstream_requests"] += 1
    metrics[f"upstream_from_{source}"] += 1
    _upstream_since[source] += 1
    total = sum(_upstream_since.values())
    if total < UPSTREAM_LOG_EVERY:
        return
    now = time.monotonic()
    span = max(now - _upstream_logged_at, 1e-6)
    refused = metrics["upstream_429"] - _upstream_logged_429
    _upstream_logged_at, _upstream_logged_429 = now, metrics["upstream_429"]
    breakdown = " ".join(f"{k}={v}" for k, v in sorted(_upstream_since.items()))
    _upstream_since.clear()
    log.info("bahn.de upstream: %d calls in %.0fs (%.1f/min) %s, 429s=%d",
             total, span, 60 * total / span, breakdown, refused)


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


async def _request(method: str, path: str, *, source: str, **kwargs) -> dict:
    """One upstream call under the circuit breaker: fails fast while the
    circuit is open, otherwise issues the request and feeds the outcome back.
    `source` is what asked for it — see _note_upstream."""
    probe = _breaker.acquire()
    try:
        data = await _issue(method, path, source, **kwargs)
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


async def _issue(method: str, path: str, source: str, **kwargs) -> dict:
    """Issue one request to bahn.de, burning the whole session identity
    (profile + cookies) on a 403, and translate every failure mode into the
    UpstreamError taxonomy. Retries are bounded and never loop: a
    single same-session wait when a 429 carries a Retry-After of at most
    QUICK_RETRY_MAX_WAIT seconds, and up to NET_RETRIES reconnects when the
    connection drops mid-request."""
    rotations = 0
    quick_retried = False
    net_retries = 0
    reconnected = False  # a transport retry whose outcome is not yet known
    while True:
        # counted per loop pass, not per call: a 403 burn and a quick 429 retry
        # each put another request on the wire, which is what we are tracking
        _note_upstream(source)
        try:
            async with _upstream_sem:
                # acquired inside the semaphore so a task parked on a slot can
                # never hold a reference to an identity burned under it
                ident = _acquire_identity()
                resp = await getattr(ident.session, method)(f"{BASE_URL}{path}", **kwargs)
        except RequestException as e:
            # DNS/connect failures and timeouts land here
            metrics["upstream_network_errors"] += 1
            log.warning("bahn.de unreachable: profile=%s path=%s error=%s",
                        ident.profile, path, type(e).__name__)
            if net_retries < NET_RETRIES and isinstance(e, RETRYABLE_NETWORK_ERRORS):
                # sleep outside the semaphore so a retry never holds a slot idle
                net_retries += 1
                reconnected = True
                await asyncio.sleep(NET_RETRY_BACKOFF * net_retries + random.uniform(0, 0.25))
                continue
            raise UpstreamUnavailable(f"bahn.de unreachable: {type(e).__name__}") from e

        if reconnected:
            # counted apart from upstream_network_errors, which stays a tally of
            # what happened on the wire: this is the subset the user never saw
            reconnected = False
            metrics["upstream_network_recovered"] += 1
            log.info("bahn.de reachable again after %d reconnect(s): path=%s", net_retries, path)

        status = resp.status_code
        if status == 403:
            metrics["upstream_403"] += 1
            rotations += 1
            if rotations < len(PROFILES):
                await _burn(ident)
                continue
            raise UpstreamBlocked("bahn.de blocked every impersonation profile")
        if status == 429:
            metrics["upstream_429"] += 1
            retry_after = _retry_after_seconds(resp)
            _note_429(ident.profile, path, retry_after)
            if not quick_retried and retry_after is not None and 0 < retry_after <= QUICK_RETRY_MAX_WAIT:
                # honor a short advertised wait once, on the same session —
                # never a retry loop, never a fresh identity
                quick_retried = True
                await asyncio.sleep(retry_after + random.uniform(0, 0.5))
                continue
            raise UpstreamRateLimited("bahn.de rate limit", retry_after=retry_after)
        if status >= 500:
            metrics["upstream_5xx"] += 1
            log.warning("bahn.de %d: path=%s profile=%s", status, path, ident.profile)
            raise UpstreamUnavailable(f"bahn.de responded {status}")
        if status >= 400:
            metrics["upstream_4xx"] += 1
            log.warning("bahn.de %d: path=%s profile=%s", status, path, ident.profile)
            raise UpstreamProtocolError(f"bahn.de responded {status}")
        try:
            data = resp.json()
        except Exception as e:
            metrics["upstream_malformed"] += 1
            log.warning("bahn.de malformed response: path=%s profile=%s error=%s",
                        path, ident.profile, type(e).__name__)
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
    global _ident
    # retiring sessions must not outlive shutdown: take over their close and
    # cancel the pending timers so nothing fires into a closing loop
    for task in list(_close_tasks):
        task.cancel()
    _close_tasks.clear()
    sessions = list(_retiring)
    _retiring.clear()
    if _ident is not None:
        sessions.append(_ident.session)
        _ident = None
    for session in sessions:
        try:
            await session.close()
        except Exception:
            pass  # a straggling _close_later may have beaten us to it


async def locations(query: str) -> list[dict]:
    # shield: the fetch task is shared with concurrent identical callers, so
    # one waiter's disconnect must not cancel it for everyone else
    return await asyncio.shield(_cached(
        ("locations", query.strip().lower()),
        LOCATIONS_TTL,
        lambda: _request("get", "/reiseloesung/orte", source="locations",
                         params={"suchbegriff": query, "typ": "ALL", "limit": 8}),
    ))


def _departure_skew(a: str, b: str) -> float:
    """Seconds between two departure timestamps; inf when either is unparsable,
    so an unusable value can never qualify as a near-enough fallback."""
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds())
    except ValueError:
        return float("inf")


# Age brackets of the bahn.de search mask, as the vendo API spells them: the
# mask files 6-14-year-olds under FAMILIENKIND (not KIND) and 0-5 under
# KLEINKIND — ids 11 and 8 in /angebote/stammdaten, which is also where the
# other three come from.
TRAVELLER_TYPES = {
    "adult": "ERWACHSENER",       # 27-64
    "senior": "SENIOR",           # 65+
    "young": "JUGENDLICHER",      # 15-26
    "child": "FAMILIENKIND",      # 6-14
    "toddler": "KLEINKIND",       # 0-5
}


async def journeys(from_id: str, to_id: str, departure_iso: str, paging_ref: str | None = None,
                   dticket: str = "off", age: str = "adult", source: str = "search") -> tuple[dict, int]:
    """Returns (data, stale_age_seconds); age is 0 for a fresh answer, else how
    old the served fallback is. from_id/to_id are full HAFAS location ids
    (A=1@O=...@L=...@) from locations().

    paging_ref is a verbindungReference.earlier/later token from a previous response;
    when set, the API returns the adjacent result page instead of the requested time.

    dticket mirrors the two bahn.de search-mask toggles: "only" restricts results to
    Deutschland-Ticket-valid connections, "all" keeps every connection but tells
    bahn.de the passenger holds the ticket — covered connections then come back with
    an MDA-NUR-DT meldung instead of a price, and mixed ones repriced for the paid
    legs only. "off" is a search without the ticket.

    age is the traveler's bracket (a TRAVELLER_TYPES key); bahn.de prices every
    connection for that one traveler, the way its own search mask does.
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
            "typ": TRAVELLER_TYPES[age],
            "ermaessigungen": [{"art": "KEINE_ERMAESSIGUNG", "klasse": "KLASSENLOS"}],
            "alter": [],
            "anzahl": 1,
        }],
        # bahn.de's "prefer fast connections" drops the slower regional options —
        # the free-with-the-ticket ones — from the list, which would leave the
        # "all trains" mode showing nothing the D-Ticket covers on exactly the
        # routes where it pays off (München -> Augsburg: 5 paid ICEs, 0 covered).
        # Routes without a slower covered alternative return the same list either way.
        "schnelleVerbindungen": dticket != "all",
        "sitzplatzOnly": False,
        "bikeCarriage": False,
        "reservierungsKontingenteVorhanden": False,
        "deutschlandTicketVorhanden": dticket != "off",
        "nurDeutschlandTicketVerbindungen": dticket == "only",
    }
    if paging_ref:
        body["pagingReference"] = paging_ref
    key = ("journeys", from_id, to_id, departure_iso, paging_ref, dticket, age)
    # paged responses are offsets into a result list, so they only ever stand in
    # for the same page (exact key), never for the route's primary answer
    route = (from_id, to_id, dticket, age) if paging_ref is None else None

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
            lambda: _request("post", "/angebote/fahrplan", source=source, json=body),
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
