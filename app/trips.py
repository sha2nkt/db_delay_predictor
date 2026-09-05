"""Booked trips: the itineraries behind an account's "Meine Fahrten" page.

The bookmark beside a booking button files the journey it stands for, and
so does a signed-in press on "Auf bahn.de buchen" itself. Nothing comes back
from bahn.de, so an entry is a press, not a confirmed booking: the page says
so, and lets the visitor drop any entry they did not go through with.

Own SQLite file like reports.db, because the delays DuckDB is read-only and
swapped nightly. Every row is personal data - an account's travel plan - and
lives only as long as the account wants it: the cross on the page deletes
the row outright, account deletion drops them all, and a cap per account
keeps the earliest-departing ones from piling up forever.
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import delays, reports
from app.ratelimit import SlidingWindowLimiter

# own subdirectory: the pipeline rebuilds and prunes files directly under data/
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trips" / "trips.db"

MAX_LEGS = 12
MAX_SNAPSHOT_BYTES = 50_000
MAX_NAME_LENGTH = 200
MAX_URL_LENGTH = 2000
# only the booking mask the page itself builds is worth keeping
BAHN_URL_PREFIX = "https://www.bahn.de/"
# how many trips one account keeps; past the cap the earliest departures go
MAX_PER_ACCOUNT = 200
# one press files at most an outbound and a return
MAX_JOURNEYS_PER_PRESS = 2

KINDS = ("oneway", "outbound", "return")
VIAS = ("card", "summary", "report-modal", "add")

# a press is one small write by a signed-in account; this budget only keeps a
# script from filling the table
record_limiter = SlidingWindowLimiter(
    burst_limit=10, burst_window=60, sustained_limit=100, sustained_window=3600
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
  id          INTEGER PRIMARY KEY,
  uid         TEXT NOT NULL,
  journey_sig TEXT NOT NULL UNIQUE,
  journey_key TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL DEFAULT 'oneway',
  lang        TEXT NOT NULL DEFAULT 'de',
  from_name   TEXT NOT NULL,
  from_id     TEXT NOT NULL DEFAULT '',
  to_name     TEXT NOT NULL,
  to_id       TEXT NOT NULL DEFAULT '',
  departure   TEXT NOT NULL,
  arrival     TEXT NOT NULL,
  price       REAL,
  dticket     INTEGER NOT NULL DEFAULT 0,
  bahn_url    TEXT NOT NULL DEFAULT '',
  via         TEXT NOT NULL DEFAULT 'card',
  snapshot    TEXT NOT NULL,
  created_ts  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trips_uid_departure ON trips(uid, departure);
"""


class TripError(ValueError):
    """Invalid press payload; the message is safe to show as a 422 detail."""


# added after the first rows existed: CREATE TABLE IF NOT EXISTS leaves an
# older file alone, so the column is bolted on here
_LATER_COLUMNS = {"verdict": "TEXT"}


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.executescript(_SCHEMA)
    have = {row[1] for row in conn.execute("PRAGMA table_info(trips)")}
    for name, kind in _LATER_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE trips ADD COLUMN {name} {kind}")
    conn.row_factory = sqlite3.Row
    return conn


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def berlin_now() -> str:
    """Berlin wall-clock time in the naive ISO form bahn.de's sollzeit uses,
    so the page can sort a trip into past or upcoming by string compare."""
    return datetime.now(delays.BERLIN).replace(tzinfo=None).isoformat(timespec="seconds")


def _name(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TripError(f"missing {what}")
    return value.strip()[:MAX_NAME_LENGTH]


def _stamp(value, what: str) -> str:
    """A sollzeit as the page carries it: Berlin-local naive ISO, seconds kept
    so rows sort by string."""
    if not isinstance(value, str):
        raise TripError(f"missing {what}")
    try:
        return delays.to_berlin_naive(value).isoformat(timespec="seconds")
    except ValueError:
        raise TripError(f"invalid {what}")


def _validate(journey: dict) -> dict:
    """The row fields a journey object yields, or TripError."""
    if not isinstance(journey, dict):
        raise TripError("invalid journey")
    if len(json.dumps(journey)) > MAX_SNAPSHOT_BYTES:
        raise TripError("snapshot too large")
    legs = journey.get("legs")
    if not isinstance(legs, list) or not 1 <= len(legs) <= MAX_LEGS:
        raise TripError("invalid legs")
    if not all(isinstance(leg, dict) for leg in legs):
        raise TripError("invalid legs")
    first, last = legs[0], legs[-1]
    origin, dest = first.get("origin") or {}, last.get("destination") or {}
    departure = _stamp(first.get("plannedDeparture"), "departure")
    arrival = _stamp(last.get("plannedArrival"), "arrival")
    if arrival < departure:
        raise TripError("arrival before departure")
    now = datetime.now(delays.BERLIN).replace(tzinfo=None)
    if datetime.fromisoformat(departure) > now + timedelta(days=400):
        raise TripError("journey too far in the future")
    price = journey.get("price")
    if price is not None and not isinstance(price, (int, float)):
        raise TripError("invalid price")
    return {
        "from_name": _name(origin.get("name"), "origin"),
        "to_name": _name(dest.get("name"), "destination"),
        "departure": departure,
        "arrival": arrival,
        "price": float(price) if price is not None else None,
        "dticket": 1 if journey.get("dticketCovered") else 0,
    }


def _raw(value) -> str:
    # the JS side interpolates with `?? ""`: null and missing become "", a
    # number keeps its digits
    return "" if value is None else str(value)


def journey_key(journey: dict) -> str:
    """The page's identity for an itinerary: every leg as
    train|from|to|departure|arrival straight from the journey object, joined.
    tripKey in static/app.js builds the same string from the same object, so
    the page can light the bookmarks of journeys already filed without a
    round trip per card."""
    return "\n".join(
        "|".join(_raw(v) for v in (
            (leg.get("line") or {}).get("fahrtNr"), (leg.get("origin") or {}).get("id"),
            (leg.get("destination") or {}).get("id"),
            leg.get("plannedDeparture"), leg.get("plannedArrival"),
        ))
        for leg in journey.get("legs") or []
    )


def _journey_sig(uid: str, key: str) -> str:
    """One row per account and itinerary: the same legs pressed twice update
    the row rather than add a second one."""
    return hashlib.sha256("\n".join([uid, key]).encode()).hexdigest()


def _clean_url(url) -> str:
    if not isinstance(url, str) or not url.startswith(BAHN_URL_PREFIX):
        raise TripError("invalid booking url")
    if len(url) > MAX_URL_LENGTH:
        raise TripError("booking url too long")
    return url


def record(uid: str, lang: str, via: str, url: str, journeys: list[dict],
           search: dict | None) -> list[dict]:
    """Blocking - run it off the event loop.

    File the journeys behind one press of the booking button. `journeys` is a
    list of {"kind", "journey"}: one "oneway" entry, or an "outbound" and a
    "return" from the round-trip summary. Validates every journey before
    writing any; a journey this account already filed is refreshed in place
    (`created` False). Each result carries the page's `key` for the journey
    so the bookmark it came from can light up. Raises TripError.
    """
    if via not in VIAS:
        raise TripError("invalid via")
    if not isinstance(journeys, list) or not 1 <= len(journeys) <= MAX_JOURNEYS_PER_PRESS:
        raise TripError("invalid journeys")
    url = _clean_url(url)
    # the search's station ids (bahn.de location ids, not the legs' station
    # numbers): what the page needs to rebuild the past-mode search for a
    # trip once its day has passed
    search = search if isinstance(search, dict) else {}
    ids = {
        key: str(search.get(param) or "")[:MAX_NAME_LENGTH]
        for key, param in (("from_id", "fromId"), ("to_id", "toId"))
    }
    rows = []
    for entry in journeys:
        if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
            raise TripError("invalid kind")
        journey = entry.get("journey")
        fields = _validate(journey)
        snapshot = json.dumps(
            {"journey": journey, "search": search or {}, "shownAt": _utcnow()},
            ensure_ascii=False,
        )
        key = journey_key(journey)
        rows.append((entry["kind"], key, _journey_sig(uid, key), fields, snapshot))
    out = []
    with closing(_open()) as conn, conn:
        for kind, key, sig, fields, snapshot in rows:
            existing = conn.execute(
                "SELECT id FROM trips WHERE journey_sig = ?", (sig,)
            ).fetchone()
            conn.execute(
                "INSERT INTO trips (uid, journey_sig, journey_key, kind, lang, from_name,"
                " from_id, to_name, to_id, departure, arrival, price, dticket, bahn_url,"
                " via, snapshot, created_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(journey_sig) DO UPDATE SET"
                " kind = excluded.kind, lang = excluded.lang, price = excluded.price,"
                " from_id = excluded.from_id, to_id = excluded.to_id,"
                " dticket = excluded.dticket, bahn_url = excluded.bahn_url,"
                " via = excluded.via, snapshot = excluded.snapshot,"
                " created_ts = excluded.created_ts",
                (
                    uid, sig, key, kind, lang, fields["from_name"], ids["from_id"],
                    fields["to_name"], ids["to_id"], fields["departure"],
                    fields["arrival"], fields["price"], fields["dticket"], url, via,
                    snapshot, _utcnow(),
                ),
            )
            row_id = existing["id"] if existing else conn.execute(
                "SELECT id FROM trips WHERE journey_sig = ?", (sig,)
            ).fetchone()["id"]
            out.append({"id": row_id, "kind": kind, "key": key, "created": existing is None})
        # the cap: the earliest departures go first, so what the page shows
        # under "next" survives a long history of past trips
        conn.execute(
            "DELETE FROM trips WHERE uid = ? AND id NOT IN"
            " (SELECT id FROM trips WHERE uid = ? ORDER BY departure DESC, id DESC LIMIT ?)",
            (uid, uid, MAX_PER_ACCOUNT),
        )
    return out


_STATS_KEYS = ("medianDelay", "maxDelay", "daysMatched", "canceledDays")
_TIGHT_KEYS = ("legIndex", "station", "transferMinutes", "medianDelay", "unlikely")


def _snapshot_view(snapshot: str) -> dict:
    """What the page draws of a stored journey: its legs with the delay
    statistics they showed at the time (per-day detail dropped), the tight
    transfers the search flagged, and the window the statistics covered."""
    try:
        data = json.loads(snapshot)
        journey, search = data["journey"], data.get("search") or {}
        legs = journey["legs"]
    except (ValueError, KeyError, TypeError):
        return {"legs": [], "tightTransfers": [], "window": 7}
    out = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        line = leg.get("line") or {}
        row = {
            "walking": bool(leg.get("walking")),
            "line": line.get("name"),
            "product": line.get("product"),
            "origin": (leg.get("origin") or {}).get("name"),
            "destination": (leg.get("destination") or {}).get("name"),
            "plannedDeparture": leg.get("plannedDeparture"),
            "plannedArrival": leg.get("plannedArrival"),
        }
        # the key alone says "tracked": a null value is a train without data
        if "delayStats" in leg:
            stats = leg["delayStats"]
            row["delayStats"] = {k: stats.get(k) for k in _STATS_KEYS} if isinstance(stats, dict) else None
        out.append(row)
    tight = [
        {k: tt.get(k) for k in _TIGHT_KEYS}
        for tt in (journey.get("tightTransfers") or []) if isinstance(tt, dict)
    ]
    window = search.get("window")
    return {
        "legs": out,
        "tightTransfers": tight,
        "window": window if isinstance(window, int) and 0 < window < 1000 else 7,
    }


def _arrival_delay(snapshot: str) -> tuple[int | None, bool]:
    """(delay at the destination on the day in minutes, cancelled) for a
    stored journey: that of the last leg a lookup can resolve - the same gate
    and lookup key the report resolver uses - or (None, False) when nothing is
    known. A cancelled arrival is (None, True)."""
    try:
        legs = json.loads(snapshot)["journey"]["legs"]
    except (ValueError, KeyError, TypeError):
        return None, False
    for leg in reversed(legs):
        if not isinstance(leg, dict) or leg.get("walking"):
            continue
        line = leg.get("line") or {}
        if line.get("product") in reports.UNTRACKED_PRODUCTS:
            continue
        if not (line.get("fahrtNr") and leg.get("plannedArrival") and (leg.get("destination") or {}).get("id")):
            continue
        try:
            train, eva, arrival = reports.leg_lookup_key(leg)
        except ValueError:
            return None, False
        hit = delays.leg_delay_on_date(train, eva, arrival)
        if hit is None:
            return None, False
        return hit["delayMin"], bool(hit["canceled"])
    return None, False


def verdict_outcome(verdict: dict) -> tuple[int | None, bool]:
    """(minutes lost at the destination, a cancellation was involved) as the
    page's tally reads a stored check: the simulated arrival delay when the
    check rode through a miss, else the final leg's, and whether a cancelled
    train was what the check had to route around."""
    canceled = bool(verdict.get("arrivalCanceled")) or any(
        bool(mt.get("canceled")) for mt in verdict.get("missedTransfers") or []
        if isinstance(mt, dict)
    )
    delay = verdict.get("arrivalDelay")
    return (delay if isinstance(delay, (int, float)) else None), canceled


def mine(uid: str) -> list[dict]:
    """Every trip of the account, earliest departure first. The page splits
    them into past and upcoming against berlin_now(); past trips carry the
    day's delay at the destination for the page's tally - from the stored
    check where one exists (`resolved`), else the final leg's delay alone."""
    with closing(_open()) as conn:
        rows = conn.execute(
            "SELECT * FROM trips WHERE uid = ? ORDER BY departure, id", (uid,)
        ).fetchall()
    now = berlin_now()
    out = []
    for r in rows:
        past = r["arrival"] <= now
        delay, canceled, resolved = None, False, False
        if past and r["verdict"]:
            try:
                delay, canceled = verdict_outcome(json.loads(r["verdict"]))
                resolved = True
            except (ValueError, AttributeError):
                pass
        if past and not resolved:
            delay, canceled = _arrival_delay(r["snapshot"])
        out.append({
            "id": r["id"],
            "key": r["journey_key"],
            "kind": r["kind"],
            "fromName": r["from_name"],
            "fromId": r["from_id"],
            "toName": r["to_name"],
            "toId": r["to_id"],
            "departure": r["departure"],
            "arrival": r["arrival"],
            "delay": delay,
            "canceled": canceled,
            "resolved": resolved,
            "price": r["price"],
            "dticket": bool(r["dticket"]),
            "bahnUrl": r["bahn_url"],
            **_snapshot_view(r["snapshot"]),
            "createdTs": r["created_ts"],
        })
    return out


def _stamp_or_none(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return delays.to_berlin_naive(value).isoformat(timespec="seconds")
    except ValueError:
        return None


def _clean_leg(raw) -> dict | None:
    """A stored leg in exactly the shape normalize_leg produces, every key
    present: the snapshot came from the page, so the past-mode check must
    not trip over a field that is missing or of the wrong type."""
    if not isinstance(raw, dict):
        return None
    line, origin, dest = (raw.get(k) or {} for k in ("line", "origin", "destination"))
    if not all(isinstance(x, dict) for x in (line, origin, dest)):
        return None
    return {
        "walking": bool(raw.get("walking")),
        "line": {k: line.get(k) for k in ("name", "fahrtNr", "product", "operator")},
        "origin": {"id": origin.get("id"), "name": origin.get("name")},
        "destination": {"id": dest.get("id"), "name": dest.get("name")},
        "plannedDeparture": _stamp_or_none(raw.get("plannedDeparture")),
        "plannedArrival": _stamp_or_none(raw.get("plannedArrival")),
    }


def journey_legs(uid: str, trip_id: int) -> dict | None:
    """The stored itinerary of one of the account's trips - its legs cleaned
    for the past-mode check, plus the row's departure and arrival stamps -
    or None when the trip is not this account's."""
    with closing(_open()) as conn:
        row = conn.execute(
            "SELECT snapshot, departure, arrival FROM trips WHERE id = ? AND uid = ?",
            (trip_id, uid),
        ).fetchone()
    if row is None:
        return None
    try:
        raw_legs = json.loads(row["snapshot"])["journey"]["legs"]
    except (ValueError, KeyError, TypeError):
        return None
    legs = [leg for leg in (_clean_leg(r) for r in raw_legs) if leg]
    return {"legs": legs, "departure": row["departure"], "arrival": row["arrival"]}


def stored_verdict(uid: str, trip_id: int) -> dict | None:
    """The check already run for this trip, or None."""
    with closing(_open()) as conn:
        row = conn.execute(
            "SELECT verdict FROM trips WHERE id = ? AND uid = ?", (trip_id, uid)
        ).fetchone()
    if row is None or not row["verdict"]:
        return None
    try:
        return json.loads(row["verdict"])
    except ValueError:
        return None


def store_verdict(uid: str, trip_id: int, verdict: dict) -> None:
    """Keep a finished check with its trip: the day is in the nightly data,
    so the answer will not change, and the tally and the page read it back
    without riding the simulation again."""
    with closing(_open()) as conn, conn:
        conn.execute(
            "UPDATE trips SET verdict = ? WHERE id = ? AND uid = ?",
            (json.dumps(verdict, ensure_ascii=False), trip_id, uid),
        )


def remove(uid: str, trip_id: int) -> bool:
    """The cross on the page: the row is gone for good. False when the trip
    is not this account's."""
    with closing(_open()) as conn, conn:
        cur = conn.execute(
            "DELETE FROM trips WHERE id = ? AND uid = ?", (trip_id, uid)
        )
        return cur.rowcount == 1


def forget_account(uid: str) -> None:
    """GDPR erasure (auth.delete_account): nothing of the account stays."""
    with closing(_open()) as conn, conn:
        conn.execute("DELETE FROM trips WHERE uid = ?", (uid,))
