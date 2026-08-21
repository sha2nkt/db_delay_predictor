"""Cache, stale fallback, single-flight, 429 handling and failure taxonomy —
all against a scripted fake session, never live bahn.de."""

import asyncio
import time as real_time
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import RequestException, Timeout

from app import bahn_api
from tests.conftest import FakeResponse, FakeSession

pytestmark = pytest.mark.anyio

PAYLOAD = {"verbindungen": [{"tripId": "demo"}]}


async def search(departure="2026-08-13T10:00:00", to="A=1@O=Muenchen@L=8000261@"):
    return await bahn_api.journeys("A=1@O=Berlin@L=8011160@", to, departure)


# --- cache ---


async def test_cache_miss_hits_upstream_once_then_serves_fresh(bahn):
    sess = bahn.use(FakeResponse(payload=PAYLOAD))
    data, age = await search()
    assert (data, age) == (PAYLOAD, 0)
    assert sess.calls == 1
    data, age = await search()
    assert (data, age) == (PAYLOAD, 0)
    assert sess.calls == 1  # fresh hit, no new upstream request
    assert bahn_api.metrics["cache_hits"] == 1
    assert bahn_api.metrics["cache_misses"] == 1


async def test_cache_expiry_refetches(bahn):
    sess = bahn.use(FakeResponse(payload=PAYLOAD))
    await search()
    bahn.clock.advance(bahn_api.JOURNEYS_TTL + 1)
    await search()
    assert sess.calls == 2


async def test_different_searches_stay_independent(bahn):
    sess = bahn.use(FakeResponse(payload=PAYLOAD))
    await search()
    await search(to="A=1@O=Hamburg@L=8002549@")
    assert sess.calls == 2


async def test_departure_minutes_bucketed_into_one_key(bahn):
    sess = bahn.use(FakeResponse(payload=PAYLOAD))
    await search(departure="2026-08-13T10:03:00")
    await search(departure="2026-08-13T10:04:59")
    assert sess.calls == 1


async def test_dticket_modes_send_their_own_flags_and_never_share_a_cache_entry(bahn):
    sess = bahn.use(FakeResponse(payload=PAYLOAD))
    bodies = []
    post = sess.post
    sess.post = lambda url, **kw: (bodies.append(kw.get("json")), post(url, **kw))[1]

    for mode in ("off", "all", "only"):
        await bahn_api.journeys("A=1@O=Berlin@L=8011160@", "A=1@O=Muenchen@L=8000261@",
                                "2026-08-13T10:00:00", dticket=mode)
    assert sess.calls == 3  # one cache entry per mode, never a shared answer
    flags = [(b["deutschlandTicketVorhanden"], b["nurDeutschlandTicketVerbindungen"],
              b["schnelleVerbindungen"]) for b in bodies]
    # "all" declares the ticket without filtering, and drops the fast-connection
    # preference that would hide every connection the ticket covers
    assert flags == [(False, False, True), (True, False, False), (True, True, True)]


# --- stale fallback ---


async def test_stale_served_when_rate_limited(bahn):
    bahn.use(FakeResponse(payload=PAYLOAD))
    await search()
    bahn.clock.advance(400)  # fresh TTL over, stale window not
    bahn.use(FakeResponse(429))
    data, age = await search()
    assert data == PAYLOAD
    assert age == 400
    assert bahn_api.metrics["stale_hits"] == 1


async def test_stale_not_served_after_stale_ttl(bahn):
    bahn.use(FakeResponse(payload=PAYLOAD))
    await search()
    bahn.clock.advance(bahn_api.STALE_TTL + 100)
    bahn.use(FakeResponse(429))
    with pytest.raises(bahn_api.UpstreamRateLimited):
        await search()
    assert bahn_api.metrics["stale_misses"] == 1


async def test_stale_served_while_circuit_open_without_upstream_call(bahn):
    sess = bahn.use(FakeResponse(payload=PAYLOAD))
    await search()
    bahn.clock.advance(400)
    bahn_api._breaker.force_open(30)
    data, age = await search()
    assert data == PAYLOAD and age == 400
    assert sess.calls == 1  # the open circuit kept bahn.de untouched
    assert bahn_api.metrics["circuit_rejected"] == 1


async def test_route_level_stale_survives_bucket_rollover_same_day_only(bahn):
    bahn.use(FakeResponse(payload=PAYLOAD))
    await search(departure="2026-08-13T10:00:00")
    bahn.clock.advance(400)
    bahn.use(FakeResponse(429))
    # same route and day, newer 5-min bucket: the route index answers
    data, age = await search(departure="2026-08-13T10:30:00")
    assert data == PAYLOAD and age == 400
    assert bahn_api.metrics["stale_hits_route"] == 1
    # another travel day must never be answered by the route entry
    with pytest.raises(bahn_api.UpstreamRateLimited):
        await search(departure="2026-08-14T10:00:00")


async def test_route_level_stale_not_served_for_another_time_of_day(bahn):
    """A 22:00 search answered from a 17:00 one reads as the site ignoring the
    entered time — the reason round trips showed afternoon return connections."""
    bahn.use(FakeResponse(payload=PAYLOAD))
    await search(departure="2026-08-13T17:00:00")
    bahn.clock.advance(400)
    bahn.use(FakeResponse(429))
    with pytest.raises(bahn_api.UpstreamRateLimited):
        await search(departure="2026-08-13T22:00:00")
    assert bahn_api.metrics["stale_hits_route"] == 0


# --- single-flight ---


async def test_concurrent_identical_searches_share_one_upstream_call(bahn):
    gate = asyncio.Event()

    async def slow_response():
        await gate.wait()
        return FakeResponse(payload=PAYLOAD)

    sess = bahn.use(slow_response)
    tasks = [asyncio.create_task(search()) for _ in range(30)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert sess.calls == 1
    assert all(r == (PAYLOAD, 0) for r in results)
    assert bahn_api.metrics["cache_coalesced"] == 29


async def test_one_waiter_cancelling_keeps_shared_fetch_alive(bahn):
    gate = asyncio.Event()

    async def slow_response():
        await gate.wait()
        return FakeResponse(payload=PAYLOAD)

    sess = bahn.use(slow_response)
    first = asyncio.create_task(search())
    second = asyncio.create_task(search())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.cancel()
    gate.set()
    assert await second == (PAYLOAD, 0)
    assert sess.calls == 1


async def test_failure_not_cached(bahn):
    sess = bahn.use(FakeResponse(500), FakeResponse(payload=PAYLOAD))
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert await search() == (PAYLOAD, 0)
    assert sess.calls == 2


# --- 429 handling ---


async def test_429_retry_after_seconds_opens_circuit(bahn):
    sess = bahn.use(FakeResponse(429, headers={"Retry-After": "120"}))
    with pytest.raises(bahn_api.UpstreamRateLimited) as exc:
        await search()
    assert exc.value.retry_after == 120
    assert sess.calls == 1
    # the explicit Retry-After is authoritative: circuit open at least that long
    assert bahn_api._breaker.state == "open"
    assert bahn_api._breaker.snapshot()["retryIn"] >= 120
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert sess.calls == 1  # rejected without touching bahn.de


async def test_429_http_date_retry_after_parsed(bahn):
    header = formatdate(real_time.time() + 90, usegmt=True)
    bahn.use(FakeResponse(429, headers={"Retry-After": header}))
    with pytest.raises(bahn_api.UpstreamRateLimited) as exc:
        await search()
    assert 80 <= exc.value.retry_after <= 91


async def test_429_without_retry_after_fails_once_no_retry_loop(bahn):
    sess = bahn.use(FakeResponse(429))
    with pytest.raises(bahn_api.UpstreamRateLimited) as exc:
        await search()
    assert exc.value.retry_after is None
    assert sess.calls == 1  # no rotation, no session swap, no retry storm
    assert bahn_api._profile_idx == 0
    assert bahn_api.metrics["session_recycled"] == 0  # a 429 never burns the identity
    assert bahn_api._breaker.state == "closed"  # one 429 is below the threshold


async def test_429_short_retry_after_grants_one_quick_retry(bahn, monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(bahn_api, "asyncio", SimpleNamespace(
        sleep=fake_sleep, ensure_future=asyncio.ensure_future, shield=asyncio.shield))
    sess = bahn.use(FakeResponse(429, headers={"Retry-After": "1"}),
                    FakeResponse(payload=PAYLOAD))
    assert await search() == (PAYLOAD, 0)
    assert sess.calls == 2
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 1.5  # honored wait + jitter

    # different route so the first search's stale entry can't answer for it
    sess = bahn.use(FakeResponse(429, headers={"Retry-After": "1"}))
    with pytest.raises(bahn_api.UpstreamRateLimited):
        await search(to="A=1@O=Hamburg@L=8002549@")
    assert sess.calls == 2  # exactly one quick retry, never a loop


async def test_repeated_429s_open_circuit(bahn):
    sess = bahn.use(FakeResponse(429))
    for _ in range(bahn_api.CIRCUIT_FAILURE_THRESHOLD):
        with pytest.raises(bahn_api.UpstreamRateLimited):
            await search()
    assert bahn_api._breaker.state == "open"
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert sess.calls == bahn_api.CIRCUIT_FAILURE_THRESHOLD
    assert bahn_api.metrics["upstream_429"] == bahn_api.CIRCUIT_FAILURE_THRESHOLD


# --- other failure classes ---


async def test_5xx_maps_to_unavailable(bahn):
    bahn.use(FakeResponse(503))
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert bahn_api.metrics["upstream_5xx"] == 1


async def test_network_failure_maps_to_unavailable(bahn):
    bahn.use(RequestException("connection refused"))
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert bahn_api.metrics["upstream_network_errors"] == 1


async def test_timeout_maps_to_unavailable(bahn):
    bahn.use(Timeout("timed out"))
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert bahn_api.metrics["upstream_network_errors"] == 1


# --- transport retry: a connection dropped mid-request is a blip on the wire,
#     not a verdict on bahn.de, so it gets one more attempt before the user
#     sees a 503 ---


def _capture_sleeps(bahn_module, monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(bahn_module, "asyncio", SimpleNamespace(
        sleep=fake_sleep, ensure_future=asyncio.ensure_future, shield=asyncio.shield))
    return sleeps


async def test_dropped_connection_reconnects_once_then_succeeds(bahn, monkeypatch):
    sleeps = _capture_sleeps(bahn_api, monkeypatch)
    sess = bahn.use(CurlConnectionError("connection reset by peer"),
                    FakeResponse(payload=PAYLOAD))
    assert await search() == (PAYLOAD, 0)
    assert sess.calls == 2
    assert len(sleeps) == 1 and 0.25 <= sleeps[0] <= 0.5  # backoff + jitter
    # the wire failure is still recorded; the point is the user never saw it
    assert bahn_api.metrics["upstream_network_errors"] == 1
    assert bahn_api.metrics["upstream_network_recovered"] == 1
    # a blip that recovered is not evidence against bahn.de
    assert bahn_api._breaker.state == "closed"


async def test_reconnect_is_bounded_never_a_loop(bahn, monkeypatch):
    _capture_sleeps(bahn_api, monkeypatch)
    sess = bahn.use(CurlConnectionError("connection reset by peer"))
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert sess.calls == bahn_api.NET_RETRIES + 1
    assert bahn_api.metrics["upstream_network_errors"] == bahn_api.NET_RETRIES + 1
    assert bahn_api.metrics["upstream_network_recovered"] == 0


async def test_timeout_is_never_retried(bahn, monkeypatch):
    """bahn.de did answer the connect and then took too long; trying again only
    doubles the user's wait, so the timeout path must stay single-shot."""
    sleeps = _capture_sleeps(bahn_api, monkeypatch)
    sess = bahn.use(Timeout("timed out"), FakeResponse(payload=PAYLOAD))
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert sess.calls == 1
    assert sleeps == []


async def test_unclassified_request_exception_is_not_retried(bahn, monkeypatch):
    """Only connection-level classes are retried; the base class stays fail-fast."""
    _capture_sleeps(bahn_api, monkeypatch)
    sess = bahn.use(RequestException("something else"), FakeResponse(payload=PAYLOAD))
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert sess.calls == 1


async def test_malformed_response_maps_to_protocol_error(bahn):
    bahn.use(FakeResponse(200, payload=ValueError("not json")))
    with pytest.raises(bahn_api.UpstreamProtocolError):
        await search()
    assert bahn_api.metrics["upstream_malformed"] == 1


# --- 403: burn the identity (session + cookies), advance the profile ---


async def test_403_rotates_profile_then_succeeds(bahn):
    sess = bahn.use(FakeResponse(403), FakeResponse(payload=PAYLOAD))
    assert await search() == (PAYLOAD, 0)
    assert sess.calls == 2
    assert bahn_api._profile_idx == 1
    assert bahn_api.metrics["upstream_403"] == 1
    assert bahn_api.metrics["session_recycled"] == 1  # the whole identity was burned


async def test_all_profiles_403_forces_circuit_open_then_recovers(bahn):
    sess = bahn.use(FakeResponse(403), FakeResponse(403), FakeResponse(403),
                    FakeResponse(payload=PAYLOAD))
    with pytest.raises(bahn_api.UpstreamBlocked):
        await search()
    assert sess.calls == 3
    with pytest.raises(bahn_api.UpstreamUnavailable):
        await search()
    assert sess.calls == 3  # fail-fast while blocked
    bahn.clock.advance(31)
    assert await search() == (PAYLOAD, 0)  # half-open probe succeeded
    assert bahn_api._breaker.state == "closed"


# --- session recycling: one identity = one profile + one cookie jar + one
#     connection, capped by request budget and age ---


async def test_session_recycled_after_request_budget(bahn, monkeypatch):
    monkeypatch.setattr(bahn_api, "SESSION_MIN_REQUESTS", 2)
    monkeypatch.setattr(bahn_api, "SESSION_MAX_REQUESTS", 2)
    fakes = []

    def factory(profile):
        fakes.append(FakeSession(FakeResponse(payload=PAYLOAD)))
        return fakes[-1]

    bahn.factory = factory
    for city in ("Muenchen@L=8000261@", "Hamburg@L=8002549@", "Koeln@L=8000207@"):
        await search(to=f"A=1@O={city}")
    assert len(fakes) == 2  # third request spent the budget, drew a new session
    assert fakes[0].calls == 2 and fakes[1].calls == 1
    assert bahn_api.metrics["session_recycled"] == 1
    assert fakes[0] in bahn_api._retiring
    assert bahn_api._profile_idx == 0  # benign recycle keeps the profile


async def test_session_recycled_after_max_age(bahn):
    fakes = []

    def factory(profile):
        fakes.append(FakeSession(FakeResponse(payload=PAYLOAD)))
        return fakes[-1]

    bahn.factory = factory
    await search()
    bahn.clock.advance(bahn_api.SESSION_MAX_AGE + 1)
    await search(to="A=1@O=Hamburg@L=8002549@")
    assert len(fakes) == 2
    assert bahn_api.metrics["session_recycled"] == 1


async def test_403_burns_session_new_factory_call(bahn):
    scripts = [[FakeResponse(403)], [FakeResponse(payload=PAYLOAD)]]
    fakes = []

    def factory(profile):
        fakes.append(FakeSession(*scripts[len(fakes)]))
        return fakes[-1]

    bahn.factory = factory
    assert await search() == (PAYLOAD, 0)
    assert len(fakes) == 2  # the retry ran on a fresh session, not the flagged one
    assert fakes[0].calls == 1 and fakes[1].calls == 1
    assert fakes[0] in bahn_api._retiring
    assert bahn_api._profile_idx == 1
    assert bahn_api.metrics["session_recycled"] == 1


async def test_429_does_not_swap_session(bahn):
    sess = bahn.use(FakeResponse(429))
    with pytest.raises(bahn_api.UpstreamRateLimited):
        await search()
    assert bahn_api._ident is not None and bahn_api._ident.session is sess
    assert bahn_api._retiring == []
    assert bahn_api.metrics["session_recycled"] == 0


async def test_deferred_close_waits_then_closes(bahn, monkeypatch):
    sleeps = _capture_sleeps(bahn_api, monkeypatch)
    monkeypatch.setattr(bahn_api, "SESSION_MIN_REQUESTS", 1)
    monkeypatch.setattr(bahn_api, "SESSION_MAX_REQUESTS", 1)
    fakes = []

    def factory(profile):
        fakes.append(FakeSession(FakeResponse(payload=PAYLOAD)))
        return fakes[-1]

    bahn.factory = factory
    await search()
    await search(to="A=1@O=Hamburg@L=8002549@")  # budget spent: retires fakes[0]
    for _ in range(3):  # let the (stubbed-sleep) close timer run
        await asyncio.sleep(0)
    assert bahn_api.SESSION_CLOSE_DELAY in sleeps  # waited out any in-flight request
    assert fakes[0].closed is True
    assert fakes[0] not in bahn_api._retiring


async def test_retired_session_without_close_is_tolerated(bahn, monkeypatch):
    _capture_sleeps(bahn_api, monkeypatch)
    monkeypatch.setattr(bahn_api, "SESSION_MIN_REQUESTS", 1)
    monkeypatch.setattr(bahn_api, "SESSION_MAX_REQUESTS", 1)

    class NoCloseSession:
        async def post(self, url, **kwargs):
            return FakeResponse(payload=PAYLOAD)

        async def get(self, url, **kwargs):
            return FakeResponse(payload=PAYLOAD)

    bahn.factory = lambda profile: NoCloseSession()
    await search()
    await search(to="A=1@O=Hamburg@L=8002549@")
    for _ in range(3):
        await asyncio.sleep(0)
    assert bahn_api._retiring == []  # drained without an AttributeError


async def test_close_closes_current_and_retiring(bahn, monkeypatch):
    monkeypatch.setattr(bahn_api, "SESSION_MIN_REQUESTS", 1)
    monkeypatch.setattr(bahn_api, "SESSION_MAX_REQUESTS", 1)
    fakes = []

    def factory(profile):
        fakes.append(FakeSession(FakeResponse(payload=PAYLOAD)))
        return fakes[-1]

    bahn.factory = factory
    await search()
    await search(to="A=1@O=Hamburg@L=8002549@")  # fakes[0] retiring, timer pending
    await bahn_api.close()
    assert fakes[0].closed and fakes[1].closed
    assert bahn_api._retiring == [] and bahn_api._ident is None
    assert bahn_api._close_tasks == set()  # no timer left to fire into a dead loop


# --- Retry-After parsing (sync) ---


def test_retry_after_parsing_forms():
    parse = bahn_api._retry_after_seconds
    assert parse(FakeResponse(429, headers={"Retry-After": "120"})) == 120.0
    assert parse(FakeResponse(429, headers={"Retry-After": "0"})) == 0.0
    assert parse(FakeResponse(429, headers={"Retry-After": "garbage"})) is None
    assert parse(FakeResponse(429)) is None
    future = formatdate(real_time.time() + 60, usegmt=True)
    assert 55 <= parse(FakeResponse(429, headers={"Retry-After": future})) <= 61
    past = formatdate(real_time.time() - 60, usegmt=True)
    assert parse(FakeResponse(429, headers={"Retry-After": past})) == 0.0


# --- upstream volume logging ---


async def test_upstream_calls_are_attributed_to_their_source(bahn):
    bahn.use(FakeResponse(payload=PAYLOAD))
    await search()                                   # defaults to source="search"
    await bahn_api.journeys("A=1@O=Berlin@L=8011160@", "A=1@O=Koeln@L=8000207@",
                            "2026-08-13T11:00:00", source="if-missed")
    assert bahn_api.metrics["upstream_from_search"] == 1
    assert bahn_api.metrics["upstream_from_if-missed"] == 1
    assert bahn_api.metrics["upstream_requests"] == 2


async def test_cache_hits_are_absent_from_the_upstream_count(bahn):
    bahn.use(FakeResponse(payload=PAYLOAD))
    await search()
    await search()  # served from cache
    assert bahn_api.metrics["upstream_requests"] == 1
    assert bahn_api.metrics["upstream_from_search"] == 1


async def test_upstream_rollup_logs_rate_and_breakdown(bahn, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(bahn_api, "UPSTREAM_LOG_EVERY", 3)
    bahn.use(FakeResponse(payload=PAYLOAD))
    with caplog.at_level(logging.INFO, logger="app.bahn_api"):
        for i in range(3):
            bahn.clock.advance(20)  # 3 calls over 60s -> 3.0/min
            await bahn_api.journeys("A=1@O=Berlin@L=8011160@", "A=1@O=Koeln@L=8000207@",
                                    f"2026-08-13T1{i}:00:00",
                                    source="search" if i else "if-missed")
    lines = [r.getMessage() for r in caplog.records if "bahn.de upstream:" in r.getMessage()]
    assert len(lines) == 1
    assert "3 calls in 60s (3.0/min)" in lines[0]
    assert "if-missed=1 search=2" in lines[0]
    assert "429s=0" in lines[0]
    assert not bahn_api._upstream_since  # the interval resets for the next line
