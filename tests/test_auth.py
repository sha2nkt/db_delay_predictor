"""Accounts live in Firebase; what this module owns is reading one off a
verified token, handing out a unique username exactly once per account, and
the erasure that spans Firebase and the local tables. Firebase itself is
stood in by the `firebase` fixture (conftest.py)."""

import re
import sys

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

def test_delete_account_spans_firebase_the_registry_and_the_local_tables(fb):
    admin = fb.admin
    admin._record("u1", "jonas@example.org", True)
    auth.claim_handle("u1", "Jonas")
    story = stories.create_story("Berlin Hbf", "", "", "", [], "", "Title", "x" * 20, "Jonas")
    stories.set_vote(story["id"], "u1", 1)
    stories.set_vote(story["id"], "u2", 1)
    stories.add_comment(story["id"], None, "Jonas", "mine")
    stories.set_report("u1", "delay", True, from_station="Berlin Hbf")

    assert auth.delete_account("u1") is True
    assert "u1" not in admin.users
    assert fb.registry.taken(["jonas"]) == set()          # the name is free again
    left = stories.get_story(story["id"])
    assert (left["author"], left["score"]) == ("", 1)      # anonymized, the other vote stays
    assert stories.list_comments(story["id"])[0]["author"] == ""
    assert stories.my_reports("u1") == []


def test_delete_account_of_an_unknown_uid_is_false(fb):
    assert auth.delete_account("nobody") is False


# --- the emailed six-digit code ----------------------------------------------

def test_a_code_round_trips_into_a_custom_token(fb):
    code, kind = auth.issue_email_code("jonas@example.org")
    assert len(code) == auth.CODE_DIGITS and code.isdigit()
    assert kind == "welcome"                      # the address is new here
    token = auth.verify_email_code("jonas@example.org", code)
    assert token == "custom-token-for-uid-new-1"
    # redeeming created the account, and the code proved the mailbox
    rec = fb.admin.get_user_by_email("jonas@example.org")
    assert rec.email_verified is True
    # single use: the same code cannot be spent twice
    assert auth.verify_email_code("jonas@example.org", code) is None


def test_a_second_code_for_a_known_address_reads_as_a_login(fb):
    fb.admin._record("u1", "jonas@example.org", True)
    _code, kind = auth.issue_email_code("jonas@example.org")
    assert kind == "login"


def test_the_code_logs_into_the_existing_account_not_a_second_one(fb):
    fb.admin._record("u1", "jonas@example.org", True)
    code, _kind = auth.issue_email_code("jonas@example.org")
    assert auth.verify_email_code("jonas@example.org", code) == "custom-token-for-u1"
    assert len(fb.admin.users) == 1


def test_redeeming_confirms_an_address_that_never_was(fb):
    """A password sign-up that never clicked its mail: the code proves the
    mailbox just as the link would have."""
    fb.admin._record("u1", "jonas@example.org", False)
    code, _kind = auth.issue_email_code("jonas@example.org")
    auth.verify_email_code("jonas@example.org", code)
    assert fb.admin.users["u1"].email_verified is True


def test_addresses_are_normalized(fb):
    code, _kind = auth.issue_email_code("  JONAS@Example.ORG ")
    assert auth.verify_email_code("jonas@example.org", code) is not None


def test_a_wrong_code_is_refused_and_the_budget_voids_the_login(fb):
    code, _kind = auth.issue_email_code("jonas@example.org")
    wrong = f"{(int(code) + 1) % 10 ** auth.CODE_DIGITS:06d}"
    for _ in range(auth.MAX_TRIES):
        assert auth.verify_email_code("jonas@example.org", wrong) is None
    # the budget is spent: even the right code is dead now
    assert auth.verify_email_code("jonas@example.org", code) is None
    assert fb.admin.users == {}          # nothing was ever created


def test_an_expired_code_is_refused(fb):
    code, _kind = auth.issue_email_code("jonas@example.org")
    fb.advance(minutes=auth.CODE_TTL_MINUTES + 1)
    assert auth.verify_email_code("jonas@example.org", code) is None


def test_an_empty_or_unknown_code_is_refused(fb):
    assert auth.verify_email_code("jonas@example.org", "") is None
    assert auth.verify_email_code("nobody@example.org", "123456") is None


def test_the_resend_cooldown_suppresses_a_second_code(fb):
    first, _kind = auth.issue_email_code("jonas@example.org")
    assert auth.issue_email_code("jonas@example.org") is None
    # crucially the first code still works - throttling must not lock anyone out
    fb.advance(seconds=auth.RESEND_COOLDOWN_SECONDS + 1)
    assert auth.verify_email_code("jonas@example.org", first) is not None


def test_a_new_code_replaces_the_previous_one(fb):
    first, _kind = auth.issue_email_code("jonas@example.org")
    fb.advance(seconds=auth.RESEND_COOLDOWN_SECONDS + 1)
    second, _kind = auth.issue_email_code("jonas@example.org")
    assert auth.verify_email_code("jonas@example.org", first) is None
    assert auth.verify_email_code("jonas@example.org", second) is not None


def test_the_daily_cap_stops_further_mail(fb):
    for _ in range(auth.MAX_CODES_PER_DAY):
        assert auth.issue_email_code("jonas@example.org") is not None
        fb.advance(seconds=auth.RESEND_COOLDOWN_SECONDS + 1)
    assert auth.issue_email_code("jonas@example.org") is None
    # the allowance is per address, not global
    assert auth.issue_email_code("meike@example.org") is not None


def test_the_daily_cap_resets_the_next_day(fb):
    for _ in range(auth.MAX_CODES_PER_DAY):
        auth.issue_email_code("jonas@example.org")
        fb.advance(seconds=auth.RESEND_COOLDOWN_SECONDS + 1)
    assert auth.issue_email_code("jonas@example.org") is None
    fb.advance(days=1)
    assert auth.issue_email_code("jonas@example.org") is not None


def test_an_abandoned_code_is_swept_when_the_next_one_is_issued(fb):
    """Nobody came back for this one. Redeeming deletes the document and so
    does a later attempt, but an abandoned code has neither - so issuing
    clears them, and the privacy notice's "the entry is deleted" holds."""
    auth.issue_email_code("gone@example.org")
    assert len(fb.registry.codes) == 1
    fb.advance(minutes=auth.CODE_TTL_MINUTES + 1)
    auth.issue_email_code("someone-else@example.org")
    assert fb.registry.swept == 1
    assert len(fb.registry.codes) == 1                    # only the live one
    assert auth._email_key("gone@example.org") not in fb.registry.codes


def test_the_sweep_spares_pending_logins_that_are_still_good(fb):
    auth.issue_email_code("waiting@example.org")
    code, _kind = auth.issue_email_code("other@example.org")
    assert fb.registry.swept == 0
    # the one still inside its window is untouched and still redeemable
    assert auth.verify_email_code("other@example.org", code) is not None


def test_the_sweep_is_bounded_per_issue(fb):
    for i in range(auth.SWEEP_LIMIT + 5):
        auth.issue_email_code(f"u{i}@example.org")
    fb.advance(minutes=auth.CODE_TTL_MINUTES + 1)
    auth.issue_email_code("fresh@example.org")
    # one pass clears at most SWEEP_LIMIT, so the rest wait for the next
    assert fb.registry.swept == auth.SWEEP_LIMIT


def test_a_failing_sweep_never_costs_anyone_their_login(fb, monkeypatch):
    """A Firestore hiccup while tidying must not turn into "no code for you"."""
    def boom(_now):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(fb.registry, "_sweep_expired", boom)
    with pytest.raises(RuntimeError):
        fb.registry._sweep_expired(None)          # the fake really does raise
    # the real registry swallows it; prove the contract on the real method
    real = auth._Registry.__new__(auth._Registry)
    real._db = None                                # any use of it will raise
    real._sweep_expired(fb.now.value)              # must return quietly


def test_a_refund_hands_back_the_cooldown_and_the_slot(fb):
    """The mail failed to send, so the retry we just told the user to make
    must actually mint a new code rather than run into the cooldown."""
    auth.issue_email_code("jonas@example.org")
    auth.refund_code("jonas@example.org")
    assert auth.issue_email_code("jonas@example.org") is not None


# --- configuration -----------------------------------------------------------

def test_unconfigured_firebase_is_reported_not_faked(fb, monkeypatch):
    monkeypatch.setattr(auth, "_firebase", fb.real_firebase)
    monkeypatch.delenv("FIREBASE_SA_FILE", raising=False)
    monkeypatch.setattr(auth, "_app", None)
    assert auth.configured() is False
    assert auth.status() == {"configured": False}
    with pytest.raises(auth.AuthUnavailable):
        auth._firebase()


def test_a_missing_service_account_file_is_unavailable(fb, monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_firebase", fb.real_firebase)
    monkeypatch.setenv("FIREBASE_SA_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setattr(auth, "_app", None)
    assert auth.configured() is True   # set, so /health says so ...
    with pytest.raises(auth.AuthUnavailable):
        auth._firebase()               # ... but nothing can be done with it


def test_an_uninstalled_sdk_is_unavailable_not_a_traceback(fb, monkeypatch, tmp_path):
    """A deploy that pulled the new code without `uv sync`. The endpoints
    answer 503 off AuthUnavailable, so an ImportError escaping here would be
    a 500 with a traceback instead."""
    monkeypatch.setattr(auth, "_firebase", fb.real_firebase)
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    monkeypatch.setenv("FIREBASE_SA_FILE", str(sa))
    monkeypatch.setattr(auth, "_app", None)
    monkeypatch.setitem(sys.modules, "firebase_admin", None)  # makes the import raise
    with pytest.raises(auth.AuthUnavailable, match="not installed"):
        auth._firebase()
