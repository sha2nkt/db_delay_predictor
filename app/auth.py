"""Passwordless accounts for the stories board: a username and an email
address, nothing else. Both registering and logging in send the same mail,
carrying two forms of one secret: a magic link and a 6-digit code. Either
proves mailbox control, which doubles as the double opt-in (verified_ts, set
on first use, is the proof) and starts the session. The code exists for the
cross-device case - request on the laptop, mail opens on the phone - where
clicking the link would sign in the wrong device. Only the self-chosen
username is ever shown publicly. Accounts that never confirm are purged after
UNVERIFIED_DAYS (data minimization, and it frees the squatted name).

The tables live in the stories SQLite file (stories.connect), because accounts
exist only to pin story authorship and votes to a stable name. Sessions, links
and codes are stored server-side only as SHA-256 hashes, so a leaked DB leaks
neither live sessions nor pending logins. Each account has at most one
outstanding login - issuing a new one invalidates the old, and using either
form clears both.
"""

import hashlib
import random
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.ratelimit import SlidingWindowLimiter
from app.stories import connect

SESSION_DAYS = 180
MAGIC_LINK_HOURS = 1
UNVERIFIED_DAYS = 7

# A 6-digit code is only a million possibilities, so the attempt budget - not
# the hash - is what protects it: MAX_TRIES wrong guesses kill the pending
# login outright and a new mail has to be requested. (The stored SHA-256 is
# therefore brute-forceable offline from a stolen DB, but only against logins
# still pending inside the one-hour window, and a reader of the DB file has
# far better options anyway. The link half is a full 256-bit token.)
CODE_DIGITS = 6
MAX_TRIES = 5

# Per-ACCOUNT budgets, independent of the per-IP limiters below. The mail is
# the login here, so a spray of requests for someone else's address is both a
# way to bombard them and a way to burn the provider's daily quota - and once
# that quota is gone, nobody can log in at all. A throttled request issues
# nothing and sends nothing, but still reports success: "too many requests"
# would say when this account last asked for a login, and the link already in
# the mailbox still works, so a legitimate user is never locked out by this.
# (Whether an address has an account at all is answered plainly - see
# request_link - but when it last logged in is not.)
RESEND_COOLDOWN_SECONDS = 60
MAX_LINKS_PER_DAY = 10

# Guesses are capped per login (MAX_TRIES) *and* cumulatively per day, because
# re-issuing a login resets the former - without this an attacker would simply
# request a fresh mail for five more attempts.
MAX_CODE_FAILS_PER_DAY = 20

# Link requests and account minting are per-IP budgets. Registration is the
# tighter one: nobody legitimately needs many accounts a day, but a shared
# NAT (campus, office) must still let a handful of people sign up.
login_limiter = SlidingWindowLimiter(
    burst_limit=5, burst_window=60, sustained_limit=30, sustained_window=3600
)
register_limiter = SlidingWindowLimiter(
    burst_limit=3, burst_window=300, sustained_limit=10, sustained_window=86400
)
# complements the per-account MAX_TRIES budget: that one caps guessing against
# a single account, this one caps spraying one guess across many
code_limiter = SlidingWindowLimiter(
    burst_limit=10, burst_window=60, sustained_limit=50, sustained_window=3600
)
# Name suggestions cost one indexed SELECT and give nothing away, so this
# budget only exists to keep a bored visitor from rerolling in a loop. It has
# to stay well clear of honest clicking: one on page load, then a handful.
suggest_limiter = SlidingWindowLimiter(
    burst_limit=20, burst_window=60, sustained_limit=120, sustained_window=3600
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, ts) VALUES (?, ?, ?)",
        (_token_hash(token), user_id, _now().isoformat(timespec="seconds")),
    )
    return token


def _roll_budget(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    """Zero the daily counters when the UTC date has turned over, and return
    the account's current budget row."""
    today = _now().date().isoformat()
    row = conn.execute(
        "SELECT budget_day, links_sent, code_fails, link_last_sent"
        " FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row["budget_day"] != today:
        conn.execute(
            "UPDATE users SET budget_day = ?, links_sent = 0, code_fails = 0"
            " WHERE id = ?",
            (today, user_id),
        )
        row = conn.execute(
            "SELECT budget_day, links_sent, code_fails, link_last_sent"
            " FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return row


def _issue_magic(conn: sqlite3.Connection, user_id: int) -> tuple[str, str] | None:
    """(link_token, code) for one pending login, replacing any outstanding
    one; None when this account's cooldown or daily allowance says no mail
    should go out. Resets the per-login guess budget but never the daily one."""
    now = _now()
    budget = _roll_budget(conn, user_id)
    if budget["links_sent"] >= MAX_LINKS_PER_DAY:
        return None
    last = budget["link_last_sent"]
    if last is not None:
        waited = (now - datetime.fromisoformat(last)).total_seconds()
        if waited < RESEND_COOLDOWN_SECONDS:
            return None

    token = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
    expires = (now + timedelta(hours=MAGIC_LINK_HOURS)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE users SET magic_hash = ?, magic_code = ?, magic_expires = ?,"
        " magic_tries = 0, links_sent = links_sent + 1, link_last_sent = ?"
        " WHERE id = ?",
        (_token_hash(token), _token_hash(code), expires,
         now.isoformat(timespec="seconds"), user_id),
    )
    return token, code


def _clear_magic(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET magic_hash = NULL, magic_code = NULL,"
        " magic_expires = NULL, magic_tries = 0 WHERE id = ?",
        (user_id,),
    )


def _purge_unverified(conn: sqlite3.Connection) -> None:
    cutoff = (_now() - timedelta(days=UNVERIFIED_DAYS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM users WHERE verified_ts IS NULL AND ts < ?", (cutoff,))


def normalize_email(email: str) -> str:
    # emails are compared lowercased: "Max@Web.de" and "max@web.de" must be
    # one account, not an enumeration side channel
    return email.strip().lower()


# Suggested handles: <lazy adjective>_<creature><digits>, in the site's two
# languages. Every word is ASCII and at most _MAX_WORD long, so the longest
# possible name - 10 + "_" + 10 + four digits - is exactly the 25 characters
# RegisterIn allows; test_auth.py pins that. Fictional creatures are folklore
# or public domain rather than trademarked characters: the site mints these
# names itself, and a name it minted is one it hands out under its own logo.
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

# One SELECT per round instead of one per candidate, and the rounds widen the
# number rather than repeating the same shape: two digits reads best, so it is
# what a first-time visitor is offered, and only a collision spends more.
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
    can be offered the same name and the second one to register gets the 409
    that any hand-typed clash would get. Blocking - run off the event loop."""
    if lang not in _LAZY:
        lang = "de"
    with closing(connect()) as conn:
        for digits in _SUGGEST_DIGITS:
            names = [_candidate(lang, digits) for _ in range(_SUGGEST_CANDIDATES)]
            marks = ",".join("?" * len(names))
            # name is COLLATE NOCASE, so this IN matches the same way the
            # UNIQUE index does - a candidate differing only in case is taken
            taken = {
                row["name"].lower() for row in conn.execute(
                    f"SELECT name FROM users WHERE name IN ({marks})", names
                )
            }
            for name in names:
                if name.lower() not in taken:
                    return name
    return None


def register(name: str, email: str) -> tuple[str, str, str | None, str | None] | None:
    """(kind, stored_name, link_token, code): kind "new" when the account was
    created, or "existing" when the email already has an account - then the
    login logs into THAT account, so a sign-up with an address already in use
    ends in a working login rather than a dead end. token and code are
    None when the account's own rate budget says no mail should go out; the
    caller still answers 202. None (the whole return) when the name is taken
    case-insensitively - "Jonas" and "jonas" would be indistinguishable in a
    comment thread. Blocking, like everything here - run off the event loop."""
    email = normalize_email(email)
    with closing(connect()) as conn, conn:
        _purge_unverified(conn)
        row = conn.execute(
            "SELECT id, name, verified_ts FROM users WHERE email = ?", (email,)
        ).fetchone()
        if row is not None:
            stored = row["name"]
            # A never-confirmed account has never been shown, has no posts and
            # no votes, so its name can still change - the only way someone who
            # typo'd their handle at signup can correct it. The new name is
            # only *recorded* here and applied when the emailed login is
            # redeemed, so knowing an address is not enough to rename someone
            # else's pending account: whoever reads the mailbox decides.
            if row["verified_ts"] is None and name.lower() != stored.lower():
                taken = conn.execute(
                    "SELECT 1 FROM users WHERE name = ? AND id <> ?",
                    (name, row["id"]),
                ).fetchone()
                if taken is not None:
                    return None
                conn.execute(
                    "UPDATE users SET pending_name = ? WHERE id = ?",
                    (name, row["id"]),
                )
                stored = name  # the mail greets them by the name they just typed
            issued = _issue_magic(conn, row["id"])
            token, code = issued if issued else (None, None)
            return "existing", stored, token, code
        try:
            cur = conn.execute(
                "INSERT INTO users (name, email, ts) VALUES (?, ?, ?)",
                (name, email, _now().isoformat(timespec="seconds")),
            )
        except sqlite3.IntegrityError:
            return None
        issued = _issue_magic(conn, cur.lastrowid)
        token, code = issued if issued else (None, None)
        return "new", name, token, code


def request_link(email: str) -> tuple[str, str | None, str | None] | None:
    """(stored_name, link_token, code) for the account behind this email -
    token and code None when its resend budget says no mail should go out,
    which the caller still reports as success. None (the whole return) only
    when the address has no account at all: the caller turns that into "no
    account yet, create one", so the two cases must not be conflated - a
    visitor on cooldown asking again is not a visitor without an account.
    Same shape as register()."""
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT id, name FROM users WHERE email = ?", (normalize_email(email),)
        ).fetchone()
        if row is None:
            return None
        issued = _issue_magic(conn, row["id"])
        token, code = issued if issued else (None, None)
        return row["name"], token, code


# Redemption is one conditional UPDATE, not a SELECT followed by an UPDATE:
# SQLite starts a deferred transaction on first write, so two concurrent
# redemptions of the same link would both pass a separate SELECT and both open
# a session. Matching on the secret inside the UPDATE means exactly one caller
# can win - the loser's WHERE no longer matches, because the winner nulled it.
# Timestamps compare as text: every one is written by isoformat() in UTC, so
# lexicographic and chronological order agree.
_CLAIM = (
    "UPDATE users SET magic_hash = NULL, magic_code = NULL, magic_expires = NULL,"
    " magic_tries = 0, verified_ts = COALESCE(verified_ts, ?)"
)


def _session_for(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict, str]:
    """Open the session, applying a rename that a re-registration recorded.
    Redeeming the login is what authorises it - the request that asked for the
    name only proposed it."""
    name = row["name"]
    if row["pending_name"]:
        try:
            conn.execute(
                "UPDATE users SET name = ? WHERE id = ?",
                (row["pending_name"], row["id"]),
            )
            name = row["pending_name"]
        except sqlite3.IntegrityError:
            pass  # claimed by somebody else meanwhile; keep the current name
        conn.execute(
            "UPDATE users SET pending_name = NULL WHERE id = ?", (row["id"],)
        )
    return {"id": row["id"], "name": name}, _new_session(conn, row["id"])


def consume(token: str) -> tuple[dict, str] | None:
    """Redeem a magic link: single use, expiring. None on unknown or expired
    token. Using it also kills the code half of the same login."""
    if not token:
        return None
    now = _now().isoformat(timespec="seconds")
    with closing(connect()) as conn, conn:
        row = conn.execute(
            _CLAIM + " WHERE magic_hash = ? AND magic_expires >= ?"
                     " RETURNING id, name, pending_name",
            (now, _token_hash(token), now),
        ).fetchone()
        return None if row is None else _session_for(conn, row)


def consume_code(email: str, code: str) -> tuple[dict, str] | None:
    """Redeem the 6-digit half, which needs the address too - a code alone
    would otherwise be guessable against every account at once. None on any
    failure (unknown address, no pending login, expired, wrong code, or budget
    exhausted); a wrong guess spends one of MAX_TRIES, and the last one voids
    the pending login so brute force has to start over from a new mail."""
    if not code:
        return None
    now = _now().isoformat(timespec="seconds")
    email = normalize_email(email)
    with closing(connect()) as conn, conn:
        # the hash comparison happens inside the claim so it stays atomic; a
        # timing side channel on comparing two SHA-256 hex digests is not
        # reachable through HTTP jitter
        row = conn.execute(
            _CLAIM + " WHERE email = ? AND magic_code = ? AND magic_expires >= ?"
                     "   AND magic_tries < ? AND code_fails < ?"
                     " RETURNING id, name, pending_name",
            (now, email, _token_hash(code), now, MAX_TRIES, MAX_CODE_FAILS_PER_DAY),
        ).fetchone()
        if row is not None:
            return _session_for(conn, row)

        # a miss: spend one from both budgets, and void the login when either
        # runs out. code_fails survives re-issuing, magic_tries does not.
        row = conn.execute(
            "SELECT id, magic_code, magic_expires, magic_tries FROM users"
            " WHERE email = ?",
            (email,),
        ).fetchone()
        if row is None or row["magic_code"] is None:
            return None
        spent = _roll_budget(conn, row["id"])["code_fails"]
        if spent >= MAX_CODE_FAILS_PER_DAY:
            # Out of guesses for today: refuse the code but leave the pending
            # login untouched. Voiding it here would hand an attacker a denial
            # of login - one guess after each mail would kill every link the
            # owner requests. The link half is unaffected by this budget, so
            # whoever actually reads the mailbox still gets in.
            return None
        conn.execute(
            "UPDATE users SET code_fails = ? WHERE id = ?", (spent + 1, row["id"])
        )
        if row["magic_expires"] < now or row["magic_tries"] + 1 >= MAX_TRIES:
            _clear_magic(conn, row["id"])
        else:
            conn.execute(
                "UPDATE users SET magic_tries = magic_tries + 1 WHERE id = ?",
                (row["id"],),
            )
        return None


def session_user(token: str | None) -> dict | None:
    """The account behind a session cookie, or None. Expired sessions are
    deleted on sight rather than by a sweeper."""
    if not token:
        return None
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT s.token_hash, s.ts, u.id, u.name FROM sessions s"
            " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        created = datetime.fromisoformat(row["ts"])
        if _now() - created > timedelta(days=SESSION_DAYS):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
            return None
    return {"id": row["id"], "name": row["name"]}


def logout(token: str | None) -> None:
    if not token:
        return
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def delete_account(name: str) -> bool:
    """GDPR erasure helper (manual, on request via kontakt@): drops the
    account, its sessions and votes (cascade), and anonymizes authored posts
    in place - the stories stay, the name goes."""
    with closing(connect()) as conn, conn:
        cur = conn.execute("DELETE FROM users WHERE name = ?", (name,))
        if cur.rowcount == 0:
            return False
        conn.execute("UPDATE stories SET author = '' WHERE author = ?", (name,))
        conn.execute("UPDATE comments SET author = '' WHERE author = ?", (name,))
    return True
