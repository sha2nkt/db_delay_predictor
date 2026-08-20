"""Minimum transfer time (bahn.de's minUmstiegszeit).

The value is an absolute floor on every transfer, not an addition to the
station's default — probed against the live API, see bahn_api.journeys. The
tests below only guard our side of it: that it reaches the request body, that
it is part of the cache identity, and that a buffered search is never answered
from an unbuffered one.
"""

from types import SimpleNamespace

import pytest

from app import bahn_api, delays, main
from tests.conftest import FakeResponse

pytestmark = pytest.mark.anyio

PAYLOAD = {"verbindungen": [{"tripId": "demo"}]}
BERLIN = "A=1@O=Berlin@L=8011160@"
MUNICH = "A=1@O=Muenchen@L=8000261@"


async def search(transfer_time=0, departure="2026-08-13T10:00:00"):
    return await bahn_api.journeys(BERLIN, MUNICH, departure, transfer_time=transfer_time)


class DummyRequest:
    headers: dict[str, str] = {}
    client = SimpleNamespace(host="203.0.113.9")


class DummyResponse:
    def __init__(self):
        self.headers: dict[str, str] = {}


# --- upstream client ---


async def test_transfer_time_reaches_the_request_body(bahn):
    sent = {}

    class Recorder(FakeResponse):
        pass

    sess = bahn.use(Recorder(payload=PAYLOAD))
    original = sess.post

    async def capture(url, **kwargs):
        sent.update(kwargs.get("json") or {})
        return await original(url, **kwargs)

    sess.post = capture
    await search(transfer_time=20)
    assert sent["minUmstiegszeit"] == 20


async def test_default_is_no_minimum(bahn):
    sent = {}
    sess = bahn.use(FakeResponse(payload=PAYLOAD))
    original = sess.post

    async def capture(url, **kwargs):
        sent.update(kwargs.get("json") or {})
        return await original(url, **kwargs)

    sess.post = capture
    await search()
    assert sent["minUmstiegszeit"] == 0


async def test_different_buffers_are_different_cache_entries(bahn):
    sess = bahn.use(FakeResponse(payload=PAYLOAD))
    await search(transfer_time=0)
    await search(transfer_time=30)
    assert sess.calls == 2
    # ...and the same buffer still coalesces
    await search(transfer_time=30)
    assert sess.calls == 2


async def test_unbuffered_answer_is_never_served_as_a_buffered_one(bahn):
    """The route-level stale fallback answers a nearby departure of the same
    route. It must not cross buffers: an unfiltered list is full of exactly the
    tight connections the buffered search asked to exclude."""
    bahn.use(FakeResponse(payload=PAYLOAD))
    await search(transfer_time=0)  # remembered as a fallback

    bahn.use(FakeResponse(status_code=503))
    with pytest.raises(bahn_api.UpstreamError):
        # same route, one bucket later, but a 30-minute buffer
        await search(transfer_time=30, departure="2026-08-13T10:20:00")

    # the unbuffered search still gets its own fallback
    _, age = await search(transfer_time=0, departure="2026-08-13T10:20:00")
    assert age >= 0


# --- endpoint ---


async def test_endpoint_rejects_a_value_outside_the_offered_set():
    with pytest.raises(main.HTTPException) as exc:
        await main.journeys(
            DummyRequest(), DummyResponse(), from_id=BERLIN, to_id=MUNICH,
            departure="2026-08-13T10:00:00", window=7, paging_ref=None,
            mode="future", dticket=False, transfer_time=7,
        )
    assert exc.value.status_code == 422


async def test_past_mode_ignores_the_buffer(monkeypatch):
    """The compensation check reconstructs a journey that was already taken;
    filtering it could hide the very itinerary being checked."""
    seen = {}

    async def fake_journeys(from_id, to_id, departure, paging_ref=None,
                            dticket=False, transfer_time=0):
        seen["transfer_time"] = transfer_time
        return {"verbindungen": []}, 0

    monkeypatch.setattr(bahn_api, "journeys", fake_journeys)
    monkeypatch.setattr(delays, "coverage", lambda: (None, None))
    await main.journeys(
        DummyRequest(), DummyResponse(), from_id=BERLIN, to_id=MUNICH,
        departure="2026-08-13T10:00:00", window=7, paging_ref=None,
        mode="past", dticket=False, transfer_time=30,
    )
    assert seen["transfer_time"] == 0


async def test_future_mode_passes_the_buffer_through(monkeypatch):
    seen = {}

    async def fake_journeys(from_id, to_id, departure, paging_ref=None,
                            dticket=False, transfer_time=0):
        seen["transfer_time"] = transfer_time
        return {"verbindungen": []}, 0

    monkeypatch.setattr(bahn_api, "journeys", fake_journeys)
    monkeypatch.setattr(delays, "coverage", lambda: (None, None))
    await main.journeys(
        DummyRequest(), DummyResponse(), from_id=BERLIN, to_id=MUNICH,
        departure="2026-08-13T10:00:00", window=7, paging_ref=None,
        mode="future", dticket=False, transfer_time=30,
    )
    assert seen["transfer_time"] == 30
