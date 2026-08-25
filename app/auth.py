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

import logging
import os
import random
import threading
from pathlib import Path

from app import stories
from app.ratelimit import SlidingWindowLimiter

log = logging.getLogger(__name__)

# a token minted a moment ago must not be "used too early" because this box's
# clock trails Google's by a second; 10 s is well inside the SDK's 60 s cap
CLOCK_SKEW_SECONDS = 10

# Firestore collections. Both are keyed for the one lookup each answers:
# "is this name free" and "does this account already have one".
_NAMES = "usernames"
_USERS = "users"

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


def _registry() -> _Registry:
    _, db = _firebase()
    return _Registry(db)


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
    goes. False when Firebase has no such account."""
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
