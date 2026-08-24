"""Edge cases and conflicts of the passwordless flow, driven over HTTP.

test_auth.py covers the auth module directly; this drives the real endpoints,
so it also pins the validation rules, status codes and cookie handling the
frontend depends on. No mail is sent: the mailer is replaced, and the token/code
are read from the issuing function instead of from an inbox.
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth, mailer, main, stories

LIMITERS = (
    auth.register_limiter, auth.login_limiter, auth.code_limiter,
    auth.suggest_limiter,
    stories.write_limiter, stories.vote_limiter,
)


@pytest.fixture(autouse=True)
def wiring(monkeypatch, tmp_path):
    """Temp DB, no outbound mail, and a clean rate-limit budget per test -
    the limiters are process-global singletons and would otherwise leak
    state between tests."""
    monkeypatch.setattr(stories, "DB_PATH", tmp_path / "stories.db")

    issued, mails = [], []
    real_issue = auth._issue_magic

    def spy_issue(conn, user_id):
        result = real_issue(conn, user_id)
        if result is None:
            return None  # the account's send budget refused; nothing minted
        token, code = result
        issued.append({"user_id": user_id, "token": token, "code": code})
        return token, code

    def fake_send(email, name, token, code, lang, kind):
        mails.append({"email": email, "name": name, "kind": kind, "lang": lang})
        return True

    def fake_spawn(coro):
        coro.close()  # nothing awaits background tasks under TestClient

    monkeypatch.setattr(auth, "_issue_magic", spy_issue)
    monkeypatch.setattr(mailer, "send_magic_link", fake_send)
    monkeypatch.setattr(main, "_spawn", fake_spawn)
    # off by default so ordinary tests can ask for two links in a row; the
    # tests that are about the cooldown set it back explicitly
    monkeypatch.setattr(auth, "RESEND_COOLDOWN_SECONDS", 0)
    # every request in a test shares one client IP, so the real per-IP budgets
    # would fire on the third registration; the two tests that are *about*
    # rate limiting restore them with realistic().
    for limiter in LIMITERS:
        limiter._hits.clear()
        monkeypatch.setattr(limiter, "_burst_limit", 10_000)
        monkeypatch.setattr(limiter, "_sustained_limit", 10_000)

    def realistic(limiter, burst, sustained):
        limiter._hits.clear()
        monkeypatch.setattr(limiter, "_burst_limit", burst)
        monkeypatch.setattr(limiter, "_sustained_limit", sustained)

    return SimpleNamespace(issued=issued, mails=mails, realistic=realistic)


@pytest.fixture
def client():
    # https base URL: the session cookie is Secure, so an http:// test client
    # would silently drop it and every authenticated assertion would 401.
    # No context manager - the lifespan would load the delays DuckDB, which
    # none of these endpoints touch.
    return TestClient(main.app, base_url="https://testserver")


def register(client, name="Jonas", email="jonas@example.org", **kw):
    return client.post("/api/auth/register",
                       json={"name": name, "email": email, **kw})


def request_link(client, email="jonas@example.org", **kw):
    return client.post("/api/auth/request-link", json={"email": email, **kw})


def last(wiring):
    return wiring.issued[-1]


# --- registration validation ------------------------------------------------

@pytest.mark.parametrize("name", [
    "a",                    # too short
    "x" * 26,               # too long
    "has space",
    "Jönas",                # non-ASCII
    "semi;colon",
    "<script>",
    "",
])
def test_invalid_usernames_are_rejected(client, name):
    assert register(client, name=name).status_code == 422


@pytest.mark.parametrize("email", [
    "not-an-email",
    "@example.org",
    "jonas@",
    "jonas@example",        # no TLD
    "two@@example.org",
    "spaced out@example.org",
    "",
    "x" * 250 + "@example.org",   # over 254
])
def test_invalid_emails_are_rejected(client, email):
    assert register(client, email=email).status_code == 422


@pytest.mark.parametrize("name", ["Jo-nas", "Jo_nas", "AB", "x" * 25, "12345"])
def test_valid_usernames_are_accepted(client, name):
    assert register(client, name=name, email=f"{name}@example.org").status_code == 202


def test_missing_fields_are_rejected(client):
    assert client.post("/api/auth/register", json={"name": "Jonas"}).status_code == 422
    assert client.post("/api/auth/register", json={"email": "a@b.co"}).status_code == 422
    assert client.post("/api/auth/register", json={}).status_code == 422


def test_unknown_language_is_rejected(client):
    assert register(client, lang="fr").status_code == 422


# --- name and email conflicts ----------------------------------------------

def test_taken_name_conflicts_regardless_of_case(client):
    assert register(client, name="Jonas", email="a@example.org").status_code == 202
    assert register(client, name="Jonas", email="b@example.org").status_code == 409
    assert register(client, name="JONAS", email="c@example.org").status_code == 409
    assert register(client, name="jonas", email="d@example.org").status_code == 409


def test_reregistering_an_email_never_makes_a_second_account(client, wiring):
    register(client, name="Jonas", email="jonas@example.org")
    first = last(wiring)

    # different name, same address, any casing: same account, and the response
    # is the same 202 a real signup returns
    resp = register(client, name="Jonas2", email="JONAS@Example.ORG")
    assert resp.status_code == 202
    assert last(wiring)["user_id"] == first["user_id"]

    client.post("/api/auth/consume", json={"token": last(wiring)["token"]})
    assert client.get("/api/auth/me").json()["name"] == "Jonas2"


def test_a_confirmed_account_keeps_its_name(client, wiring):
    register(client, name="Jonas", email="jonas@example.org")
    client.post("/api/auth/consume", json={"token": last(wiring)["token"]})

    register(client, name="Hijack", email="jonas@example.org")
    client.post("/api/auth/consume", json={"token": last(wiring)["token"]})
    assert client.get("/api/auth/me").json()["name"] == "Jonas"


# --- per-account send budget ------------------------------------------------

def test_cooldown_suppresses_the_mail_but_still_answers_202(client, wiring, monkeypatch):
    monkeypatch.setattr(auth, "RESEND_COOLDOWN_SECONDS", 60)
    register(client, email="jonas@example.org")
    first = last(wiring)
    assert len(wiring.mails) == 1

    resp = request_link(client, "jonas@example.org")
    assert resp.status_code == 202          # indistinguishable from a real send
    assert len(wiring.mails) == 1           # ... but nothing went out
    assert len(wiring.issued) == 1          # and no new token was minted

    # crucially, the first link still works - throttling must not lock anyone out
    assert client.post("/api/auth/consume",
                       json={"token": first["token"]}).status_code == 200


def test_a_refused_send_is_a_503_and_refunds_the_budget(client, wiring, monkeypatch):
    """The relay refusing (out of credits) must not turn into "check your
    inbox" - and the retry the user is told to make must actually mint a new
    link, not run into the cooldown the failed send started."""
    monkeypatch.setattr(auth, "RESEND_COOLDOWN_SECONDS", 60)
    monkeypatch.setattr(mailer, "send_magic_link", lambda *a: False)
    assert register(client, email="jonas@example.org").status_code == 503
    assert len(wiring.issued) == 1

    monkeypatch.setattr(mailer, "send_magic_link", lambda *a: True)
    assert request_link(client, "jonas@example.org").status_code == 202
    assert len(wiring.issued) == 2          # the refund made room for this one
    # the voided first token is dead; the second one logs in
    assert client.post("/api/auth/consume",
                       json={"token": wiring.issued[0]["token"]}).status_code == 401
    assert client.post("/api/auth/consume",
                       json={"token": wiring.issued[1]["token"]}).status_code == 200


def test_both_send_endpoints_report_the_resend_cooldown(client, monkeypatch):
    """The login page's resend countdown is the server's own constant, not a
    number copied into the JS. It is the same for every account, so it cannot
    be read as "this one asked for a login recently"."""
    monkeypatch.setattr(auth, "RESEND_COOLDOWN_SECONDS", 45)
    register(client, email="jonas@example.org")
    assert request_link(client, "jonas@example.org").json() == {"resend_after": 45}
    assert register(client, name="Meike", email="meike@example.org").json() \
        == {"resend_after": 45}


def test_the_daily_send_cap_stops_further_mail(client, wiring, monkeypatch):
    monkeypatch.setattr(auth, "MAX_LINKS_PER_DAY", 3)
    register(client, email="jonas@example.org")
    for _ in range(5):
        assert request_link(client, "jonas@example.org").status_code == 202
    assert len(wiring.issued) == 3


def test_the_send_budget_is_per_account_not_global(client, wiring, monkeypatch):
    monkeypatch.setattr(auth, "RESEND_COOLDOWN_SECONDS", 60)
    register(client, name="Jonas", email="jonas@example.org")
    register(client, name="Meike", email="meike@example.org")
    # Jonas is on cooldown; Meike is unaffected
    request_link(client, "jonas@example.org")
    assert len(wiring.issued) == 2
    request_link(client, "meike@example.org")
    assert len(wiring.issued) == 2  # Meike is on her own cooldown, also fresh


# --- cumulative guess budget ------------------------------------------------

def test_a_new_mail_does_not_restore_spent_guesses(client, wiring, monkeypatch):
    monkeypatch.setattr(auth, "MAX_CODE_FAILS_PER_DAY", 6)
    register(client, email="jonas@example.org")

    # burn one whole per-login budget, then ask for a fresh mail
    for _ in range(auth.MAX_TRIES):
        client.post("/api/auth/consume-code",
                    json={"email": "jonas@example.org",
                          "code": wrong(last(wiring)["code"])})
    request_link(client, "jonas@example.org")
    fresh = last(wiring)

    # one guess left on the daily figure; spending it closes the code path for
    # the rest of the day, even for the correct code
    client.post("/api/auth/consume-code",
                json={"email": "jonas@example.org", "code": wrong(fresh["code"])})
    assert client.post("/api/auth/consume-code",
                       json={"email": "jonas@example.org",
                             "code": fresh["code"]}).status_code == 401
    # the link half is deliberately untouched - see the lockout test above
    assert client.post("/api/auth/consume",
                       json={"token": fresh["token"]}).status_code == 200


def test_spent_guesses_never_lock_the_owner_out_of_the_link(client, wiring, monkeypatch):
    """The daily guess cap must refuse codes without voiding the pending
    login. If it voided it, one guess after each mail would let an attacker
    kill every link the owner requests - a denial of login."""
    monkeypatch.setattr(auth, "MAX_CODE_FAILS_PER_DAY", 2)
    register(client, email="jonas@example.org")
    for _ in range(2):
        client.post("/api/auth/consume-code",
                    json={"email": "jonas@example.org",
                          "code": wrong(last(wiring)["code"])})

    request_link(client, "jonas@example.org")
    fresh = last(wiring)
    # an attacker submitting a guess against the brand-new login
    client.post("/api/auth/consume-code",
                json={"email": "jonas@example.org", "code": wrong(fresh["code"])})
    # the code half stays refused, but the link the owner received still works
    assert client.post("/api/auth/consume-code",
                       json={"email": "jonas@example.org",
                             "code": fresh["code"]}).status_code == 401
    assert client.post("/api/auth/consume",
                       json={"token": fresh["token"]}).status_code == 200


def test_the_daily_guess_cap_is_per_account(client, wiring, monkeypatch):
    monkeypatch.setattr(auth, "MAX_CODE_FAILS_PER_DAY", 3)
    register(client, name="Jonas", email="jonas@example.org")
    jonas = last(wiring)
    register(client, name="Meike", email="meike@example.org")
    meike = last(wiring)
    for _ in range(3):
        client.post("/api/auth/consume-code",
                    json={"email": "jonas@example.org", "code": wrong(jonas["code"])})
    assert client.post("/api/auth/consume-code",
                       json={"email": "meike@example.org",
                             "code": meike["code"]}).status_code == 200


def test_an_unknown_email_is_told_so_rather_than_promised_a_mail(client, wiring):
    """A login form that answers "check your inbox" for an address that will
    never receive anything is a dead end - so this one says there is no
    account, and the page offers to create one. The cost is that the endpoint
    answers "is this address registered?"; login_limiter caps how often."""
    register(client, email="jonas@example.org")
    wiring.issued.clear()

    assert request_link(client, "jonas@example.org").status_code == 202
    assert request_link(client, "nobody@example.org").status_code == 404
    # only the known address minted anything
    assert len(wiring.issued) == 1


def test_a_spent_cooldown_is_not_reported_as_a_missing_account(client, wiring, monkeypatch):
    """The two None-ish cases must stay apart: an account on cooldown gets the
    same 202 as a fresh send, or the page would tell a real user to create the
    account they already have."""
    monkeypatch.setattr(auth, "RESEND_COOLDOWN_SECONDS", 60)
    register(client, email="jonas@example.org")
    assert len(wiring.issued) == 1

    resp = request_link(client, "jonas@example.org")
    assert resp.status_code == 202     # not 404
    assert len(wiring.issued) == 1     # ... though nothing new was minted


def test_email_whitespace_and_case_are_normalized(client, wiring):
    register(client, email="  Jonas@Example.ORG  ")
    uid = last(wiring)["user_id"]
    assert request_link(client, "jonas@example.org").status_code == 202
    assert last(wiring)["user_id"] == uid


# --- redeeming the link -----------------------------------------------------

def test_link_round_trip_sets_a_session_cookie(client, wiring):
    register(client)
    resp = client.post("/api/auth/consume", json={"token": last(wiring)["token"]})
    assert resp.status_code == 200
    cookie = resp.cookies.get(main.SESSION_COOKIE)
    assert cookie
    # the raw Set-Cookie carries the hardening flags the browser relies on
    raw = resp.headers["set-cookie"].lower()
    assert "httponly" in raw and "samesite=lax" in raw and "secure" in raw


@pytest.mark.parametrize("token", ["wrong-token", "x", "../../etc/passwd", "%00"])
def test_bad_link_tokens_are_rejected(client, token):
    register(client)
    assert client.post("/api/auth/consume", json={"token": token}).status_code == 401


def test_empty_token_is_a_validation_error(client):
    assert client.post("/api/auth/consume", json={"token": ""}).status_code == 422
    assert client.post("/api/auth/consume", json={}).status_code == 422


def test_link_is_single_use(client, wiring):
    register(client)
    token = last(wiring)["token"]
    assert client.post("/api/auth/consume", json={"token": token}).status_code == 200
    assert client.post("/api/auth/consume", json={"token": token}).status_code == 401


def test_a_new_link_kills_the_previous_one(client, wiring):
    register(client)
    old = last(wiring)
    request_link(client)
    new = last(wiring)
    assert client.post("/api/auth/consume", json={"token": old["token"]}).status_code == 401
    assert client.post("/api/auth/consume-code",
                       json={"email": "jonas@example.org",
                             "code": old["code"]}).status_code == 401
    assert client.post("/api/auth/consume", json={"token": new["token"]}).status_code == 200


def test_expired_link_and_code_are_rejected(client, wiring, monkeypatch):
    monkeypatch.setattr(auth, "MAGIC_LINK_HOURS", -1)
    register(client)
    pending = last(wiring)
    assert client.post("/api/auth/consume",
                       json={"token": pending["token"]}).status_code == 401
    assert client.post("/api/auth/consume-code",
                       json={"email": "jonas@example.org",
                             "code": pending["code"]}).status_code == 401


# --- redeeming the code -----------------------------------------------------

def test_code_round_trip(client, wiring):
    register(client)
    resp = client.post("/api/auth/consume-code",
                       json={"email": "JONAS@example.org", "code": last(wiring)["code"]})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Jonas"


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", "12 34 56", "", "-12345"])
def test_malformed_codes_are_validation_errors(client, code):
    register(client)
    assert client.post("/api/auth/consume-code",
                       json={"email": "jonas@example.org",
                             "code": code}).status_code == 422


def test_code_is_bound_to_its_own_account(client, wiring):
    register(client, name="Jonas", email="jonas@example.org")
    jonas = last(wiring)
    register(client, name="Meike", email="meike@example.org")

    # right code, wrong address
    assert client.post("/api/auth/consume-code",
                       json={"email": "meike@example.org",
                             "code": jonas["code"]}).status_code == 401
    # right code, address with no account at all
    assert client.post("/api/auth/consume-code",
                       json={"email": "ghost@example.org",
                             "code": jonas["code"]}).status_code == 401
    assert client.post("/api/auth/consume-code",
                       json={"email": "jonas@example.org",
                             "code": jonas["code"]}).status_code == 200


def test_using_the_code_also_kills_the_link(client, wiring):
    register(client)
    pending = last(wiring)
    assert client.post("/api/auth/consume-code",
                       json={"email": "jonas@example.org",
                             "code": pending["code"]}).status_code == 200
    assert client.post("/api/auth/consume",
                       json={"token": pending["token"]}).status_code == 401


def wrong(code):
    return f"{(int(code) + 1) % 10 ** auth.CODE_DIGITS:0{auth.CODE_DIGITS}d}"


def test_the_attempt_budget_voids_the_login(client, wiring):
    register(client)
    pending = last(wiring)
    for _ in range(auth.MAX_TRIES):
        assert client.post("/api/auth/consume-code",
                           json={"email": "jonas@example.org",
                                 "code": wrong(pending["code"])}).status_code == 401
    # correct code and emailed link are both dead now
    assert client.post("/api/auth/consume-code",
                       json={"email": "jonas@example.org",
                             "code": pending["code"]}).status_code == 401
    assert client.post("/api/auth/consume",
                       json={"token": pending["token"]}).status_code == 401


def test_guessing_one_account_does_not_spend_anothers_budget(client, wiring):
    register(client, name="Jonas", email="jonas@example.org")
    jonas = last(wiring)
    register(client, name="Meike", email="meike@example.org")
    meike = last(wiring)
    for _ in range(auth.MAX_TRIES):
        client.post("/api/auth/consume-code",
                    json={"email": "jonas@example.org", "code": wrong(jonas["code"])})
    assert client.post("/api/auth/consume-code",
                       json={"email": "meike@example.org",
                             "code": meike["code"]}).status_code == 200


# --- sessions ---------------------------------------------------------------

def login(client, wiring, name="Jonas", email="jonas@example.org"):
    register(client, name=name, email=email)
    client.post("/api/auth/consume", json={"token": last(wiring)["token"]})


def test_writing_needs_a_session(client, wiring):
    story = {"from_station": "Berlin Hbf", "title": "Stranded", "text": "x" * 20}
    assert client.post("/api/stories", json=story).status_code == 401
    assert client.get("/api/auth/me").json()["name"] is None
    # reading stays anonymous
    assert client.get("/api/stories").status_code == 200

    login(client, wiring)
    assert client.post("/api/stories", json=story).status_code == 201
    assert client.get("/api/auth/me").json()["name"] == "Jonas"


def test_logout_invalidates_the_session_everywhere(client, wiring):
    login(client, wiring)
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").json()["name"] is None
    assert client.post("/api/stories",
                       json={"from_station": "Berlin Hbf", "title": "abc",
                             "text": "x" * 20}).status_code == 401


def test_a_forged_cookie_is_not_a_session(client):
    client.cookies.set(main.SESSION_COOKIE, "made-up-token")
    assert client.get("/api/auth/me").json()["name"] is None


def test_posts_are_attributed_to_the_session_not_the_payload(client, wiring):
    login(client, wiring)
    created = client.post("/api/stories",
                          json={"from_station": "Berlin Hbf", "title": "Mine",
                                "text": "x" * 20, "author": "SomeoneElse"}).json()
    assert created["author"] == "Jonas"


# --- injection and abuse ----------------------------------------------------

def test_sql_metacharacters_are_data_not_syntax(client):
    # the name pattern rejects these outright; the email path takes them as a
    # value, so the users table must still be standing afterwards
    assert register(client, name="a'; DROP TABLE users;--").status_code == 422
    request_link(client, "'; DROP TABLE users;--@example.org")
    assert register(client, name="Jonas", email="jonas@example.org").status_code == 202


def test_register_is_rate_limited_per_ip(client, wiring):
    wiring.realistic(auth.register_limiter, burst=3, sustained=10)
    codes = [register(client, name=f"User{i}", email=f"u{i}@example.org").status_code
             for i in range(6)]
    assert codes.count(202) == 3
    assert codes.count(429) == 3


def test_code_submission_is_rate_limited_per_ip(client, wiring):
    register(client)
    pending = last(wiring)
    wiring.realistic(auth.code_limiter, burst=10, sustained=50)
    codes = [client.post("/api/auth/consume-code",
                         json={"email": "jonas@example.org",
                               "code": wrong(pending["code"])}).status_code
             for i in range(12)]
    assert 429 in codes, codes


# --- concurrency ------------------------------------------------------------

def test_concurrent_signups_on_one_name_yield_one_account():
    """Two racing registrations, same name, different addresses: SQLite's
    unique index has to break the tie, not application-level checking."""
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(auth.register, "Racer", f"racer{i}@example.org")
                   for i in range(2)]
        results = [f.result() for f in futures]
    assert sum(r is not None for r in results) == 1
    with sqlite3.connect(stories.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_concurrent_signups_on_one_email_yield_one_account():
    """Same address from two names at once. Whoever loses must not create a
    duplicate row - the partial unique index on email is what enforces it."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(auth.register, f"Racer{i}", "shared@example.org")
                   for i in range(2)]
        results = [f.result() for f in futures]
    assert sum(r is not None for r in results) >= 1
    with sqlite3.connect(stories.DB_PATH) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM users WHERE email = 'shared@example.org'"
        ).fetchone()[0] == 1


def test_concurrent_redemption_of_one_link_opens_one_session(client, wiring):
    register(client)
    token = last(wiring)["token"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [f.result() for f in
                   [pool.submit(auth.consume, token) for _ in range(4)]]
    assert sum(r is not None for r in results) == 1


# --- account lifecycle ------------------------------------------------------

def test_purge_frees_a_squatted_name_but_spares_verified_accounts(client, wiring, monkeypatch):
    login(client, wiring, name="Kept", email="kept@example.org")
    register(client, name="Squatter", email="squatter@example.org")

    monkeypatch.setattr(auth, "UNVERIFIED_DAYS", -1)
    register(client, name="Trigger", email="trigger@example.org")

    assert auth.request_link("squatter@example.org") is None
    assert auth.request_link("kept@example.org") is not None
    # the abandoned name is available again
    assert register(client, name="Squatter", email="new@example.org").status_code == 202


def test_suggested_name_is_free_and_registers(client):
    """What the endpoint hands out has to survive the register validator and
    the uniqueness check - it is offered as a name the visitor can just use."""
    resp = client.get("/api/auth/suggest-name?lang=en")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    name = resp.json()["name"]

    assert register(client, name=name).status_code == 202
    # now taken, so the next suggestion has to differ from it
    for _ in range(5):
        assert client.get("/api/auth/suggest-name?lang=en").json()["name"] != name


def test_suggest_name_rejects_an_unknown_language(client):
    assert client.get("/api/auth/suggest-name?lang=fr").status_code == 422


def test_suggest_name_is_rate_limited_per_ip(client, wiring):
    wiring.realistic(auth.suggest_limiter, burst=20, sustained=120)
    for _ in range(20):
        assert client.get("/api/auth/suggest-name").status_code == 200
    throttled = client.get("/api/auth/suggest-name")
    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"]


# --- editing and removing your own posts ------------------------------------

def _own_story(client, wiring, name="Jonas"):
    register(client, name=name, email=f"{name}@example.org")
    client.post("/api/auth/consume", json={"token": last(wiring)["token"]})
    return client.post("/api/stories", json={
        "from_station": "Berlin Hbf", "title": "Stranded", "text": "x" * 20,
    }).json()


def test_editing_needs_a_session(client, wiring):
    story = _own_story(client, wiring)
    client.post("/api/auth/logout")
    # a valid body on purpose: FastAPI validates before the session is read,
    # so a too-short title would 422 and never reach the check under test
    assert client.patch(f"/api/stories/{story['id']}",
                        json={"title": "Hijacked", "text": "y" * 20}).status_code == 401
    assert client.delete(f"/api/stories/{story['id']}").status_code == 401


def test_another_account_cannot_edit_or_delete_your_story(client, wiring):
    """The button is hidden for other people, but a hidden button is not a
    permission - the endpoint has to refuse it too."""
    story = _own_story(client, wiring, "Jonas")
    client.post("/api/auth/logout")
    register(client, name="Meike", email="meike@example.org")
    client.post("/api/auth/consume", json={"token": last(wiring)["token"]})

    assert client.patch(f"/api/stories/{story['id']}",
                        json={"title": "Mine", "text": "y" * 20}).status_code == 403
    assert client.delete(f"/api/stories/{story['id']}").status_code == 403
    assert client.get("/api/stories").json()[0]["title"] == "Stranded"


def test_the_author_edits_and_deletes_over_http(client, wiring):
    story = _own_story(client, wiring)
    edited = client.patch(f"/api/stories/{story['id']}",
                          json={"title": "Reworded", "text": "y" * 20})
    assert edited.status_code == 200
    assert (edited.json()["title"], edited.json()["edited"]) == ("Reworded", True)
    assert client.delete(f"/api/stories/{story['id']}").status_code == 204
    assert client.get("/api/stories").json() == []


def test_comment_votes_and_ownership_over_http(client, wiring):
    story = _own_story(client, wiring)
    comment = client.post(f"/api/stories/{story['id']}/comments",
                          json={"text": "mine"}).json()

    voted = client.post(f"/api/comments/{comment['id']}/vote", json={"vote": True})
    assert voted.json() == {"score": 1, "voted": True}
    assert client.get(f"/api/stories/{story['id']}/comments").json()[0]["voted"] is True

    assert client.patch(f"/api/comments/{comment['id']}",
                        json={"text": "reworded"}).json()["text"] == "reworded"
    assert client.delete(f"/api/comments/{comment['id']}").status_code == 204
    assert client.get(f"/api/stories/{story['id']}/comments").json() == []


def test_voting_on_a_missing_comment_is_a_404(client, wiring):
    _own_story(client, wiring)
    assert client.post("/api/comments/999/vote", json={"vote": True}).status_code == 404


# --- the tally board --------------------------------------------------------

def test_problem_counts_are_public_and_validated(client):
    stories.create_story("Hannover Hbf", "", "", "", ["delay", "wifi"], "",
                         "Stuck again", "x" * 20, "Max")
    resp = client.get("/api/stories/problems")
    assert resp.status_code == 200
    board = resp.json()
    assert board["counts"]["delay"] == 1 and board["counts"]["wc"] == 0
    assert board["mine"] == []  # no session: no tile is the viewer's
    assert client.get("/api/stories/problems?span=all").status_code == 200
    assert client.get("/api/stories/problems?span=fortnight").status_code == 422


def test_tapping_a_tile_needs_a_session_and_answers_with_the_board(client, wiring):
    leg = {"vote": True, "from_station": "Hannover Hbf", "to_station": "Berlin Hbf",
           "departure": "2026-08-23T09:11", "train": "ICE 574"}
    assert client.post("/api/stories/problems/delay", json=leg).status_code == 401
    login(client, wiring)
    resp = client.post("/api/stories/problems/delay?span=week", json=leg)
    assert resp.status_code == 200
    assert resp.json()["counts"]["delay"] == 1
    assert resp.json()["mine"] == ["delay"]
    # the GET now knows the tile is this viewer's
    assert client.get("/api/stories/problems").json()["mine"] == ["delay"]
    # a second tap is the same tap, and the toggle takes it back
    again = client.post("/api/stories/problems/delay", json=leg).json()
    assert again["counts"]["delay"] == 1
    off = client.post("/api/stories/problems/delay", json={"vote": False}).json()
    assert off == {"counts": dict.fromkeys(stories.PROBLEMS, 0), "mine": []}
    assert client.post("/api/stories/problems/teleported", json=leg).status_code == 404
    assert client.post("/api/stories/problems/delay?span=fortnight",
                       json=leg).status_code == 422


def test_a_tap_needs_a_leg_but_taking_it_back_does_not(client, wiring):
    login(client, wiring)
    # no origin: nothing to count against
    assert client.post("/api/stories/problems/delay", json={"vote": True}).status_code == 422
    assert client.post("/api/stories/problems/delay",
                       json={"vote": True, "from_station": "H"}).status_code == 422
    assert client.post("/api/stories/problems/delay",
                       json={"vote": True, "from_station": "Hannover Hbf",
                             "departure": "yesterday"}).status_code == 422
    assert client.get("/api/stories/problems").json()["counts"]["delay"] == 0
    # the origin alone is a leg
    assert client.post("/api/stories/problems/delay",
                       json={"vote": True, "from_station": "Hannover Hbf"}).status_code == 200
    # "other" has to say what, the rest must not
    assert client.post("/api/stories/problems/other",
                       json={"vote": True, "from_station": "Hannover Hbf"}).status_code == 422
    assert client.post("/api/stories/problems/other",
                       json={"vote": True, "from_station": "Hannover Hbf",
                             "problem_other": "doors froze"}).status_code == 200
    assert client.post("/api/stories/problems/other",
                       json={"vote": True, "from_station": "Hannover Hbf",
                             "problem_other": "x" * 81}).status_code == 422
    # clearing names no leg
    assert client.post("/api/stories/problems/delay", json={"vote": False}).status_code == 200
    assert client.get("/api/stories/problems").json()["mine"] == ["other"]


def test_stories_page_has_one_url_per_language(client):
    de = client.get("/geschichten")
    en = client.get("/stories")
    assert de.status_code == en.status_code == 200
    assert '<html lang="de">' in de.text and '<html lang="en">' in en.text
    assert '<link rel="canonical" href="https://delaybahn.com/geschichten">' in de.text
    assert '<link rel="canonical" href="https://delaybahn.com/stories">' in en.text
    # each variant points at the other and keeps its own navigation in-language
    for page in (de, en):
        assert 'hreflang="de" href="https://delaybahn.com/geschichten"' in page.text
        assert 'hreflang="en" href="https://delaybahn.com/stories"' in page.text
    assert '<a class="logo-link" href="/geschichten">' in de.text
    assert '<a class="logo-link" href="/stories">' in en.text
    assert '<a href="/en/" data-i18n="footerBack">' in en.text
    # English text is in the markup, not only after the script runs
    assert "<title>Delay Stories – DelayBahn</title>" in en.text
    assert 'data-i18n="sortLiked">Most liked<' in en.text
    assert 'data-i18n="sortLiked">Beliebteste<' in de.text
    assert 'data-lang="en" class="lang-btn active"' in en.text
    # the old spellings still land somewhere
    for path in ("/stories/", "/stories.html", "/geschichten/"):
        assert client.get(path, follow_redirects=False).status_code == 301


def test_home_footer_links_to_the_stories_page_in_its_language(client):
    assert 'href="/geschichten" data-i18n="footerStories"' in client.get("/").text
    assert 'href="/stories" data-i18n="footerStories"' in client.get("/en/").text
