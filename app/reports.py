"""Journey-report orders: an itinerary snapshot stored against a Delay Stories
account, resolved against the delay data after the journey and sent as a
comparison report to the account's address.

The bell on a journey card is the whole sign-up. The account (app/auth.py)
already proved its address, so there is no email form and no double opt-in of
its own here: one signed-in press is the order, a second one withdraws it.

Like feedback.db this lives in its own SQLite file because the delays DuckDB
is read-only and swapped nightly. Rows carry personal data - the account id,
its email, its username - only while a report is open or freshly sent:
cancelling, the unsubscribe link in a mail, account deletion and the daily
job's retention sweep all scrub those three fields, and the anonymous
snapshot/actuals stay for aggregate statistics.
"""

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app import delays
from app.ratelimit import SlidingWindowLimiter

# own subdirectory: the pipeline rebuilds and prunes files directly under data/
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "reports" / "reports.db"

MAX_LEGS = 12
MAX_SNAPSHOT_BYTES = 50_000
MAX_OPEN_PER_ACCOUNT = 20
# after this many days past the travel date the report is sent with whatever
# resolved, so a pipeline outage cannot hold orders forever
RESOLVE_TIMEOUT_DAYS = 10
# how long a settled row (sent, failed) keeps the account behind it: long
# enough for "unsubscribe" in the mail to still find something to withdraw
RETENTION_DAYS = 30

# An order is one small write per bell press by a signed-in account; this
# budget only keeps a script from filling the table.
subscribe_limiter = SlidingWindowLimiter(
    burst_limit=10, burst_window=60, sustained_limit=100, sustained_window=3600
)

# matches UNTRACKED_PRODUCTS in app/main.py (importing it would be circular)
UNTRACKED_PRODUCTS = {"BUS", "TRAM", "UBAHN", "SCHIFF", "ANRUFPFLICHTIG"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
  id            INTEGER PRIMARY KEY,
  uid           TEXT,
  unsub_token   TEXT NOT NULL UNIQUE,
  email         TEXT,
  name          TEXT NOT NULL DEFAULT '',
  lang          TEXT NOT NULL DEFAULT 'de',
  status        TEXT NOT NULL DEFAULT 'active',
  journey_sig   TEXT NOT NULL,
  journey_key   TEXT NOT NULL DEFAULT '',
  snapshot      TEXT NOT NULL,
  travel_date   TEXT NOT NULL,
  actuals       TEXT,
  created_ts    TEXT NOT NULL,
  sent_ts       TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sub_status_travel ON subscriptions(status, travel_date);
CREATE INDEX IF NOT EXISTS idx_sub_uid ON subscriptions(uid);
CREATE INDEX IF NOT EXISTS idx_sub_sig ON subscriptions(journey_sig);
"""

# what every scrub drops; the row itself stays as an anonymous statistic
_SCRUB = "uid = NULL, email = NULL, name = ''"


class SnapshotError(ValueError):
    """Invalid order payload; the message is safe to show as a 422 detail."""


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tracked_leg_indices(journey: dict) -> list[tuple[int, dict]]:
    """The legs a later actuals lookup can resolve, with their positions in
    journey["legs"]. The gate mirrors normalize_leg in app/main.py: not walking,
    tracked product, and carrying fahrtNr + plannedArrival + destination id."""
    legs = journey.get("legs")
    if not isinstance(legs, list) or not 1 <= len(legs) <= MAX_LEGS:
        raise SnapshotError("invalid legs")
    out = []
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict) or leg.get("walking"):
            continue
        line = leg.get("line") or {}
        dest = leg.get("destination") or {}
        if line.get("product") in UNTRACKED_PRODUCTS:
            continue
        if not (line.get("fahrtNr") and leg.get("plannedArrival") and dest.get("id")):
            continue
        try:
            delays.to_berlin_naive(str(leg["plannedArrival"]))
        except ValueError:
            raise SnapshotError("invalid leg time")
        out.append((i, leg))
    if not out:
        raise SnapshotError("no tracked legs")
    return out


def leg_lookup_key(leg: dict) -> tuple[str, str, datetime]:
    """(train, eva, planned arrival) exactly as normalize_leg derives them, so the
    resolver joins on the same key the live site uses."""
    train = str(leg["line"]["fahrtNr"]).replace(" ", "")
    eva = delays.pad_eva(str(leg["destination"]["id"]))
    arrival = delays.to_berlin_naive(str(leg["plannedArrival"]))
    return train, eva, arrival


def _raw(value) -> str:
    # the JS side interpolates with `?? ""`: null and missing become "", a
    # number keeps its digits
    return "" if value is None else str(value)


def journey_key(journey: dict) -> str:
    """The page's identity for an itinerary: the legs it marked resolvable (a
    delayStats key - walks and untracked products never carry one), each as
    train|stop|arrival straight from the journey object, sorted and joined.
    reportKey in static/app.js builds the same string from the same object,
    so the page can tell which cards already carry an order without a round
    trip per card. Only for that matching: the dedup below keys on the
    normalized lookup tuples, which are what resolvability is decided on."""
    keys = []
    for leg in journey.get("legs") or []:
        if not isinstance(leg, dict) or "delayStats" not in leg:
            continue
        line = leg.get("line") or {}
        dest = leg.get("destination") or {}
        keys.append(
            f"{_raw(line.get('fahrtNr'))}|{_raw(dest.get('id'))}|{_raw(leg.get('plannedArrival'))}"
        )
    return "\n".join(sorted(keys))


def _validate(journey: dict) -> tuple[list[tuple[int, dict]], str]:
    if len(json.dumps(journey)) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("snapshot too large")
    tracked = tracked_leg_indices(journey)
    arrivals = [leg_lookup_key(leg)[2] for _, leg in tracked]
    now = datetime.now(delays.BERLIN).replace(tzinfo=None)
    if max(arrivals) <= now:
        raise SnapshotError("journey already arrived")
    if max(arrivals) > now + timedelta(days=400):
        raise SnapshotError("journey too far in the future")
    # service date of the last tracked arrival; overnight legs shift naturally
    return tracked, max(a.date() for a in arrivals).isoformat()


def _journey_sig(uid: str, tracked: list[tuple[int, dict]]) -> str:
    parts = sorted(
        f"{train}|{eva}|{arrival.isoformat()}"
        for train, eva, arrival in (leg_lookup_key(leg) for _, leg in tracked)
    )
    return hashlib.sha256("\n".join([uid, *parts]).encode()).hexdigest()


def subscribe(user: dict, lang: str, journey: dict, search: dict) -> dict:
    """Blocking - run it off the event loop.

    Order a report for this account and itinerary. `user` is the dict
    auth.account() returns (uid, email, name). Validates and stores the
    snapshot; a second press on the same journey hands back the existing
    order (`created` False) rather than a second row. Raises SnapshotError.
    """
    tracked, travel_date = _validate(journey)
    sig = _journey_sig(user["uid"], tracked)
    legs = journey.get("legs") or []
    first, last = legs[0], legs[-1]
    result = {
        "key": journey_key(journey),
        "travelDate": travel_date,
        "email": user["email"],
        "fromName": (search or {}).get("fromName") or (first.get("origin") or {}).get("name") or "",
        "toName": (search or {}).get("toName") or (last.get("destination") or {}).get("name") or "",
    }
    snapshot = json.dumps(
        {"journey": journey, "search": search or {}, "shownAt": _utcnow()},
        ensure_ascii=False,
    )
    with closing(_open()) as conn, conn:
        row = conn.execute(
            "SELECT id FROM subscriptions WHERE journey_sig = ? AND status = 'active'",
            (sig,),
        ).fetchone()
        if row:
            return result | {"id": row["id"], "created": False}
        open_count = conn.execute(
            "SELECT count(*) FROM subscriptions WHERE uid = ? AND status = 'active'",
            (user["uid"],),
        ).fetchone()[0]
        if open_count >= MAX_OPEN_PER_ACCOUNT:
            raise SnapshotError("too many open reports for this account")
        cur = conn.execute(
            "INSERT INTO subscriptions (uid, unsub_token, email, name, lang, journey_sig,"
            " journey_key, snapshot, travel_date, created_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user["uid"],
                secrets.token_urlsafe(24),
                user["email"],
                (user.get("name") or "")[:100],
                lang,
                sig,
                result["key"],
                snapshot,
                travel_date,
                _utcnow(),
            ),
        )
        return result | {"id": cur.lastrowid, "created": True}


def mine(uid: str) -> list[dict]:
    """The account's open orders, keyed for the page's bells."""
    with closing(_open()) as conn:
        rows = conn.execute(
            "SELECT id, journey_key, travel_date FROM subscriptions"
            " WHERE uid = ? AND status = 'active' ORDER BY travel_date, id",
            (uid,),
        ).fetchall()
    return [{"id": r["id"], "key": r["journey_key"], "travelDate": r["travel_date"]} for r in rows]


def cancel(uid: str, sub_id: int) -> bool:
    """The bell pressed again: this one order is withdrawn and its personal
    data dropped. False when the order is not this account's, or not open."""
    with closing(_open()) as conn, conn:
        cur = conn.execute(
            f"UPDATE subscriptions SET status = 'cancelled', {_SCRUB}"
            " WHERE id = ? AND uid = ? AND status = 'active'",
            (sub_id, uid),
        )
        return cur.rowcount == 1


def _scrub_account(conn: sqlite3.Connection, uid: str) -> None:
    conn.execute(
        f"UPDATE subscriptions SET {_SCRUB},"
        " status = CASE WHEN status = 'active' THEN 'cancelled' ELSE status END"
        " WHERE uid = ?",
        (uid,),
    )


def unsubscribe(token: str) -> bool:
    """The link in a mail: withdraws every open order of the account the mail
    went to and scrubs the account from all of its rows, sent ones included.
    Works without a login, and stays a success when clicked twice."""
    with closing(_open()) as conn, conn:
        row = conn.execute(
            "SELECT uid FROM subscriptions WHERE unsub_token = ?", (token,)
        ).fetchone()
        if row is None:
            return False
        if row["uid"] is not None:
            _scrub_account(conn, row["uid"])
        return True


def forget_account(uid: str) -> None:
    """GDPR erasure (auth.delete_account): the same scrub as the mail's link."""
    with closing(_open()) as conn, conn:
        _scrub_account(conn, uid)


def token_lang(token: str) -> str | None:
    """Language of the order an unsubscribe token belongs to, or None."""
    with closing(_open()) as conn:
        row = conn.execute(
            "SELECT lang FROM subscriptions WHERE unsub_token = ?", (token,)
        ).fetchone()
    return row["lang"] if row else None


# --- daily job ---


def scrub_old(apply: bool) -> int:
    """Drop the account from settled rows (sent or given up) older than
    RETENTION_DAYS; dry run counts only."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat(
        timespec="seconds"
    )
    where = (
        "uid IS NOT NULL AND status IN ('sent', 'failed')"
        " AND coalesce(sent_ts, created_ts) < ?"
    )
    with closing(_open()) as conn, conn:
        if apply:
            return conn.execute(f"UPDATE subscriptions SET {_SCRUB} WHERE {where}", (cutoff,)).rowcount
        return conn.execute(f"SELECT count(*) FROM subscriptions WHERE {where}", (cutoff,)).fetchone()[0]


def due_rows(parquet_max: str, today: str, limit: int) -> list[dict]:
    """Open orders ready to resolve: the data covers a day past the travel date
    (normal case, effectively D+2), or the timeout has passed regardless."""
    timeout_cutoff = (date.fromisoformat(today) - timedelta(days=RESOLVE_TIMEOUT_DAYS)).isoformat()
    with closing(_open()) as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE status = 'active'"
            " AND (travel_date < ? OR travel_date < ?)"
            " ORDER BY travel_date, id LIMIT ?",
            (parquet_max, timeout_cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_sent(sub_id: int, actuals: dict) -> None:
    with closing(_open()) as conn, conn:
        conn.execute(
            "UPDATE subscriptions SET status = 'sent', actuals = ?, sent_ts = ? WHERE id = ?",
            (json.dumps(actuals, ensure_ascii=False), _utcnow(), sub_id),
        )


def record_failure(sub_id: int, error: str, give_up: bool) -> None:
    with closing(_open()) as conn, conn:
        conn.execute(
            "UPDATE subscriptions SET attempts = attempts + 1, last_error = ?,"
            " status = CASE WHEN ? THEN 'failed' ELSE status END WHERE id = ?",
            (error[:500], give_up, sub_id),
        )
