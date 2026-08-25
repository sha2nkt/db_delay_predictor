"""The account contract over HTTP: a Firebase ID token as bearer, the two
403s that name a missing step, the one-time username claim, and the stories
endpoints attributing everything to the token. test_auth.py covers the auth
module directly; this drives the real endpoints, so it also pins the status
codes and validation rules the pages depend on. Firebase is the `firebase`
fixture from conftest.py - no token is ever really verified."""

import re

import pytest
from fastapi.testclient import TestClient

from app import auth, main, stories

LIMITERS = (
    auth.register_limiter, auth.suggest_limiter,
    stories.write_limiter, stories.vote_limiter,
)


@pytest.fixture(autouse=True)
def wiring(firebase, monkeypatch):
    """Fake Firebase, no background pushes, and a clean rate-limit budget
    per test - the limiters are process-global singletons and would
    otherwise leak state between tests."""

    def fake_spawn(coro):
        coro.close()  # nothing awaits background tasks under TestClient

    monkeypatch.setattr(main, "_spawn", fake_spawn)
    # every request in a test shares one client IP, so the real per-IP
    # budgets would fire early; the tests that are *about* rate limiting
    # restore them with realistic()
    for limiter in LIMITERS:
        limiter._hits.clear()
        monkeypatch.setattr(limiter, "_burst_limit", 10_000)
        monkeypatch.setattr(limiter, "_sustained_limit", 10_000)

    def realistic(limiter, burst, sustained):
        limiter._hits.clear()
        monkeypatch.setattr(limiter, "_burst_limit", burst)
        monkeypatch.setattr(limiter, "_sustained_limit", sustained)

    firebase.realistic = realistic
    return firebase


@pytest.fixture
def client():
    # No context manager: the lifespan would load the delays DuckDB, which
    # none of these endpoints touch.
    return TestClient(main.app)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def login(wiring, name="Jonas", uid=None, **kw):
    """A finished account - verified, name claimed, token carrying the
    claim - as the headers to send. Same uid twice is the same account."""
    uid = uid or f"u-{name.lower()}"
    if uid not in wiring.registry.users:
        assert auth.claim_handle(uid, name) == "ok"
    return bearer(wiring.token(f"tok-{uid}", uid=uid, handle=name, **kw))


STORY = {"from_station": "Berlin Hbf", "title": "Stranded", "text": "x" * 20}


def _own_story(client, headers):
    resp = client.post("/api/stories", json=STORY, headers=headers)
    assert resp.status_code == 201
    return resp.json()


# --- claiming a username ------------------------------------------------------

@pytest.mark.parametrize("name", [
    "a",                    # too short
    "x" * 26,               # too long
    "has space",
    "Jönas",                # non-ASCII
    "semi;colon",
    "<script>",
    "a'; DROP TABLE users;--",
    "",
])
def test_invalid_usernames_are_rejected(client, wiring, name):
    headers = bearer(wiring.token("t", uid="u1"))
    resp = client.post("/api/auth/handle", json={"name": name}, headers=headers)
    assert resp.status_code == 422
    assert wiring.stamped == {}


@pytest.mark.parametrize("name", ["Jo-nas", "Jo_nas", "AB", "x" * 25, "12345"])
def test_valid_usernames_are_accepted(client, wiring, name):
    headers = bearer(wiring.token("t", uid="u1"))
    resp = client.post("/api/auth/handle", json={"name": name}, headers=headers)
    assert resp.status_code == 201 and resp.json() == {"name": name}
    assert wiring.stamped == {"u1": name}


def test_a_missing_name_is_a_validation_error(client, wiring):
    headers = bearer(wiring.token("t", uid="u1"))
    assert client.post("/api/auth/handle", json={}, headers=headers).status_code == 422


def test_claiming_needs_a_login_with_proven_contact(client, wiring):
    assert client.post("/api/auth/handle", json={"name": "Jonas"}).status_code == 401
    fresh = bearer(wiring.token("fresh", uid="u1", provider="password", verified=False))
    resp = client.post("/api/auth/handle", json={"name": "Jonas"}, headers=fresh)
    assert (resp.status_code, resp.json()["detail"]) == (403, "unverified")
    assert wiring.stamped == {}
    # an SMS-confirmed number is contact enough
    phone = bearer(wiring.token("phone", uid="u2", provider="phone"))
    assert client.post("/api/auth/handle", json={"name": "Jonas"},
                       headers=phone).status_code == 201


def test_taken_name_conflicts_regardless_of_case(client, wiring):
    first = bearer(wiring.token("t1", uid="u1"))
    assert client.post("/api/auth/handle", json={"name": "Jonas"}, headers=first).status_code == 201
    second = bearer(wiring.token("t2", uid="u2"))
    for name in ("Jonas", "JONAS", "jonas"):
        resp = client.post("/api/auth/handle", json={"name": name}, headers=second)
        assert (resp.status_code, resp.json()["detail"]) == (409, "taken")
    assert "u2" not in wiring.stamped


def test_an_account_claims_exactly_one_name(client, wiring):
    before = bearer(wiring.token("t1", uid="u1"))  # a token from before the claim
    assert client.post("/api/auth/handle", json={"name": "Jonas"}, headers=before).status_code == 201
    # the same stale token again: the registry knows the account is named
    resp = client.post("/api/auth/handle", json={"name": "Other"}, headers=before)
    assert (resp.status_code, resp.json()["detail"]) == (409, "named")
    # a refreshed token carries the claim, so the answer needs no registry
    after = bearer(wiring.token("t2", uid="u1", handle="Jonas"))
    resp = client.post("/api/auth/handle", json={"name": "Other"}, headers=after)
    assert (resp.status_code, resp.json()["detail"]) == (409, "named")
    assert wiring.stamped == {"u1": "Jonas"}
    assert wiring.registry.taken(["other"]) == set()


def test_handle_claims_are_rate_limited_per_ip(client, wiring):
    wiring.realistic(auth.register_limiter, burst=3, sustained=10)
    codes = []
    for i in range(5):
        headers = bearer(wiring.token(f"t{i}", uid=f"u{i}"))
        resp = client.post("/api/auth/handle", json={"name": f"User{i}"}, headers=headers)
        codes.append(resp.status_code)
    assert codes == [201, 201, 201, 429, 429]
    assert "retry-after" in {k.lower() for k in resp.headers}


# --- the token ------------------------------------------------------------------

def test_a_bad_bearer_is_refused_even_for_reading(client, wiring):
    assert client.get("/api/stories").status_code == 200
    assert client.get("/api/stories", headers=bearer("never-issued")).status_code == 401
    assert client.get("/api/auth/me", headers=bearer("never-issued")).status_code == 401
    # only the bearer scheme is a login attempt; anything else is not ours
    assert client.get("/api/stories", headers={"Authorization": "Basic abc"}).status_code == 200
    assert client.get("/api/stories", headers={"Authorization": "Bearer "}).status_code == 200


def test_writing_needs_a_finished_account(client, wiring):
    assert client.post("/api/stories", json=STORY).status_code == 401
    assert client.get("/api/auth/me").json() == {"name": None}

    fresh = bearer(wiring.token("fresh", uid="u1", provider="password", verified=False))
    resp = client.post("/api/stories", json=STORY, headers=fresh)
    assert (resp.status_code, resp.json()["detail"]) == (403, "unverified")

    unnamed = bearer(wiring.token("unnamed", uid="u1", provider="password"))
    resp = client.post("/api/stories", json=STORY, headers=unnamed)
    assert (resp.status_code, resp.json()["detail"]) == (403, "unnamed")
    assert client.get("/api/auth/me", headers=unnamed).json() == {
        "name": None, "uid": "u1", "verified": True,
    }

    done = login(wiring, "Jonas", uid="u1", provider="password")
    assert client.post("/api/stories", json=STORY, headers=done).status_code == 201
    assert client.get("/api/auth/me", headers=done).json()["name"] == "Jonas"


def test_posts_are_attributed_to_the_token_not_the_payload(client, wiring):
    headers = login(wiring)
    created = client.post("/api/stories", json={**STORY, "author": "SomeoneElse"},
                          headers=headers).json()
    assert created["author"] == "Jonas"


def test_votes_count_once_per_account_over_http(client, wiring):
    jonas, meike = login(wiring, "Jonas"), login(wiring, "Meike")
    story = _own_story(client, jonas)
    path = f"/api/stories/{story['id']}/vote"
    assert client.post(path, json={"vote": 1}, headers=jonas).json() == {"score": 1, "voted": 1}
    assert client.post(path, json={"vote": 1}, headers=jonas).json()["score"] == 1
    assert client.post(path, json={"vote": -1}, headers=meike).json() == {"score": 0, "voted": -1}
    # the lists mark the viewer's own vote, and nobody else's
    assert client.get("/api/stories", headers=jonas).json()[0]["voted"] == 1
    assert client.get("/api/stories", headers=meike).json()[0]["voted"] == -1
    assert client.get("/api/stories").json()[0]["voted"] == 0


def test_a_firebase_outage_is_a_503_not_a_login_failure(client, wiring, monkeypatch):
    def down(*_a, **_k):
        raise auth.AuthUnavailable("keys unreachable")

    headers = login(wiring)
    monkeypatch.setattr(auth, "_verify", down)
    assert client.post("/api/stories", json=STORY, headers=headers).status_code == 503
    assert client.get("/api/stories").status_code == 200  # reading needs no Firebase
    monkeypatch.setattr(auth, "_registry", down)
    assert client.get("/api/auth/suggest-name").status_code == 503


def test_health_reports_whether_accounts_are_configured(client, monkeypatch):
    monkeypatch.delenv("FIREBASE_SA_FILE", raising=False)
    assert client.get("/health").json()["auth"] == {"configured": False}
    monkeypatch.setenv("FIREBASE_SA_FILE", "/etc/delaybahn/firebase.json")
    assert client.get("/health").json()["auth"] == {"configured": True}


def test_the_old_login_routes_are_gone(client):
    assert client.get("/verify?token=abc").status_code == 404
    for path in ("/api/auth/register", "/api/auth/request-link", "/api/auth/consume",
                 "/api/auth/consume-code", "/api/auth/logout"):
        assert client.post(path, json={}).status_code in (404, 405), path


def test_the_login_page_offers_every_way_in(client):
    page = client.get("/login")
    assert page.status_code == 200
    for provider in ("google", "apple", "phone"):
        assert f'id="sso-{provider}"' in page.text
    assert 'id="email-form"' in page.text and 'id="name-form"' in page.text
    assert '<script type="module" src="/login.js?v=' in page.text
    assert client.get("/login/", follow_redirects=False).status_code == 301
    # the SDK entry point and its config are served as modules
    assert "export const auth" in client.get("/firebase.js").text
    assert "export const config" in client.get("/firebase-config.js").text


# --- suggested names ------------------------------------------------------------

HOUSE_FORMAT = re.compile(r"^[A-Za-z]{1,10}_[A-Za-z]{1,10}\d{2,4}$")


def test_suggested_name_is_free_and_claims(client, wiring):
    resp = client.get("/api/auth/suggest-name?lang=en")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    name = resp.json()["name"]
    assert HOUSE_FORMAT.match(name)
    headers = bearer(wiring.token("t", uid="u1"))
    assert client.post("/api/auth/handle", json={"name": name}, headers=headers).status_code == 201


def test_suggest_name_rejects_an_unknown_language(client):
    assert client.get("/api/auth/suggest-name?lang=fr").status_code == 422


def test_suggest_name_is_rate_limited_per_ip(client, wiring):
    wiring.realistic(auth.suggest_limiter, burst=20, sustained=120)
    codes = [client.get("/api/auth/suggest-name").status_code for _ in range(21)]
    assert codes == [200] * 20 + [429]


# --- editing and removal ------------------------------------------------------

def test_editing_needs_a_login(client, wiring):
    story = _own_story(client, login(wiring))
    # a valid body on purpose: FastAPI validates before the token is read,
    # so a too-short title would 422 and never reach the check under test
    assert client.patch(f"/api/stories/{story['id']}",
                        json={"title": "Hijacked", "text": "y" * 20}).status_code == 401
    assert client.delete(f"/api/stories/{story['id']}").status_code == 401


def test_another_account_cannot_edit_or_delete_your_story(client, wiring):
    """The button is hidden for other people, but a hidden button is not a
    permission - the endpoint has to refuse it too."""
    story = _own_story(client, login(wiring, "Jonas"))
    meike = login(wiring, "Meike")
    assert client.patch(f"/api/stories/{story['id']}", headers=meike,
                        json={"title": "Mine", "text": "y" * 20}).status_code == 403
    assert client.delete(f"/api/stories/{story['id']}", headers=meike).status_code == 403
    assert client.get("/api/stories").json()[0]["title"] == "Stranded"


def test_the_author_edits_and_deletes_over_http(client, wiring):
    jonas = login(wiring)
    story = _own_story(client, jonas)
    edited = client.patch(f"/api/stories/{story['id']}", headers=jonas,
                          json={"title": "Reworded", "text": "y" * 20})
    assert edited.status_code == 200
    assert (edited.json()["title"], edited.json()["edited"]) == ("Reworded", True)
    assert client.delete(f"/api/stories/{story['id']}", headers=jonas).status_code == 204
    assert client.get("/api/stories").json() == []


def test_comment_votes_and_ownership_over_http(client, wiring):
    jonas = login(wiring)
    story = _own_story(client, jonas)
    comment = client.post(f"/api/stories/{story['id']}/comments",
                          json={"text": "mine"}, headers=jonas).json()

    # a pre-downvote cached script still sends booleans; True must mean +1
    voted = client.post(f"/api/comments/{comment['id']}/vote", json={"vote": True},
                        headers=jonas)
    assert voted.json() == {"score": 1, "voted": 1}
    assert client.get(f"/api/stories/{story['id']}/comments",
                      headers=jonas).json()[0]["voted"] == 1
    down = client.post(f"/api/comments/{comment['id']}/vote", json={"vote": -1},
                       headers=jonas)
    assert down.json() == {"score": -1, "voted": -1}

    assert client.patch(f"/api/comments/{comment['id']}", json={"text": "reworded"},
                        headers=jonas).json()["text"] == "reworded"
    assert client.delete(f"/api/comments/{comment['id']}", headers=jonas).status_code == 204
    assert client.get(f"/api/stories/{story['id']}/comments").json() == []


def test_voting_on_a_missing_comment_is_a_404(client, wiring):
    jonas = login(wiring)
    _own_story(client, jonas)
    assert client.post("/api/comments/999/vote", json={"vote": True},
                       headers=jonas).status_code == 404


# --- the tally board --------------------------------------------------------

def test_problem_counts_are_public_and_validated(client):
    stories.create_story("Hannover Hbf", "", "", "", ["delay", "wifi"], "",
                         "Stuck again", "x" * 20, "Max")
    resp = client.get("/api/stories/problems")
    assert resp.status_code == 200
    board = resp.json()
    assert board["counts"]["delay"] == 1 and board["counts"]["wc"] == 0
    assert board["mine"] == []  # not logged in: no tile is the viewer's
    assert client.get("/api/stories/problems?span=all").status_code == 200
    assert client.get("/api/stories/problems?span=fortnight").status_code == 422


def test_tapping_a_tile_needs_a_login_and_answers_with_the_board(client, wiring):
    leg = {"vote": True, "from_station": "Hannover Hbf", "to_station": "Berlin Hbf",
           "departure": "2026-08-23T09:11", "train": "ICE 574"}
    assert client.post("/api/stories/problems/delay", json=leg).status_code == 401
    jonas = login(wiring)
    resp = client.post("/api/stories/problems/delay?span=week", json=leg, headers=jonas)
    assert resp.status_code == 200
    assert resp.json()["counts"]["delay"] == 1
    assert resp.json()["mine"] == ["delay"]
    # the GET now knows the tile is this viewer's
    assert client.get("/api/stories/problems", headers=jonas).json()["mine"] == ["delay"]
    # a second tap is the same tap, and the toggle takes it back
    again = client.post("/api/stories/problems/delay", json=leg, headers=jonas).json()
    assert again["counts"]["delay"] == 1
    off = client.post("/api/stories/problems/delay", json={"vote": False},
                      headers=jonas).json()
    assert off == {"counts": dict.fromkeys(stories.PROBLEMS, 0), "mine": []}
    assert client.post("/api/stories/problems/teleported", json=leg,
                       headers=jonas).status_code == 404
    assert client.post("/api/stories/problems/delay?span=fortnight",
                       json=leg, headers=jonas).status_code == 422


def test_a_tap_needs_a_leg_but_taking_it_back_does_not(client, wiring):
    jonas = login(wiring)
    tap = lambda code, body: client.post(f"/api/stories/problems/{code}", json=body,
                                         headers=jonas).status_code
    # no origin: nothing to count against
    assert tap("delay", {"vote": True}) == 422
    assert tap("delay", {"vote": True, "from_station": "H"}) == 422
    assert tap("delay", {"vote": True, "from_station": "Hannover Hbf",
                         "departure": "yesterday"}) == 422
    assert client.get("/api/stories/problems").json()["counts"]["delay"] == 0
    # the origin alone is a leg
    assert tap("delay", {"vote": True, "from_station": "Hannover Hbf"}) == 200
    # "other" has to say what, the rest must not
    assert tap("other", {"vote": True, "from_station": "Hannover Hbf"}) == 422
    assert tap("other", {"vote": True, "from_station": "Hannover Hbf",
                         "problem_other": "doors froze"}) == 200
    assert tap("other", {"vote": True, "from_station": "Hannover Hbf",
                         "problem_other": "x" * 81}) == 422
    # clearing names no leg
    assert tap("delay", {"vote": False}) == 200
    assert client.get("/api/stories/problems", headers=jonas).json()["mine"] == ["other"]


# --- the pages --------------------------------------------------------------

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


def test_a_story_has_a_permalink_page_in_each_language(client, wiring):
    jonas = login(wiring)
    story = _own_story(client, jonas)
    sid = story["id"]
    client.patch(f"/api/stories/{sid}", headers=jonas,
                 json={"title": 'Gleis 9 & "Nacht"', "text": "Zwanzig Minuten " * 20})
    de = client.get(f"/geschichten/{sid}")
    en = client.get(f"/stories/{sid}")
    assert de.status_code == en.status_code == 200
    # the story, not the board, is what a shared link unfurls as - user text escaped
    assert "<title>Gleis 9 &amp; &quot;Nacht&quot; – Delay Geschichten</title>" in de.text
    assert "<title>Gleis 9 &amp; &quot;Nacht&quot; – Delay Stories</title>" in en.text
    assert '<meta property="og:type" content="article">' in de.text
    assert '<meta property="og:description" content="Zwanzig Minuten Zwanzig' in de.text
    assert "Minuten…\">" in de.text  # cut at a word boundary, not mid-word
    assert f'<link rel="canonical" href="https://delaybahn.com/geschichten/{sid}">' in de.text
    assert f'<link rel="canonical" href="https://delaybahn.com/stories/{sid}">' in en.text
    for page in (de, en):
        assert f'hreflang="de" href="https://delaybahn.com/geschichten/{sid}"' in page.text
        assert f'hreflang="en" href="https://delaybahn.com/stories/{sid}"' in page.text
        assert f'<a href="/stories/{sid}" hreflang="en"' in page.text
    # the rest of the page is the board in that language, as before
    assert '<html lang="en">' in en.text
    assert '<a class="logo-link" href="/stories">' in en.text
    assert 'data-i18n="permalinkHead">Shared story<' in en.text
    # a dead link is a 404 that still carries the page, not a JSON error
    gone = client.get("/stories/999")
    assert gone.status_code == 404 and 'data-i18n="permalinkHead"' in gone.text
    assert client.get("/stories/abc").status_code == 404


def test_a_story_embeds_as_a_standalone_card(client, wiring):
    jonas = login(wiring)
    story = _own_story(client, jonas)
    sid = story["id"]
    client.post(f"/api/stories/{sid}/comments", json={"text": "same here"}, headers=jonas)
    tagged = client.post("/api/stories", headers=jonas, json={
        "from_station": "Hannover Hbf", "to_station": "Berlin Hbf", "title": "Tagged",
        "text": "y" * 20, "problems": ["delay", "other"], "problem_other": "Tür klemmte",
    }).json()
    en = client.get(f"/embed/stories/{sid}")
    de = client.get(f"/embed/geschichten/{sid}")
    assert en.status_code == de.status_code == 200
    # meant for other sites' iframes: frameable, unindexed, links open the parent
    assert "x-frame-options" not in {k.lower() for k in en.headers}
    assert '<meta name="robots" content="noindex">' in en.text
    assert '<base target="_top">' in en.text
    assert f'<h1><a href="https://delaybahn.com/stories/{sid}">Stranded</a></h1>' in en.text
    assert f'<h1><a href="https://delaybahn.com/geschichten/{sid}">Stranded</a></h1>' in de.text
    assert "1 comment" in en.text and "1 Kommentar" in de.text
    assert "Read on DelayBahn" in en.text and "Auf DelayBahn lesen" in de.text
    assert "Jonas" in en.text
    tags_en = client.get(f"/embed/stories/{tagged['id']}").text
    tags_de = client.get(f"/embed/geschichten/{tagged['id']}").text
    assert "Hannover Hbf → Berlin Hbf" in tags_en
    assert '<span class="tag">Delayed</span><span class="tag">Tür klemmte</span>' in tags_en
    assert '<span class="tag">Verspätung</span>' in tags_de
    assert client.get("/embed/stories/999").status_code == 404
    # a removed story has nothing to embed, tombstone or not
    client.delete(f"/api/stories/{sid}", headers=jonas)
    assert client.get(f"/embed/stories/{sid}").status_code == 404


def test_a_single_story_is_fetchable_by_id(client, wiring):
    jonas = login(wiring)
    story = _own_story(client, jonas)
    client.post(f"/api/stories/{story['id']}/vote", json={"vote": 1}, headers=jonas)
    one = client.get(f"/api/stories/{story['id']}", headers=jonas)
    assert one.status_code == 200
    assert (one.json()["title"], one.json()["voted"]) == ("Stranded", 1)
    assert client.get(f"/api/stories/{story['id']}").json()["voted"] == 0
    assert client.get("/api/stories/999").status_code == 404
    # the fixed problems path still wins over the id route
    assert client.get("/api/stories/problems").status_code == 200


def test_home_footer_links_to_the_stories_page_in_its_language(client):
    assert 'href="/geschichten" data-i18n="footerStories"' in client.get("/").text
    assert 'href="/stories" data-i18n="footerStories"' in client.get("/en/").text
