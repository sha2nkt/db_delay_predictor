"""Report orders: validation, one row per account and itinerary, the scrubs
(cancel, the mail's unsubscribe link, account erasure, retention), and the
daily job's queries."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from app import delays, reports

JONAS = {"uid": "u-jonas", "email": "jonas@example.org", "name": "Jonas"}
MIA = {"uid": "u-mia", "email": "mia@example.org", "name": None}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "DB_PATH", tmp_path / "reports.db")
    return tmp_path / "reports.db"


def _berlin_now():
    return datetime.now(delays.BERLIN).replace(tzinfo=None)


def leg(name="ICE 1080", fahrt_nr="1080", product="ICE", dep_h=2.0, arr_h=5.0,
        origin=("8000284", "Nürnberg Hbf"), dest=("8002549", "Hamburg Hbf")):
    dep = (_berlin_now() + timedelta(hours=dep_h)).isoformat(timespec="seconds")
    arr = (_berlin_now() + timedelta(hours=arr_h)).isoformat(timespec="seconds")
    out = {
        "walking": False,
        "line": {"name": name, "fahrtNr": fahrt_nr, "product": product},
        "origin": {"id": origin[0], "name": origin[1]},
        "destination": {"id": dest[0], "name": dest[1]},
        "plannedDeparture": dep,
        "plannedArrival": arr,
    }
    if product not in reports.UNTRACKED_PRODUCTS:
        out["delayStats"] = {"medianDelay": 4, "maxDelay": 12, "daysMatched": 7,
                             "canceledDays": 0}
    return out


def journey(*legs_):
    return {"transfers": max(0, len(legs_) - 1), "legs": list(legs_)}


SEARCH = {"fromName": "Nürnberg Hbf", "toName": "Hamburg Hbf", "window": 7}


def rows(db, cols="uid, email, name, status"):
    with sqlite3.connect(db) as conn:
        return [tuple(r) for r in conn.execute(f"SELECT {cols} FROM subscriptions ORDER BY id")]


def test_an_order_is_active_at_once(db):
    j = journey(leg())
    res = reports.subscribe(JONAS, "de", j, SEARCH)
    assert res["created"] is True and res["id"] == 1
    assert res["email"] == "jonas@example.org"
    assert res["key"] == reports.journey_key(j)
    assert res["fromName"] == "Nürnberg Hbf" and res["toName"] == "Hamburg Hbf"
    assert rows(db) == [("u-jonas", "jonas@example.org", "Jonas", "active")]


def test_a_second_press_hands_back_the_same_order(db):
    j = journey(leg())
    first = reports.subscribe(JONAS, "de", j, SEARCH)
    again = reports.subscribe(JONAS, "en", j, SEARCH)
    assert again["created"] is False and again["id"] == first["id"]
    assert len(rows(db)) == 1


def test_two_accounts_may_order_the_same_journey(db):
    j = journey(leg())
    reports.subscribe(JONAS, "de", j, SEARCH)
    assert reports.subscribe(MIA, "de", j, SEARCH)["created"] is True
    assert rows(db, "uid, name") == [("u-jonas", "Jonas"), ("u-mia", "")]


def test_open_orders_per_account_are_capped(db):
    assert reports.MAX_OPEN_PER_ACCOUNT == 3
    for nr, hour in [("101", 6), ("599", 7), ("77", 9)]:
        reports.subscribe(JONAS, "de", journey(leg(fahrt_nr=nr, arr_h=hour)), SEARCH)
    with pytest.raises(reports.TooManyOpenReports) as exc:
        reports.subscribe(JONAS, "de", journey(leg(fahrt_nr="42", arr_h=11)), SEARCH)
    assert exc.value.limit == 3
    # the cap is per account
    assert reports.subscribe(MIA, "de", journey(leg()), SEARCH)["created"] is True


def test_a_cancelled_order_frees_a_slot(db, monkeypatch):
    monkeypatch.setattr(reports, "MAX_OPEN_PER_ACCOUNT", 1)
    first = reports.subscribe(JONAS, "de", journey(leg()), SEARCH)
    over = journey(leg(fahrt_nr="77", arr_h=9))
    with pytest.raises(reports.TooManyOpenReports):
        reports.subscribe(JONAS, "de", over, SEARCH)
    # a repress on an order that is already open is never the one over the cap
    assert reports.subscribe(JONAS, "de", journey(leg()), SEARCH)["created"] is False
    reports.cancel("u-jonas", first["id"])
    assert reports.subscribe(JONAS, "de", over, SEARCH)["created"] is True


def test_mine_lists_open_orders_with_their_keys(db):
    j1, j2 = journey(leg()), journey(leg(fahrt_nr="599", arr_h=7))
    a = reports.subscribe(JONAS, "de", j1, SEARCH)
    b = reports.subscribe(JONAS, "de", j2, SEARCH)
    reports.subscribe(MIA, "de", j1, SEARCH)
    mine = reports.mine("u-jonas")
    assert [(m["id"], m["key"]) for m in mine] == [
        (a["id"], reports.journey_key(j1)), (b["id"], reports.journey_key(j2)),
    ]
    assert mine[0]["travelDate"] == a["travelDate"]
    assert reports.mine("u-nobody") == []


def test_cancel_scrubs_the_row_and_only_the_owners(db):
    res = reports.subscribe(JONAS, "de", journey(leg()), SEARCH)
    assert reports.cancel("u-mia", res["id"]) is False
    assert reports.cancel("u-jonas", res["id"]) is True
    assert rows(db) == [(None, None, "", "cancelled")]
    assert reports.cancel("u-jonas", res["id"]) is False  # already withdrawn
    assert reports.mine("u-jonas") == []
    # ordering again is a fresh row; the withdrawn one stays as an anonymous statistic
    assert reports.subscribe(JONAS, "de", journey(leg()), SEARCH)["created"] is True
    assert len(rows(db)) == 2


def test_the_mails_unsubscribe_link_scrubs_every_row_of_the_account(db):
    reports.subscribe(JONAS, "de", journey(leg()), SEARCH)
    sent = reports.subscribe(JONAS, "de", journey(leg(fahrt_nr="599", arr_h=7)), SEARCH)
    reports.subscribe(MIA, "de", journey(leg()), SEARCH)
    reports.mark_sent(sent["id"], {"legs": {}})
    with sqlite3.connect(db) as conn:
        token = conn.execute(
            "SELECT unsub_token FROM subscriptions WHERE id = ?", (sent["id"],)
        ).fetchone()[0]
    assert reports.token_lang(token) == "de"
    assert reports.unsubscribe(token) is True
    assert rows(db) == [
        (None, None, "", "cancelled"),              # the open one is withdrawn
        (None, None, "", "sent"),                   # the sent one stays sent, anonymously
        ("u-mia", "mia@example.org", "", "active"),  # somebody else's is untouched
    ]
    assert reports.unsubscribe(token) is True   # a re-clicked link stays a success
    assert reports.unsubscribe("nope") is False
    assert reports.token_lang("nope") is None


def test_account_erasure_scrubs_like_the_link(db):
    reports.subscribe(JONAS, "de", journey(leg()), SEARCH)
    reports.forget_account("u-jonas")
    assert rows(db) == [(None, None, "", "cancelled")]


def test_rejects_bad_payloads(db):
    with pytest.raises(reports.SnapshotError):  # only untracked legs
        reports.subscribe(JONAS, "de", journey(leg(product="BUS")), SEARCH)
    with pytest.raises(reports.SnapshotError):  # already arrived
        reports.subscribe(JONAS, "de", journey(leg(dep_h=-30, arr_h=-27)), SEARCH)
    with pytest.raises(reports.SnapshotError):  # walking-only journey
        reports.subscribe(JONAS, "de", journey({"walking": True}), SEARCH)
    with pytest.raises(reports.SnapshotError):  # no legs at all
        reports.subscribe(JONAS, "de", {"legs": []}, SEARCH)
    assert not db.exists()  # validation runs before the database is even opened


def test_journey_key_mirrors_the_page():
    """Only legs the page marked resolvable, raw fields, sorted - the string
    reportKey() in app.js derives from the same object."""
    j = journey(
        leg(fahrt_nr="1080", arr_h=5),
        {"walking": True, "origin": {}, "destination": {}},
        leg(name="Bus 12", fahrt_nr="12", product="BUS", arr_h=6),
        leg(name="RE 5", fahrt_nr="5", arr_h=3),
    )
    lines = reports.journey_key(j).split("\n")
    assert len(lines) == 2 and lines == sorted(lines)
    assert {line.split("|")[0] for line in lines} == {"1080", "5"}
    assert all(line.split("|")[1] == "8002549" for line in lines)
    # numbers keep their digits, null becomes "" - as `?? ""` does in JS
    odd = {"legs": [{"delayStats": None, "line": {"fahrtNr": 7},
                     "destination": {"id": None}, "plannedArrival": "x"}]}
    assert reports.journey_key(odd) == "7||x"


def test_due_gate_and_timeout(db):
    reports.subscribe(JONAS, "de", journey(leg()), SEARCH)
    with sqlite3.connect(db) as conn:  # pretend the journey was on Aug 10
        conn.execute("UPDATE subscriptions SET travel_date = '2026-08-10'")
    # the data covers a later day: due (the normal D+2 morning)
    assert len(reports.due_rows("2026-08-11", "2026-08-12", 10)) == 1
    # the data is stuck before the travel day: not due...
    assert reports.due_rows("2026-08-09", "2026-08-12", 10) == []
    # ...until the timeout passes despite the outage
    assert len(reports.due_rows("2026-08-09", "2026-08-25", 10)) == 1


def test_mark_sent_removes_from_due(db):
    reports.subscribe(JONAS, "de", journey(leg()), SEARCH)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE subscriptions SET travel_date = '2026-08-10'")
    due = reports.due_rows("2026-08-12", "2026-08-12", 10)
    assert due and due[0]["unsub_token"] and due[0]["email"] == "jonas@example.org"
    reports.mark_sent(due[0]["id"], {"legs": {"0": {"delayMin": 3, "canceled": False}}})
    assert reports.due_rows("2026-08-12", "2026-08-12", 10) == []


def test_record_failure_gives_up_after_retries(db):
    res = reports.subscribe(JONAS, "de", journey(leg()), SEARCH)
    reports.record_failure(res["id"], "boom", give_up=False)
    assert rows(db, "status, attempts") == [("active", 1)]
    reports.record_failure(res["id"], "boom", give_up=True)
    assert rows(db, "status, attempts, last_error") == [("failed", 2, "boom")]


def test_retention_sweep_scrubs_settled_rows_only(db):
    sent = reports.subscribe(JONAS, "de", journey(leg()), SEARCH)
    reports.subscribe(JONAS, "de", journey(leg(fahrt_nr="599", arr_h=7)), SEARCH)
    reports.mark_sent(sent["id"], {"legs": {}})
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE subscriptions SET sent_ts = '2026-01-01T00:00:00+00:00'"
                     " WHERE id = ?", (sent["id"],))
    assert reports.scrub_old(apply=False) == 1  # dry run only counts
    assert rows(db)[0][0] == "u-jonas"
    assert reports.scrub_old(apply=True) == 1
    assert rows(db) == [
        (None, None, "", "sent"),
        ("u-jonas", "jonas@example.org", "Jonas", "active"),  # open orders keep their owner
    ]
    assert reports.scrub_old(apply=True) == 0


def test_throttle_is_the_shared_limiter():
    assert reports.subscribe_limiter.retry_after("9.9.9.9-reports-test") is None
