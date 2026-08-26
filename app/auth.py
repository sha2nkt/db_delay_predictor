"""Accounts for the stories board live in Firebase Authentication: nothing
about who somebody is touches this server's disk. The browser signs in
through the Firebase JS SDK (Google, Apple, phone, or email + password) and
sends the resulting ID token as a bearer on every request that needs an
account; this module verifies the token's signature against Google's public
keys - fetched once and cached, no network per request - and reads the
account straight from the signed claims.

The one thing Firebase cannot enforce is a unique public username, so the
claim of a name is a transaction on a tiny Firestore registry
(usernames/{lowercased} -> uid, users/{uid} -> name), and the claimed name is
then stamped on the account as the custom claim `handle`, so every later
token carries it and no lookup is needed to attribute a post. Names never
change once claimed, so a token can never carry a stale one.

Configuration is a single service-account file (FIREBASE_SA_FILE); without it
every account operation reports itself unavailable (503) and the rest of the
site is unaffected. Blocking throughout - run it off the event loop.
"""

import hashlib
import logging
import os
import random
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import stories
from app.ratelimit import SlidingWindowLimiter

log = logging.getLogger(__name__)

# a token minted a moment ago must not be "used too early" because this box's
# clock trails Google's by a second; 10 s is well inside the SDK's 60 s cap
CLOCK_SKEW_SECONDS = 10

# Firestore collections. The first two are keyed for the one lookup each
# answers: "is this name free" and "does this account already have one".
_NAMES = "usernames"
_USERS = "users"
# Pending email logins. Firebase has no email one-time code of its own - its
# codes are SMS only, its email flows are all clickable links - so the code is
# minted here and kept here. It lives in Firestore rather than the app's
# SQLite deliberately: no part of a login may touch this server's disk.
_CODES = "email_codes"

CODE_DIGITS = 6
CODE_TTL_MINUTES = 15

# A 6-digit code is only a million possibilities, so the attempt budget - not
# the hash - is what protects it: MAX_TRIES wrong guesses kill the pending
# login outright and a new mail has to be requested. (The stored SHA-256 is
# therefore brute-forceable offline by anyone who can read Firestore, but only
# against logins still pending inside the short window, and the rules there
# deny every client.)
MAX_TRIES = 5

# Per-ADDRESS budgets, independent of the per-IP limiters below. The mail is
# the login here, so a spray of requests for someone else's address is both a
# way to bombard them and a way to burn the provider's daily quota - and once
# that quota is gone, nobody can log in at all. A throttled request issues
# nothing and sends nothing, but still reports success: "too many requests"
# would say when this address last asked for a code, and the code already in
# the mailbox still works, so a legitimate user is never locked out by this.
RESEND_COOLDOWN_SECONDS = 60
MAX_CODES_PER_DAY = 10

# Claiming a name is the one write a fresh account makes; nobody legitimately
# needs many a day, but a shared NAT (campus, office) must still let a
# handful of people sign up.
register_limiter = SlidingWindowLimiter(
    burst_limit=3, burst_window=300, sustained_limit=10, sustained_window=86400
)
# Name suggestions cost one batched Firestore read and give nothing away, so
# this budget only exists to keep a bored visitor from rerolling in a loop.
suggest_limiter = SlidingWindowLimiter(
    burst_limit=20, burst_window=60, sustained_limit=120, sustained_window=3600
)
# Asking for a code sends mail, so this is the tightest per-IP budget of the
# lot; the per-address budget above is what protects one mailbox, this is what
# stops one machine spraying many.
email_limiter = SlidingWindowLimiter(
    burst_limit=5, burst_window=60, sustained_limit=30, sustained_window=3600
)
# Complements the per-address MAX_TRIES budget: that one caps guessing against
# a single address, this one caps spraying one guess across many.
code_limiter = SlidingWindowLimiter(
    burst_limit=10, burst_window=60, sustained_limit=50, sustained_window=3600
)


class AuthUnavailable(Exception):
    """Firebase is not configured or not reachable: the caller answers 503,
    never "invalid login" - that would tell a user to fix something on their
    side that is broken on ours."""


def configured() -> bool:
    return bool(os.environ.get("FIREBASE_SA_FILE"))


def status() -> dict:
    """In-memory only - /health calls this."""
    return {"configured": configured()}


# --- Firebase wiring ----------------------------------------------------------
# Imported lazily and initialised once: firebase_admin pulls in the google-cloud
# stack, which has no business loading for a request that never needs it.

_lock = threading.Lock()
_app = None
_db = None


def _firebase():
    """(firebase_admin.auth, firestore client), initialised on first use."""
    global _app, _db
    with _lock:
        if _app is None:
            sa_file = os.environ.get("FIREBASE_SA_FILE")
            if not sa_file or not Path(sa_file).is_file():
                raise AuthUnavailable("FIREBASE_SA_FILE is unset or missing")
            try:
                import firebase_admin
                from firebase_admin import credentials, firestore
            except ImportError as exc:
                # a deploy that pulled the new code without `uv sync`: the site
                # is fine, accounts are simply unavailable, and that has to
                # read as 503 rather than a traceback
                raise AuthUnavailable(f"firebase-admin is not installed: {exc}") from exc
            _app = firebase_admin.initialize_app(credentials.Certificate(sa_file))
            _db = firestore.client(_app)
    from firebase_admin import auth as fb_auth

    return fb_auth, _db


def _verify(token: str) -> dict | None:
    """The token's claims, or None when it is not a valid, current Firebase
    ID token for this project. Replaced wholesale by the tests."""
    fb_auth, _ = _firebase()
    from firebase_admin import exceptions

    try:
        return fb_auth.verify_id_token(
            token, app=_app, clock_skew_seconds=CLOCK_SKEW_SECONDS
        )
    except (fb_auth.InvalidIdTokenError, ValueError):
        # expired and revoked are subclasses; a garbage string is the ValueError
        return None
    except exceptions.FirebaseError as exc:
        # the public keys could not be fetched: nothing to verify against
        raise AuthUnavailable(str(exc)) from exc


def _stamp(uid: str, name: str) -> None:
    """Write the claimed name onto the Firebase account: the display name
    for the SDK's own use, and the custom claim the server trusts."""
    fb_auth, _ = _firebase()
    fb_auth.update_user(uid, display_name=name, app=_app)
    fb_auth.set_custom_user_claims(uid, {"handle": name}, app=_app)


class _Registry:
    """The Firestore side of usernames, isolated so tests can swap in a
    dict. Names are compared lowercased: "Jonas" and "jonas" would be
    indistinguishable in a comment thread."""

    def __init__(self, db):
        self._db = db

    def taken(self, names: list[str]) -> set[str]:
        """Which of these names (lowercased) already belong to someone - one
        batched read for the whole list."""
        refs = [self._db.collection(_NAMES).document(n.lower()) for n in names]
        return {snap.id for snap in self._db.get_all(refs) if snap.exists}

    def claim(self, uid: str, name: str) -> str:
        """"ok", "taken", or "named" (this account already has a name).
        One transaction, so two claims of the same name - or two names by
        one account - cannot both go through."""
        from google.cloud import firestore

        name_ref = self._db.collection(_NAMES).document(name.lower())
        user_ref = self._db.collection(_USERS).document(uid)

        @firestore.transactional
        def run(txn):
            # reads first, as Firestore transactions require
            user_snap = user_ref.get(transaction=txn)
            name_snap = name_ref.get(transaction=txn)
            if user_snap.exists:
                return "named"
            if name_snap.exists:
                return "taken"
            stamp = {"ts": firestore.SERVER_TIMESTAMP}
            txn.set(name_ref, {"uid": uid, "name": name, **stamp})
            txn.set(user_ref, {"name": name, **stamp})
            return "ok"

        return run(self._db.transaction())

    def release(self, uid: str) -> str | None:
        """Drop the account's registry entries; the name it held, if any."""
        user_ref = self._db.collection(_USERS).document(uid)
        snap = user_ref.get()
        if not snap.exists:
            return None
        name = snap.get("name")
        self._db.collection(_NAMES).document(name.lower()).delete()
        user_ref.delete()
        return name

    # --- pending email logins -------------------------------------------------
    # Keyed by a hash of the address rather than the address itself: Firestore
    # document ids are listable, and a collection of plaintext addresses of
    # people mid-login is not something to keep even behind deny-all rules.

    def issue_code(self, email: str, code_hash: str) -> bool:
        """Record a freshly minted code against this address, spending one of
        its daily allowance. False when the cooldown or the allowance says no
        mail should go out - the caller then sends nothing and still reports
        success. One transaction, so two simultaneous requests cannot both
        pass the budget check."""
        from google.cloud import firestore

        ref = self._db.collection(_CODES).document(_email_key(email))
        now = _now()
        today = now.date().isoformat()

        @firestore.transactional
        def run(txn):
            snap = ref.get(transaction=txn)
            sent, last = 0, None
            if snap.exists:
                data = snap.to_dict()
                # the daily counter resets on the UTC date turning over
                sent = data.get("sent_today", 0) if data.get("day") == today else 0
                last = data.get("sent_at")
            if sent >= MAX_CODES_PER_DAY:
                return False
            if last is not None and (now - _parse(last)).total_seconds() < RESEND_COOLDOWN_SECONDS:
                return False
            expires = now + timedelta(minutes=CODE_TTL_MINUTES)
            txn.set(ref, {
                "code_hash": code_hash,
                "expires": _iso(expires),
                # the same instant as a real Timestamp, for the Firestore TTL
                # policy on this collection: redeeming deletes the document
                # and so does a later attempt, but a code that is simply
                # abandoned would otherwise sit here forever, and "the entry
                # is deleted" is what the privacy notice promises
                "expire_at": expires,
                "tries": 0,
                "sent_at": _iso(now),
                "day": today,
                "sent_today": sent + 1,
            })
            return True

        return run(self._db.transaction())

    def redeem_code(self, email: str, code_hash: str) -> bool:
        """Spend the pending code for this address. True only for the right
        code, unexpired, with guesses left - and then the pending login is
        gone, so it cannot be spent twice. A wrong guess costs one try, and
        the last one voids the login outright so brute force has to start
        over from a new mail. All inside one transaction, so two racing
        attempts cannot both win."""
        from google.cloud import firestore

        ref = self._db.collection(_CODES).document(_email_key(email))
        now = _now()

        @firestore.transactional
        def run(txn):
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return False
            data = snap.to_dict()
            if _parse(data["expires"]) < now or data.get("tries", 0) >= MAX_TRIES:
                txn.delete(ref)
                return False
            # compare_digest over two hex digests: not reachable through HTTP
            # jitter, but free to do right
            if not secrets.compare_digest(data.get("code_hash", ""), code_hash):
                if data.get("tries", 0) + 1 >= MAX_TRIES:
                    txn.delete(ref)
                else:
                    txn.update(ref, {"tries": data.get("tries", 0) + 1})
                return False
            txn.delete(ref)
            return True

        return run(self._db.transaction())

    def refund_code(self, email: str) -> None:
        """Undo issue_code for a code that never left the server, handing back
        the cooldown and the daily slot it spent - otherwise the retry the
        caller just asked for would report success and send nothing."""
        self._db.collection(_CODES).document(_email_key(email)).delete()


def _registry() -> _Registry:
    _, db = _firebase()
    return _Registry(db)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def normalize_email(email: str) -> str:
    # emails are compared lowercased: "Max@Web.de" and "max@web.de" must be
    # one account, not an enumeration side channel
    return email.strip().lower()


def _email_key(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# --- the email code -----------------------------------------------------------

def issue_email_code(email: str) -> tuple[str, str] | None:
    """(code, kind) for one pending login, replacing any outstanding one, or
    None when this address's cooldown or daily allowance says no mail should
    go out. kind is "welcome" the first time an address is seen and "login"
    afterwards, so a first mail does not read like a login it never asked
    for. Raises AuthUnavailable."""
    email = normalize_email(email)
    code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
    if not _registry().issue_code(email, _code_hash(code)):
        return None
    return code, "login" if _existing_user(email) else "welcome"


def _existing_user(email: str) -> str | None:
    """The uid already registered to this address, or None."""
    fb_auth, _ = _firebase()
    try:
        return fb_auth.get_user_by_email(email, app=_app).uid
    except fb_auth.UserNotFoundError:
        return None


def verify_email_code(email: str, code: str) -> str | None:
    """Redeem the code and hand back a Firebase custom token the browser
    signs in with; None on any failure (unknown address, no pending login,
    expired, wrong code, or budget exhausted) - the caller must not spell out
    which. Redeeming proves control of the mailbox, so the account is created
    if new and marked verified either way; that is exactly the guarantee the
    old emailed link gave. An address that already signed in through Google
    lands in the same account, which is the point: the address is the
    identity, and this is no weaker than the password reset Google itself
    offers. Raises AuthUnavailable."""
    email = normalize_email(email)
    if not code or not _registry().redeem_code(email, _code_hash(code)):
        return None
    fb_auth, _ = _firebase()
    uid = _existing_user(email)
    if uid is None:
        uid = fb_auth.create_user(email=email, email_verified=True, app=_app).uid
    else:
        # the code proved the mailbox; an account that had never confirmed it
        # (signed up with a password and never clicked) is confirmed now
        fb_auth.update_user(uid, email_verified=True, app=_app)
    return fb_auth.create_custom_token(uid, app=_app).decode()


def refund_code(email: str) -> None:
    """Undo issue_email_code when the mail could not be sent. Raises
    AuthUnavailable."""
    _registry().refund_code(normalize_email(email))


# --- the account behind a request ---------------------------------------------

def account(token: str | None) -> dict | None:
    """The account a bearer token proves, or None when the token is missing,
    malformed, expired, or not ours. `name` is None until the account has
    claimed one; `verified` says whether the sign-in method proved contact
    with the person - Google and Apple vouch for the address, a phone number
    was just confirmed by SMS, and email + password only counts once the
    verification mail was clicked. Raises AuthUnavailable."""
    if not token:
        return None
    claims = _verify(token)
    if not claims or not claims.get("sub"):
        return None
    provider = (claims.get("firebase") or {}).get("sign_in_provider", "")
    verified = bool(claims.get("email_verified")) or provider == "phone"
    return {
        "uid": claims["sub"],
        "name": claims.get("handle") or None,
        "verified": verified,
        "provider": provider,
    }


def claim_handle(uid: str, name: str) -> str:
    """Give this account its public name: "ok", "taken", or "named" when it
    already has one. The name is only stamped onto the token once the
    registry accepted it, so a race on one name ends with exactly one
    winner carrying it. Raises AuthUnavailable."""
    result = _registry().claim(uid, name)
    if result == "ok":
        _stamp(uid, name)
    return result


def delete_account(uid: str) -> bool:
    """GDPR erasure helper (manual, on request via kontakt@): removes the
    Firebase account and its registry entries, drops its votes and taps,
    and anonymizes authored posts in place - the stories stay, the name
    goes. False when Firebase has no such account.

    One caveat, verified end-to-end rather than assumed: an ID token issued
    before the deletion keeps verifying until it expires (Firebase tokens
    last an hour, and account() checks the signature, not the account's
    continued existence). So for up to an hour the erased account can still
    write, and the username it released can be claimed by somebody else
    while the old token still carries it. Refreshing is already impossible -
    the account is gone - so the window cannot be extended. Closing it
    outright means verify_id_token(check_revoked=True), which spends a
    Firebase round trip on EVERY authenticated request; that is a poor
    trade for a manual, rare operation, so the window is accepted. If
    deletion ever becomes a moderation tool (banning), revisit this and
    pass check_revoked on the write paths only."""
    fb_auth, _ = _firebase()
    try:
        fb_auth.delete_user(uid, app=_app)
    except fb_auth.UserNotFoundError:
        return False
    name = _registry().release(uid)
    stories.forget_account(uid, name)
    return True


# --- suggested handles --------------------------------------------------------
# <lazy adjective>_<creature><digits>, in the site's two languages. Every word
# is ASCII and at most _MAX_WORD long, so the longest possible name - 10 + "_"
# + 10 + four digits - is exactly the 25 characters HandleIn allows;
# test_auth.py pins that. Fictional creatures are folklore or public domain
# rather than trademarked characters: the site mints these names itself, and
# a name it minted is one it hands out under its own logo.
_MAX_WORD = 10

_LAZY = {
    "de": [
        "Verspaetet", "Traege", "Faul", "Langsam", "Gemuetlich", "Bummelig",
        "Schlaefrig", "Verzoegert", "Saeumig", "Muede", "Zoegerlich",
        "Wartend", "Trudelnd", "Doesig", "Lahm", "Behaebig", "Schlurfend",
        "Traeumend", "Schleppend",
    ],
    "en": [
        "Sluggish", "Tardy", "Delayed", "Lazy", "Idle", "Dawdling", "Belated",
        "Slothful", "Stalled", "Snoozy", "Lagging", "Creeping", "Drowsy",
        "Sleepy", "Unhurried", "Overdue", "Stranded", "Lingering",
        "Postponed", "Dozy", "Languid", "Trudging", "Shuffling",
        "Unpunctual", "Late", "Loitering", "Yawning",
    ],
}

_CREATURES = {
    "de": [
        "Faultier", "Wombat", "Kapybara", "Axolotl", "Seekuh", "Schnecke",
        "Koala", "Murmeltier", "Dachs", "Igel", "Maulwurf", "Waschbaer",
        "Nilpferd", "Tapir", "Panda", "Quokka", "Drache", "Greif", "Lindwurm",
        "Einhorn", "Wichtel", "Kobold", "Yeti", "Kraken", "Nessie", "Troll",
        "Gnom", "Zwerg", "Riese", "Basilisk", "Phoenix", "Golem", "Sphinx",
        "Nachtmahr", "Irrwisch",
    ],
    "en": [
        "Sloth", "Wombat", "Capybara", "Axolotl", "Manatee", "Tortoise",
        "Snail", "Koala", "Pangolin", "Narwhal", "Quokka", "Platypus",
        "Dodo", "Gryphon", "Wyvern", "Kraken", "Yeti", "Bigfoot", "Unicorn",
        "Phoenix", "Chimera", "Basilisk", "Jackalope", "Mothman", "Nessie",
        "Golem", "Goblin", "Gnome", "Sphinx", "Hydra", "Pegasus", "Centaur",
        "Minotaur", "Ogre", "Troll", "Griffin", "Lindworm", "Sandman",
        "Selkie", "Banshee",
    ],
}

# One batched read per round instead of one per candidate, and the rounds
# widen the number rather than repeating the same shape: two digits reads
# best, so it is what a first-time visitor is offered, and only a collision
# spends more.
_SUGGEST_CANDIDATES = 8
_SUGGEST_DIGITS = (2, 3, 4)


# A suggestion is a public handle, not a secret - the ordinary PRNG is the
# right tool. Nothing downstream trusts the name to be unguessable.
def _candidate(lang: str, digits: int) -> str:
    number = random.randrange(10 ** (digits - 1), 10 ** digits)
    return (f"{random.choice(_LAZY[lang])}_"
            f"{random.choice(_CREATURES[lang])}{number}")


def suggest_name(lang: str = "de") -> str | None:
    """A free username in the house format, or None if even four digits kept
    colliding (which needs the wordlist to be exhausted, not merely busy).
    Free at the moment of asking only: nothing is reserved, so two visitors
    can be offered the same name and the second one to claim it gets the 409
    that any hand-typed clash would get. Raises AuthUnavailable."""
    if lang not in _LAZY:
        lang = "de"
    registry = _registry()
    for digits in _SUGGEST_DIGITS:
        names = [_candidate(lang, digits) for _ in range(_SUGGEST_CANDIDATES)]
        taken = registry.taken(names)
        for name in names:
            if name.lower() not in taken:
                return name
    return None
