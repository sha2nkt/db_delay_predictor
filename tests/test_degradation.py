"""Endpoint behavior when bahn.de is straining: autocomplete degrades quietly,
and the optional if-missed replans stop spending upstream budget."""

import pytest

from app import bahn_api, delays, main

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


def test_healthy_tracks_the_circuit(monkeypatch):
    monkeypatch.setattr(bahn_api, "_breaker", bahn_api.CircuitBreaker(
        threshold=1, window=60, base_cooldown=30, max_cooldown=300, probes=1))
    assert bahn_api.healthy() is True
    bahn_api._breaker.force_open(30)
    assert bahn_api.healthy() is False


def _fake_request():
    from starlette.requests import Request
    return Request({"type": "http", "headers": [], "client": ("198.51.100.7", 0),
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
                              window=7, paging_ref=None, mode="future", dticket=False,
                              transfer_time=0)
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
