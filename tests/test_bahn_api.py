"""Cache, stale fallback, single-flight, 429 handling and failure taxonomy —
all against a scripted fake session, never live bahn.de."""

import asyncio
import time as real_time
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import RequestException, Timeout

from app import bahn_api
from tests.conftest import FakeResponse

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


async def test_malformed_response_maps_to_protocol_error(bahn):
    bahn.use(FakeResponse(200, payload=ValueError("not json")))
    with pytest.raises(bahn_api.UpstreamProtocolError):
        await search()
    assert bahn_api.metrics["upstream_malformed"] == 1


# --- 403 profile rotation (pre-existing behavior kept) ---


async def test_403_rotates_profile_then_succeeds(bahn):
    sess = bahn.use(FakeResponse(403), FakeResponse(payload=PAYLOAD))
    assert await search() == (PAYLOAD, 0)
    assert sess.calls == 2
    assert bahn_api._profile_idx == 1
    assert bahn_api.metrics["upstream_403"] == 1


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
