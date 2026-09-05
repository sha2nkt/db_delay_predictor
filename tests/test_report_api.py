"""The report endpoints over HTTP: a Firebase bearer is the whole order form,
the two refusals name their reason, and the unsubscribe link in a mail works
without any login. Firebase is the `firebase` fixture from conftest.py."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main, reports
from tests.test_reports import SEARCH, journey, leg


@pytest.fixture(autouse=True)
def wiring(firebase, monkeypatch, tmp_path):
    monkeypatch.setattr(reports, "DB_PATH", tmp_path / "reports.db")
    # every request here shares one client IP; the budget is not under test
    reports.subscribe_limiter._hits.clear()
    monkeypatch.setattr(reports.subscribe_limiter, "_burst_limit", 10_000)
    monkeypatch.setattr(reports.subscribe_limiter, "_sustained_limit", 10_000)
    return firebase


@pytest.fixture
def client():
    # no context manager: the lifespan would load the delays DuckDB
    return TestClient(main.app)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def order(lang="de"):
    return {"lang": lang, "journey": journey(leg()), "search": SEARCH}


def test_a_press_without_an_account_is_401(client):
    assert client.post("/api/reports/subscribe", json=order()).status_code == 401
    assert client.get("/api/reports/mine").status_code == 401
    assert client.delete("/api/reports/1").status_code == 401


def test_a_bad_token_is_401(client):
    resp = client.post("/api/reports/subscribe", json=order(), headers=bearer("nope"))
    assert resp.status_code == 401


def test_an_unproven_address_is_403(client, wiring):
    fresh = wiring.token("t-fresh", uid="u-fresh", verified=False)
    resp = client.post("/api/reports/subscribe", json=order(), headers=bearer(fresh))
    assert resp.status_code == 403 and resp.json()["detail"] == "unverified"


def test_a_phone_account_has_no_address_to_mail(client, wiring):
    phone = wiring.token("t-phone", uid="u-phone", provider="phone")
    resp = client.post("/api/reports/subscribe", json=order(), headers=bearer(phone))
    assert resp.status_code == 403


def test_order_list_and_cancel(client, wiring):
    # no username needed: the report goes to the inbox, not the board
    headers = bearer(wiring.token("t1", uid="u1"))
    # one payload for both presses: order() stamps the legs from the clock, so a
    # second call a second later would be a different itinerary, not a repress
    payload = order("en")
    resp = client.post("/api/reports/subscribe", json=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True and body["email"] == "u1@example.org"
    assert body["key"] and body["travelDate"]
    # the same press again is the same order
    again = client.post("/api/reports/subscribe", json=payload, headers=headers).json()
    assert again["created"] is False and again["id"] == body["id"]

    mine = client.get("/api/reports/mine", headers=headers).json()
    assert mine["email"] == "u1@example.org"
    assert [(s["id"], s["key"]) for s in mine["subscriptions"]] == [(body["id"], body["key"])]

    # somebody else cannot withdraw it
    other = bearer(wiring.token("t2", uid="u2"))
    assert client.delete(f"/api/reports/{body['id']}", headers=other).status_code == 404
    assert client.delete(f"/api/reports/{body['id']}", headers=headers).status_code == 204
    assert client.delete(f"/api/reports/{body['id']}", headers=headers).status_code == 404
    assert client.get("/api/reports/mine", headers=headers).json()["subscriptions"] == []


def test_an_unusable_journey_is_422(client, wiring):
    headers = bearer(wiring.token("t1", uid="u1"))
    bus_only = {"lang": "de", "journey": journey(leg(product="BUS")), "search": SEARCH}
    assert client.post("/api/reports/subscribe", json=bus_only, headers=headers).status_code == 422
    resp = client.post("/api/reports/subscribe", json={"lang": "fr", "journey": {}}, headers=headers)
    assert resp.status_code == 422


def test_a_fourth_open_order_is_409_naming_the_cap(client, wiring):
    headers = bearer(wiring.token("t1", uid="u1"))
    for nr, hour in [("101", 6), ("599", 7), ("77", 9)]:
        payload = {"lang": "en", "journey": journey(leg(fahrt_nr=nr, arr_h=hour)), "search": SEARCH}
        assert client.post("/api/reports/subscribe", json=payload, headers=headers).status_code == 200
    over = {"lang": "en", "journey": journey(leg(fahrt_nr="42", arr_h=11)), "search": SEARCH}
    resp = client.post("/api/reports/subscribe", json=over, headers=headers)
    assert resp.status_code == 409
    # the page names the number it refuses on, so the copy needs it back
    assert resp.json()["detail"] == {"error": "too_many_open_reports", "limit": 3}
    # another account is untouched by it
    other = bearer(wiring.token("t2", uid="u2"))
    assert client.post("/api/reports/subscribe", json=over, headers=other).status_code == 200


def test_the_unsubscribe_link_needs_no_login(client, wiring):
    headers = bearer(wiring.token("t1", uid="u1"))
    client.post("/api/reports/subscribe", json=order("en"), headers=headers)
    with sqlite3.connect(reports.DB_PATH) as conn:
        token = conn.execute("SELECT unsub_token FROM subscriptions").fetchone()[0]
    page = client.get(f"/r/unsubscribe?token={token}")
    assert page.status_code == 200
    # a deliberate click, never auto-submitted, and nothing for crawlers
    assert "Unsubscribe" in page.text and "<form" in page.text and "noindex" in page.text
    done = client.post(f"/r/unsubscribe?token={token}")
    assert done.status_code == 200 and "Unsubscribed" in done.text
    assert client.get("/api/reports/mine", headers=headers).json()["subscriptions"] == []
    assert client.get("/r/unsubscribe?token=nope").status_code == 404
    assert client.get("/r/confirm?token=x").status_code == 404  # the double opt-in is gone
