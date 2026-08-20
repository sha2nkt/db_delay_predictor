"""Endpoint behavior when bahn.de is straining: autocomplete degrades quietly,
and the optional if-missed replans stop spending upstream budget."""

import pytest
from fastapi import HTTPException

from app import bahn_api, delays, main, ratelimit

pytestmark = pytest.mark.anyio


class DummyResponse:
    """Stands in for FastAPI's Response, which only the headers are used from."""

    def __init__(self):
        self.headers: dict[str, str] = {}


async def test_locations_degrade_to_empty_instead_of_erroring(monkeypatch):
    monkeypatch.setattr(delays, "station_search", lambda q: [])

    async def refuse(query):
        raise bahn_api.UpstreamRateLimited("rate limited", retry_after=45)

    monkeypatch.setattr(bahn_api, "locations", refuse)
    response = DummyResponse()
    # a typo mid-search must not paint the mask red: no exception, no results
    assert await main.locations("Dormtund", response) == []
    assert response.headers["Cache-Control"] == "no-store"


async def test_locations_prefer_local_index_without_touching_upstream(monkeypatch):
    monkeypatch.setattr(delays, "station_search", lambda q: [{"id": "1", "name": "Dortmund Hbf"}])

    async def explode(query):
        raise AssertionError("upstream must not be called when the index answers")

    monkeypatch.setattr(bahn_api, "locations", explode)
    assert await main.locations("Dortmund", DummyResponse())


# what bahn.de answers around Tübingen Hbf: bus stops first, the station itself
# further out, and two municipal stops it labels REGIONAL anyway
_NEARBY_TUEBINGEN = [
    {"id": "A=1@L=422914@", "extId": "422914", "name": "Nonnenhaus, Tübingen",
     "lat": 48.52218, "lon": 9.057468, "products": ["BUS"]},
    {"id": "A=1@L=752100@", "extId": "752100", "name": "Hauptbahnhof, Tübingen",
     "lat": 48.516705, "lon": 9.056003, "products": ["REGIONAL", "BUS", "ANRUFPFLICHTIG"]},
    {"id": "A=1@L=8000141@", "extId": "8000141", "name": "Tübingen Hbf",
     "lat": 48.515663, "lon": 9.056003, "products": ["REGIONAL"]},
]


async def test_nearby_prefers_a_station_we_hold_data_for(monkeypatch):
    async def answer(lat, lon):
        return _NEARBY_TUEBINGEN

    monkeypatch.setattr(bahn_api, "nearby", answer)
    monkeypatch.setattr(delays, "has_delay_data", lambda ext: ext == "8000141")
    found = await main.locations_nearby(
        _fake_request("198.51.100.11"), DummyResponse(), lat=48.5216, lon=9.0576)
    # the bus stops are gone, and the closer REGIONAL-labelled municipal stop
    # loses to the station our statistics can actually speak about
    assert [s["extId"] for s in found] == ["8000141", "752100"]


async def test_nearby_reports_an_upstream_failure(monkeypatch):
    async def refuse(lat, lon):
        raise bahn_api.UpstreamRateLimited("rate limited", retry_after=45)

    monkeypatch.setattr(bahn_api, "nearby", refuse)
    # a deliberate tap deserves an error, not a silent "no station near you"
    with pytest.raises(HTTPException) as exc:
        await main.locations_nearby(
            _fake_request("198.51.100.12"), DummyResponse(), lat=48.5216, lon=9.0576)
    assert exc.value.status_code == 503
    # asserted on the exception, not the injected response: that one is dropped
    # the moment an exception propagates, so headers set on it never ship
    assert exc.value.headers["Cache-Control"] == "no-store"
    assert exc.value.headers["Retry-After"]


async def test_nearby_answers_outside_our_region_without_calling_upstream(monkeypatch):
    async def explode(lat, lon):
        raise AssertionError("a coordinate we hold no stations for must not reach bahn.de")

    monkeypatch.setattr(bahn_api, "nearby", explode)
    # Sydney: no delay data out there, and every distinct coordinate would
    # otherwise be a guaranteed cache miss for a sweep to exploit
    assert await main.locations_nearby(
        _fake_request("198.51.100.13"), DummyResponse(), lat=-33.87, lon=151.21) == []


async def test_nearby_rate_limits_one_client(monkeypatch):
    calls = 0

    async def answer(lat, lon):
        nonlocal calls
        calls += 1
        return _NEARBY_TUEBINGEN

    monkeypatch.setattr(bahn_api, "nearby", answer)
    monkeypatch.setattr(main, "_nearby_limiter", ratelimit.SlidingWindowLimiter(
        burst_limit=2, burst_window=10, sustained_limit=10, sustained_window=60))
    request = _fake_request("198.51.100.14")
    for _ in range(2):
        await main.locations_nearby(request, DummyResponse(), lat=48.5216, lon=9.0576)
    with pytest.raises(HTTPException) as exc:
        # each coordinate is its own upstream call, so the sweep stops here
        await main.locations_nearby(request, DummyResponse(), lat=48.6, lon=9.1)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"]
    assert exc.value.headers["Cache-Control"] == "no-store"
    assert calls == 2


def test_healthy_tracks_the_circuit(monkeypatch):
    monkeypatch.setattr(bahn_api, "_breaker", bahn_api.CircuitBreaker(
        threshold=1, window=60, base_cooldown=30, max_cooldown=300, probes=1))
    assert bahn_api.healthy() is True
    bahn_api._breaker.force_open(30)
    assert bahn_api.healthy() is False


def _fake_request(ip: str = "198.51.100.7"):
    from starlette.requests import Request
    return Request({"type": "http", "headers": [], "client": (ip, 0),
                    "method": "GET", "scheme": "http", "path": "/api/journeys",
                    "query_string": b""})


async def _journeys_with_one_tight_transfer(monkeypatch, *, healthy: bool, stale: int = 0):
    """Drives the real endpoint over a synthetic upstream answer; returns the
    tight transfers it produced plus how many if-missed replans it requested."""
    payload = {"verbindungen": [{
        "verbindungsAbschnitte": [{
            "verkehrsmittel": {"typ": "PUBLICTRANSPORT", "mittelText": "ICE 1", "nummer": "1",
                               "produktGattung": "ICE"},
            "abfahrtsOrtExtId": "8011160", "abfahrtsOrt": "Berlin Hbf",
            "ankunftsOrtExtId": "8000261", "ankunftsOrt": "München Hbf",
            "abfahrt": {"sollzeit": "2026-08-13T10:00:00"},
            "ankunft": {"sollzeit": "2026-08-13T14:00:00"},
        }],
        "umstiegsAnzahl": 1,
    }], "verbindungReference": {}}

    async def fake_journeys(*a, **kw):
        return payload, stale

    calls: list[dict] = []

    async def fake_if_missed(legs, tt, window):
        calls.append(tt)
        return {"legs": [], "arrival": "2026-08-13T15:00:00"}

    monkeypatch.setattr(bahn_api, "journeys", fake_journeys)
    monkeypatch.setattr(bahn_api, "healthy", lambda: healthy)
    monkeypatch.setattr(main, "_if_missed_connection", fake_if_missed)
    monkeypatch.setattr(main, "tight_transfers", lambda legs: [
        {"station": "Hbf", "legIndex": 0, "depLegIndex": 1, "transferMinutes": 3,
         "medianDelay": 9, "unlikely": False} for _ in range(5)
    ])
    monkeypatch.setattr(main, "_search_limiter", _AlwaysAllow())
    # called directly, so FastAPI's Query defaults are passed explicitly
    out = await main.journeys(_fake_request(), DummyResponse(),
                              "A=1@O=a@L=1@", "A=1@O=b@L=2@", "2026-08-13T10:00:00",
                              window=7, paging_ref=None, mode="future", dticket=False)
    return [tt for j in out["journeys"] for tt in j["tightTransfers"]], calls


class _AlwaysAllow:
    def retry_after(self, key):
        return None


async def test_if_missed_replans_run_and_stay_capped_when_healthy(monkeypatch):
    transfers, calls = await _journeys_with_one_tight_transfer(monkeypatch, healthy=True)
    assert transfers, "the enrichment must still fire on a healthy upstream"
    assert len(calls) == main.MAX_IF_MISSED_REPLANS  # 5 offered, capped
    assert any("ifMissed" in tt for tt in transfers)


async def test_if_missed_replans_skipped_while_upstream_strains(monkeypatch):
    transfers, calls = await _journeys_with_one_tight_transfer(monkeypatch, healthy=False)
    assert calls == []  # not one extra upstream call while bahn.de is refusing
    # the warning itself still renders; only the optional disclosure is absent
    assert transfers and all("ifMissed" not in tt for tt in transfers)


async def test_if_missed_replans_skipped_when_answer_is_stale(monkeypatch):
    _transfers, calls = await _journeys_with_one_tight_transfer(
        monkeypatch, healthy=True, stale=300)
    assert calls == []  # a degraded answer must not trigger fresh upstream work
