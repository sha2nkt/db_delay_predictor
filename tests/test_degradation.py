"""Endpoint behavior when bahn.de is straining: autocomplete degrades quietly,
and the optional if-missed replans stop spending upstream budget."""

import logging

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

    async def fake_if_missed(legs, tt, window, dticket="off", products=None):
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
                              window=7, paging_ref=None, mode="future", dticket="0",
                              age="adult", travellers_raw=None, transfer=0)
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


async def _if_missed_over(monkeypatch, replies: list, dtickets: list[str] | None = None):
    """Runs _if_missed_connection against a scripted _replan; returns (results, calls).
    Each reply is either a payload dict or None (upstream refused); dtickets sets
    the search's toggle per run (default "off")."""
    main._if_missed_cache.clear()
    calls: list[tuple] = []

    async def fake_replan(origin, dest, ready, source, dticket="off", products=None):
        calls.append((origin["id"], dest["id"], ready, source, dticket, products))
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(main, "_replan", fake_replan)
    legs = [
        {"walking": False, "line": {"product": "ICE"}, "plannedArrival": "2026-08-13T12:00:00",
         "origin": {"id": "8011160", "name": "Berlin Hbf"},
         "destination": {"id": "8000105", "name": "Frankfurt Hbf"}},
        {"walking": False, "line": {"product": "ICE"}, "plannedDeparture": "2026-08-13T12:05:00",
         "origin": {"id": "8000105", "name": "Frankfurt Hbf"},
         "destination": {"id": "8000261", "name": "München Hbf"}},
    ]
    tt = {"legIndex": 0, "depLegIndex": 1, "medianDelay": 9}
    out = [await main._if_missed_connection(legs, tt, 7, dticket) for dticket in dtickets or ["off"] * len(replies)]
    return out, calls


def _replacement(depart: str, arrive: str) -> dict:
    return {"verbindungen": [{"verbindungsAbschnitte": [{
        "verkehrsmittel": {"typ": "PUBLICTRANSPORT", "mittelText": "ICE 9", "nummer": "9",
                           "produktGattung": "ICE"},
        "abfahrtsOrtExtId": "8000105", "abfahrtsOrt": "Frankfurt Hbf",
        "ankunftsOrtExtId": "8000261", "ankunftsOrt": "München Hbf",
        "abfahrt": {"sollzeit": depart}, "ankunft": {"sollzeit": arrive},
    }]}]}


async def test_if_missed_answer_is_cached_across_searches(monkeypatch):
    payload = _replacement("2026-08-13T12:40:00", "2026-08-13T16:00:00")
    out, calls = await _if_missed_over(monkeypatch, [payload, payload])
    # the key is timetable-derived, so the second search reuses the first one's answer
    assert len(calls) == 1
    assert out[0] is out[1]
    assert out[0]["arrival"] == "2026-08-13T16:00:00"


async def test_if_missed_caches_the_absence_of_a_connection(monkeypatch):
    # a payload whose only option departs too soon to be catchable
    payload = _replacement("2026-08-13T12:09:00", "2026-08-13T16:00:00")
    out, calls = await _if_missed_over(monkeypatch, [payload, payload])
    assert out == [None, None]
    assert len(calls) == 1  # "no connection" is an answer, not a reason to re-ask


async def test_if_missed_keeps_the_dticket_restriction(monkeypatch):
    payload = _replacement("2026-08-13T12:40:00", "2026-08-13T16:00:00")
    out, calls = await _if_missed_over(monkeypatch, [payload] * 3,
                                       dtickets=["only", "off", "all"])
    # "only" must reach the replan (a D-Ticket passenger cannot board an ICE) and
    # must not share a cache entry with the unrestricted answer; "all" accepts paid
    # trains, so it folds onto "off" and reuses its entry
    assert [c[4] for c in calls] == ["only", "off"]
    assert out[0] is not out[1] and out[1] is out[2]


async def test_if_missed_never_caches_an_upstream_refusal(monkeypatch):
    payload = _replacement("2026-08-13T12:40:00", "2026-08-13T16:00:00")
    out, calls = await _if_missed_over(monkeypatch, [None, payload])
    assert out[0] is None and out[1] is not None
    assert len(calls) == 2  # the refusal must not stick to the route


async def test_if_missed_cache_evicts_oldest_past_the_cap(monkeypatch):
    monkeypatch.setattr(main, "IF_MISSED_CACHE_MAX", 1)
    main._if_missed_cache.clear()
    calls: list[str] = []

    async def fake_replan(origin, dest, ready, source, dticket="off", products=None):
        calls.append(dest["id"])
        return _replacement("2026-08-13T12:40:00", "2026-08-13T16:00:00")

    monkeypatch.setattr(main, "_replan", fake_replan)

    async def ask(dest_id: str):
        legs = [
            {"walking": False, "line": {"product": "ICE"}, "plannedArrival": "2026-08-13T12:00:00",
             "origin": {"id": "8011160", "name": "Berlin Hbf"},
             "destination": {"id": "8000105", "name": "Frankfurt Hbf"}},
            {"walking": False, "line": {"product": "ICE"}, "plannedDeparture": "2026-08-13T12:05:00",
             "origin": {"id": "8000105", "name": "Frankfurt Hbf"},
             "destination": {"id": dest_id, "name": "dest"}},
        ]
        return await main._if_missed_connection(
            legs, {"legIndex": 0, "depLegIndex": 1, "medianDelay": 9}, 7)

    await ask("8000261")
    await ask("8000207")  # evicts the first at a cap of one
    await ask("8000261")
    assert calls == ["8000261", "8000207", "8000261"]
    assert len(main._if_missed_cache) == 1


async def test_if_missed_cache_rollup_reaches_the_log(monkeypatch, caplog):
    monkeypatch.setattr(main, "IF_MISSED_LOG_EVERY", 4)
    # start the interval from wherever the shared counters already stand
    base = (bahn_api.metrics["if_missed_hits"], bahn_api.metrics["if_missed_misses"])
    monkeypatch.setattr(main, "_if_missed_logged", base)
    with caplog.at_level(logging.INFO, logger="app.main"):
        for i in range(8):
            main._note_if_missed(i % 2 == 0)
    lines = [r.getMessage() for r in caplog.records if "if-missed cache" in r.getMessage()]
    assert len(lines) == 2  # one rollup per 4 lookups, not a line per lookup
    assert "hit_rate=50%" in lines[0] and "over last 4" in lines[0]
