"""Accounts live in Firebase; what this module owns is reading one off a
verified token, handing out a unique username exactly once per account, and
the erasure that spans Firebase and the local tables. Firebase itself is
stood in by the `firebase` fixture (conftest.py)."""

import re
from types import SimpleNamespace

import pytest

from app import auth, stories


@pytest.fixture(autouse=True)
def fb(firebase):
    return firebase


# --- the account behind a token ---------------------------------------------

def test_no_token_is_nobody(fb):
    assert auth.account(None) is None
    assert auth.account("") is None
    assert auth.account("never-issued") is None


def test_a_token_names_its_account(fb):
    tok = fb.token("t1", uid="u1", handle="Jonas")
    assert auth.account(tok) == {
        "uid": "u1", "name": "Jonas", "verified": True, "provider": "google.com",
    }


def test_a_token_without_a_handle_has_no_name(fb):
    assert auth.account(fb.token("t1", uid="u1"))["name"] is None


def test_password_accounts_count_as_verified_only_after_the_mail(fb):
    fresh = fb.token("t1", uid="u1", provider="password", verified=False)
    assert auth.account(fresh)["verified"] is False
    clicked = fb.token("t2", uid="u1", provider="password", verified=True)
    assert auth.account(clicked)["verified"] is True


def test_a_phone_sign_in_is_verified_by_the_sms(fb):
    assert auth.account(fb.token("t1", uid="u1", provider="phone"))["verified"] is True


def test_a_token_without_a_subject_is_rejected(fb):
    fb.tokens["odd"] = {"firebase": {"sign_in_provider": "google.com"}}
    assert auth.account("odd") is None


# --- claiming a username -----------------------------------------------------

def test_claiming_a_name_stamps_it_on_the_account(fb):
    assert auth.claim_handle("u1", "Jonas") == "ok"
    assert fb.stamped == {"u1": "Jonas"}
    assert fb.registry.taken(["jonas"]) == {"jonas"}


def test_names_are_unique_case_insensitively(fb):
    auth.claim_handle("u1", "Jonas")
    assert auth.claim_handle("u2", "jonas") == "taken"
    assert auth.claim_handle("u2", "JONAS") == "taken"
    assert "u2" not in fb.stamped  # nothing was written to a refused account


def test_an_account_gets_exactly_one_name(fb):
    auth.claim_handle("u1", "Jonas")
    assert auth.claim_handle("u1", "Other") == "named"
    assert fb.stamped == {"u1": "Jonas"}
    assert fb.registry.taken(["other"]) == set()


# --- suggested handles -------------------------------------------------------

HOUSE_FORMAT = re.compile(r"^[A-Za-z]{1,10}_[A-Za-z]{1,10}\d{2,4}$")


def test_suggested_names_fit_the_house_format_and_the_length_cap(fb):
    for _ in range(50):
        name = auth.suggest_name("de")
        assert HOUSE_FORMAT.match(name), name
        assert len(name) <= 25
    assert HOUSE_FORMAT.match(auth.suggest_name("en"))


def test_every_word_fits_the_cap_in_ascii():
    for table in (auth._LAZY, auth._CREATURES):
        for words in table.values():
            for word in words:
                assert len(word) <= auth._MAX_WORD and word.isascii(), word


def test_a_suggestion_is_free_and_claims(fb):
    name = auth.suggest_name("en")
    assert auth.claim_handle("u1", name) == "ok"


def test_a_taken_candidate_is_skipped(fb, monkeypatch):
    calls = iter(["Lazy_Sloth11"] * 8 + ["Lazy_Sloth22"] * 100)
    monkeypatch.setattr(auth, "_candidate", lambda lang, digits: next(calls))
    auth.claim_handle("u1", "lazy_sloth11")
    assert auth.suggest_name("en") == "Lazy_Sloth22"


def test_an_exhausted_wordlist_yields_nothing(fb, monkeypatch):
    monkeypatch.setattr(auth, "_candidate", lambda lang, digits: "Lazy_Sloth11")
    auth.claim_handle("u1", "Lazy_Sloth11")
    assert auth.suggest_name("en") is None


def test_an_unknown_language_falls_back_to_german(fb):
    name = auth.suggest_name("fr")
    adjective, _rest = name.split("_", 1)
    assert adjective in auth._LAZY["de"]


# --- erasure -----------------------------------------------------------------

class FakeAdmin:
    class UserNotFoundError(Exception):
        pass

    def __init__(self, known):
        self.known = set(known)
        self.deleted = []

    def delete_user(self, uid, app=None):
        if uid not in self.known:
            raise self.UserNotFoundError(uid)
        self.known.discard(uid)
        self.deleted.append(uid)


def test_delete_account_spans_firebase_the_registry_and_the_local_tables(fb, monkeypatch):
    admin = FakeAdmin(["u1"])
    monkeypatch.setattr(auth, "_firebase", lambda: (admin, None))
    auth.claim_handle("u1", "Jonas")
    story = stories.create_story("Berlin Hbf", "", "", "", [], "", "Title", "x" * 20, "Jonas")
    stories.set_vote(story["id"], "u1", 1)
    stories.set_vote(story["id"], "u2", 1)
    stories.add_comment(story["id"], None, "Jonas", "mine")
    stories.set_report("u1", "delay", True, from_station="Berlin Hbf")

    assert auth.delete_account("u1") is True
    assert admin.deleted == ["u1"]
    assert fb.registry.taken(["jonas"]) == set()          # the name is free again
    left = stories.get_story(story["id"])
    assert (left["author"], left["score"]) == ("", 1)      # anonymized, the other vote stays
    assert stories.list_comments(story["id"])[0]["author"] == ""
    assert stories.my_reports("u1") == []


def test_delete_account_of_an_unknown_uid_is_false(fb, monkeypatch):
    monkeypatch.setattr(auth, "_firebase", lambda: (FakeAdmin([]), None))
    assert auth.delete_account("nobody") is False


# --- configuration -----------------------------------------------------------

def test_unconfigured_firebase_is_reported_not_faked(monkeypatch):
    monkeypatch.delenv("FIREBASE_SA_FILE", raising=False)
    monkeypatch.setattr(auth, "_app", None)
    assert auth.configured() is False
    assert auth.status() == {"configured": False}
    with pytest.raises(auth.AuthUnavailable):
        auth._firebase()


def test_a_missing_service_account_file_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("FIREBASE_SA_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setattr(auth, "_app", None)
    assert auth.configured() is True   # set, so /health says so ...
    with pytest.raises(auth.AuthUnavailable):
        auth._firebase()               # ... but nothing can be done with it
