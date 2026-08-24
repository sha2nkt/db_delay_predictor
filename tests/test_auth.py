"""Passwordless accounts: link and code are two halves of one login - single
use, expiring, and either one kills the other. Registering an already-used
email quietly turns into a login for that account (no enumeration), names stay
unique case-insensitively, sessions round-trip until they expire or are logged
out, and unconfirmed accounts are purged."""

import re

import pytest

from app import auth, stories


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(stories, "DB_PATH", tmp_path / "stories.db")
    # the per-account resend cooldown would otherwise swallow the second link
    # in almost every test here; test_auth_api.py covers it deliberately
    monkeypatch.setattr(auth, "RESEND_COOLDOWN_SECONDS", 0)


def register(name="Jonas", email="jonas@example.org"):
    return auth.register(name, email)


def login_user(name="Jonas", email="jonas@example.org"):
    """Register + consume: the account and a live session."""
    _kind, _name, magic, _code = register(name, email)
    return auth.consume(magic)


def test_register_consume_and_session():
    kind, name, magic, code = register()
    assert (kind, name) == ("new", "Jonas")
    assert len(code) == auth.CODE_DIGITS and code.isdigit()

    account, session = auth.consume(magic)
    assert account["name"] == "Jonas"
    assert auth.session_user(session) == account
    # consumed: the same link cannot log in twice
    assert auth.consume(magic) is None
    # ... and the code half of that same login died with it
    assert auth.consume_code("jonas@example.org", code) is None


def test_consume_rejects_garbage_and_empty():
    register()
    assert auth.consume("not-a-token") is None
    assert auth.consume("") is None


def test_names_are_unique_case_insensitively():
    register()
    assert register("jonas", "other@example.org") is None


def test_existing_email_gets_login_link_not_second_account():
    register()
    # same address, any casing: no second account is ever created
    kind, _name, magic, _code = register("Renamed", "JONAS@example.org")
    assert kind == "existing"
    account, _session = auth.consume(magic)
    assert account["id"] == 1
    assert auth.request_link("renamed@example.org") is None  # never created


def test_reregistering_renames_an_unconfirmed_account_on_redemption():
    """The typo-at-signup fix: the name changes, but only once the emailed
    login is redeemed - proposing it is not the same as being allowed it."""
    register("Jonsa", "jonas@example.org")          # typo
    _kind, greeted, magic, _code = register("Jonas", "jonas@example.org")
    assert greeted == "Jonas"                        # the mail uses the new name
    account, _session = auth.consume(magic)
    assert account["name"] == "Jonas"
    assert auth.delete_account("Jonsa") is False     # the old name is gone


def test_a_confirmed_account_is_never_renamed():
    login_user("Jonas", "jonas@example.org")         # consumed = verified
    _kind, name, magic, _code = register("SomethingElse", "jonas@example.org")
    assert name == "Jonas"
    account, _session = auth.consume(magic)
    assert account["name"] == "Jonas"


def test_a_rename_onto_a_taken_name_is_refused():
    register("Taken", "taken@example.org")
    register("Jonas", "jonas@example.org")
    assert register("Taken", "jonas@example.org") is None


def test_request_link_round_trip_and_unknown_email():
    login_user()
    # None means "no such account" and nothing else - the caller turns it into
    # "create one", so a throttled account must not land here
    assert auth.request_link("nobody@example.org") is None
    name, magic, _code = auth.request_link("Jonas@Example.org")
    assert name == "Jonas"
    account, session = auth.consume(magic)
    assert account["name"] == "Jonas"
    assert auth.session_user(session)["name"] == "Jonas"


def test_a_throttled_account_still_returns_its_name(monkeypatch):
    """Cooldown withholds the mail, not the account: request_link reports the
    account with no token rather than the None that means "unknown address"."""
    monkeypatch.setattr(auth, "RESEND_COOLDOWN_SECONDS", 60)
    login_user()
    name, magic, code = auth.request_link("jonas@example.org")
    assert (name, magic, code) == ("Jonas", None, None)


def test_new_link_invalidates_the_old_one():
    _kind, _name, old, old_code = register()
    _stored, new, _new_code = auth.request_link("jonas@example.org")
    assert auth.consume(old) is None
    assert auth.consume_code("jonas@example.org", old_code) is None
    assert auth.consume(new) is not None


def test_expired_link_is_rejected(monkeypatch):
    monkeypatch.setattr(auth, "MAGIC_LINK_HOURS", -1)
    _kind, _name, magic, code = register()
    assert auth.consume(magic) is None
    assert auth.consume_code("jonas@example.org", code) is None


def test_code_logs_in_and_is_single_use():
    _kind, _name, magic, code = register()
    account, session = auth.consume_code("Jonas@Example.org", code)
    assert account["name"] == "Jonas"
    assert auth.session_user(session)["name"] == "Jonas"
    # the whole login is spent, link half included
    assert auth.consume_code("jonas@example.org", code) is None
    assert auth.consume(magic) is None


def test_code_needs_the_matching_address():
    _kind, _name, _magic, code = register()
    register("Other", "other@example.org")
    assert auth.consume_code("other@example.org", code) is None
    assert auth.consume_code("nobody@example.org", code) is None
    assert auth.consume_code("jonas@example.org", code) is not None


def wrong_code(code):
    return f"{(int(code) + 1) % 10 ** auth.CODE_DIGITS:0{auth.CODE_DIGITS}d}"


def test_wrong_codes_burn_the_attempt_budget():
    _kind, _name, _magic, code = register()
    for _ in range(auth.MAX_TRIES - 1):
        assert auth.consume_code("jonas@example.org", wrong_code(code)) is None
    # the budget is not yet spent, so the right code still works
    assert auth.consume_code("jonas@example.org", code) is not None


def test_exhausting_the_budget_voids_the_whole_login():
    _kind, _name, magic, code = register()
    wrong = wrong_code(code)
    for _ in range(auth.MAX_TRIES):
        assert auth.consume_code("jonas@example.org", wrong) is None
    # even the correct code and the emailed link are dead now
    assert auth.consume_code("jonas@example.org", code) is None
    assert auth.consume(magic) is None
    # a fresh mail starts over
    _stored, new_magic, new_code = auth.request_link("jonas@example.org")
    assert auth.consume_code("jonas@example.org", new_code) is not None


def test_session_expiry_and_logout(monkeypatch):
    _account, session = login_user()
    assert auth.session_user(None) is None
    assert auth.session_user("not-a-token") is None

    auth.logout(session)
    assert auth.session_user(session) is None

    monkeypatch.setattr(auth, "SESSION_DAYS", -1)
    _name, magic, _code = auth.request_link("jonas@example.org")
    _account, session = auth.consume(magic)
    assert auth.session_user(session) is None
    # deleted on sight: still gone after the expiry window is restored
    monkeypatch.setattr(auth, "SESSION_DAYS", 180)
    assert auth.session_user(session) is None


def test_stale_unverified_accounts_are_purged(monkeypatch):
    login_user("Kept", "kept@example.org")  # consumed = verified
    _kind, _name, _magic, _code = register("Stale", "stale@example.org")

    # the next registration runs the purge with a zero-day grace period
    monkeypatch.setattr(auth, "UNVERIFIED_DAYS", -1)
    register("Later", "later@example.org")

    assert auth.request_link("stale@example.org") is None
    assert register("Stale", "stale2@example.org") is not None  # name is free
    assert auth.request_link("kept@example.org") is not None


def test_delete_account_anonymizes_posts():
    account, session = login_user()
    story = stories.create_story("Berlin Hbf", "", "", "", [], "", "Stranded", "x" * 20, "Jonas")
    stories.add_comment(story["id"], None, "Jonas", "same person")
    stories.set_vote(story["id"], account["id"], True)

    assert auth.delete_account("Nobody") is False
    assert auth.delete_account("Jonas") is True
    assert auth.session_user(session) is None
    listed = stories.list_stories("new", 10, 0)[0]
    assert listed["author"] == ""
    assert listed["score"] == 0  # the vote went with the account
    assert stories.list_comments(story["id"])[0]["author"] == ""


def test_suggested_names_fit_the_registration_rules():
    """The wordlists are the only thing keeping a suggestion inside the 2-25
    ASCII characters RegisterIn accepts, so pin both the shape and the bound."""
    words = [w for lists in (auth._LAZY, auth._CREATURES)
             for words in lists.values() for w in words]
    assert all(len(w) <= auth._MAX_WORD for w in words)
    assert all(w.isascii() and w.isalpha() for w in words)

    for lang in ("de", "en"):
        for digits in auth._SUGGEST_DIGITS:
            for _ in range(50):
                assert re.fullmatch(
                    r"[A-Za-z]+_[A-Za-z]+[0-9]{%d}" % digits,
                    auth._candidate(lang, digits),
                )
    # the worst case the wordlists allow is exactly the longest legal name
    assert auth._MAX_WORD * 2 + len("_") + max(auth._SUGGEST_DIGITS) == 25


def test_suggest_name_skips_taken_names(monkeypatch):
    register("Faul_Dachs42", "dachs@example.org")
    monkeypatch.setattr(auth, "_SUGGEST_DIGITS", (2,))

    # every candidate is the taken one until the pool is exhausted, so the
    # round has to come back empty rather than offering it again
    monkeypatch.setattr(auth, "_candidate", lambda lang, digits: "faul_dachs42")
    assert auth.suggest_name("de") is None

    names = iter(["Faul_Dachs42", "Traege_Yeti77"])
    monkeypatch.setattr(auth, "_candidate", lambda lang, digits: next(names, "x"))
    assert auth.suggest_name("de") == "Traege_Yeti77"


def test_suggest_name_follows_the_language(monkeypatch):
    monkeypatch.setattr(auth, "_SUGGEST_DIGITS", (2,))
    for lang, words in (("de", auth._LAZY["de"]), ("en", auth._LAZY["en"])):
        assert auth.suggest_name(lang).split("_")[0] in words
    # an unknown language falls back rather than raising a KeyError
    assert auth.suggest_name("fr").split("_")[0] in auth._LAZY["de"]
