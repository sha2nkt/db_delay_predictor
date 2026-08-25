"""Station horror stories: an anonymous mini-forum in its own SQLite file.

Same ownership story as feedback.py: the delays DuckDB is read-only and the
file is swapped out nightly, so durable state the app owns lives in a
per-module SQLite file under its own data/ subdirectory (the pipeline rebuilds
and prunes files directly under data/).

Reading is anonymous; posting, commenting and voting need an account (see
auth.py), which pins every post to one stable name and makes votes count once
per person instead of once per browser. Accounts themselves live in Firebase;
the only trace of one here is the opaque Firebase uid on its votes and taps
(what makes them count once) and the public username on its posts. The
per-IP throttles live in memory.
"""

import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.ratelimit import SlidingWindowLimiter

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stories" / "stories.db"

# Posting budget (stories + comments share it): a human venting about their
# commute stays far inside it; only scripted spam trips it. Votes get a looser
# budget of their own - toggling an arrow twice must not eat a posting slot.
write_limiter = SlidingWindowLimiter(
    burst_limit=5, burst_window=60, sustained_limit=20, sustained_window=3600
)
vote_limiter = SlidingWindowLimiter(
    burst_limit=15, burst_window=30, sustained_limit=200, sustained_window=3600
)

_ntfy = httpx.AsyncClient(timeout=5)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
  id           INTEGER PRIMARY KEY,
  ts           TEXT NOT NULL,
  from_station TEXT NOT NULL,
  to_station   TEXT NOT NULL DEFAULT '',
  departure    TEXT NOT NULL DEFAULT '',
  train        TEXT NOT NULL DEFAULT '',
  problem_other TEXT NOT NULL DEFAULT '',
  edited_ts    TEXT,
  deleted_ts   TEXT,
  title        TEXT NOT NULL,
  text         TEXT NOT NULL,
  author       TEXT NOT NULL DEFAULT ''
);
-- what went wrong on the leg, as stable ASCII codes rather than the words the
-- visitor saw: the labels are localized in the frontend, and a row here has
-- to stay countable across both languages ("how often is the WC broken at
-- Hannover?"). One row per problem so that stays a GROUP BY, not a LIKE.
CREATE TABLE IF NOT EXISTS story_problems (
  story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  code     TEXT NOT NULL,
  PRIMARY KEY (story_id, code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS comments (
  id        INTEGER PRIMARY KEY,
  story_id  INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
  ts        TEXT NOT NULL,
  author    TEXT NOT NULL DEFAULT '',
  text      TEXT NOT NULL,
  edited_ts TEXT,
  deleted_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_story ON comments(story_id);
"""

# The tables that pin something to an account, one DDL each so the migration
# below can recreate a single one. uid is the Firebase account id: opaque
# here, resolvable only in Firebase, and the one thing that makes a vote
# count once per person.
_ACCOUNT_TABLES = {
    "votes": """
CREATE TABLE IF NOT EXISTS votes (
  story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
  uid      TEXT NOT NULL,
  value    INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (story_id, uid)
) WITHOUT ROWID;
""",
    # a tap on a board tile: "this happened to me today, on this leg", with
    # no story behind it. Keyed per account, code and UTC day, so one person
    # can add one to each counter per day and a tap is a toggle rather than
    # a button to lean on. The leg columns mirror stories: a count with no
    # train and no station behind it would be a number nobody could do
    # anything with.
    "problem_reports": """
CREATE TABLE IF NOT EXISTS problem_reports (
  uid          TEXT NOT NULL,
  code         TEXT NOT NULL,
  day          TEXT NOT NULL,
  ts           TEXT NOT NULL,
  from_station TEXT NOT NULL,
  to_station   TEXT NOT NULL DEFAULT '',
  departure    TEXT NOT NULL DEFAULT '',
  train        TEXT NOT NULL DEFAULT '',
  problem_other TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (uid, code, day)
) WITHOUT ROWID;
""",
    # comments carry their own upvotes, on the same once-per-account terms
    # as stories; separate table because a comment id and a story id are
    # different namespaces and one shared column could not be a foreign key
    # to both
    "comment_votes": """
CREATE TABLE IF NOT EXISTS comment_votes (
  comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
  uid        TEXT NOT NULL,
  value      INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (comment_id, uid)
) WITHOUT ROWID;
""",
}

_FULL_SCHEMA = _SCHEMA + "".join(_ACCOUNT_TABLES.values())

# score, comment count and the viewer's own vote are computed, never stored: a
# count can't drift from the votes that back it, and the tables stay small
# enough that it never matters. The ? is the viewing account's uid; NULL (not
# logged in) matches no vote, so `voted` is 0 for anonymous readers.
_STORY_COLS = (
    "s.id, s.ts, s.from_station, s.to_station, s.departure, s.train, s.problem_other,"
    " s.edited_ts, s.deleted_ts,"
    " s.title, s.text, s.author,"
    " (SELECT COALESCE(SUM(v.value), 0) FROM votes v WHERE v.story_id = s.id) AS score,"
    " (SELECT v.value FROM votes v WHERE v.story_id = s.id AND v.uid = ?) AS voted,"
    " (SELECT COUNT(*) FROM comments c WHERE c.story_id = s.id) AS comments,"
    " (SELECT GROUP_CONCAT(code) FROM story_problems p WHERE p.story_id = s.id)"
    "   AS problems"
)

# The order the compose form offers them in, which is also the order they read
# back in - GROUP_CONCAT has no order of its own, and alphabetical would put
# "ac" before "delay", burying the one that matters most.
PROBLEMS = ("delay", "cancelled", "missed", "ac", "wc", "crowding", "wifi", "other")
_PROBLEM_RANK = {code: i for i, code in enumerate(PROBLEMS)}


def _story_dict(row: sqlite3.Row) -> dict:
    story = dict(row)
    story["voted"] = int(story["voted"] or 0)  # -1, 0 or 1, Reddit-style
    # the client needs to know that an edit or a removal happened, never when:
    # a timestamp would just be one more thing to localize
    story["edited"] = story.pop("edited_ts") is not None
    story["deleted"] = story.pop("deleted_ts") is not None
    codes = (story["problems"] or "").split(",") if story["problems"] else []
    # unknown codes would be ones this build no longer offers; drop rather than
    # render a chip with no label
    story["problems"] = sorted(
        (c for c in codes if c in _PROBLEM_RANK), key=_PROBLEM_RANK.get
    )
    return story


# Accounts used to be rows here (users + sessions, keyed by an integer id)
# and moved out to Firebase. The tables that pinned something to an account
# are rebuilt around the Firebase uid; rows from the SQLite era keep theirs
# under 'legacy-<id>', the uid scripts/import_users_to_firebase.py gives
# that account in Firebase, so scores and taps survive the move. Same column
# order as the new tables, with the id swapped for its uid form.
_LEGACY_COLUMNS = {
    "votes": "story_id, 'legacy-' || user_id, value",
    "comment_votes": "comment_id, 'legacy-' || user_id, value",
    "problem_reports": "'legacy-' || user_id, code, day, ts, from_station,"
                       " to_station, departure, train, problem_other",
}


def _move_accounts_out(conn: sqlite3.Connection) -> None:
    """One-time: re-key the per-account tables by uid, drop the sessions,
    and set the old users table aside as users_legacy for the import script
    to read and then drop - deleting it here would make forgetting to run
    the import the same as losing every account. One explicit transaction:
    the module's DML auto-begins but a read-only caller never commits, and
    a half-applied rebuild would strand rows in an *_old table."""
    def columns(table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    rebuild = [t for t in _LEGACY_COLUMNS if "user_id" in columns(t)]
    if not rebuild and "sessions" not in tables and "users" not in tables:
        return
    conn.execute("BEGIN")
    for table in rebuild:
        if table != "problem_reports" and "value" not in columns(table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN value INTEGER NOT NULL DEFAULT 1")
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        conn.execute(_ACCOUNT_TABLES[table])
        conn.execute(f"INSERT INTO {table} SELECT {_LEGACY_COLUMNS[table]} FROM {table}_old")
        conn.execute(f"DROP TABLE {table}_old")
    conn.execute("DROP TABLE IF EXISTS sessions")
    if "users" in tables and "users_legacy" not in tables:
        conn.execute("ALTER TABLE users RENAME TO users_legacy")
    conn.execute("DROP INDEX IF EXISTS idx_users_email")
    conn.commit()


# stories predates the journey fields: a story used to name one station, and
# now names the leg it happened on. The old column IS the origin, so it is
# renamed rather than dropped - existing stories keep their station as "from"
# and simply have no destination and no departure time.
_STORY_COLUMNS = {
    "to_station": "TEXT NOT NULL DEFAULT ''",
    "departure": "TEXT NOT NULL DEFAULT ''",
    "train": "TEXT NOT NULL DEFAULT ''",
    "problem_other": "TEXT NOT NULL DEFAULT ''",
    "edited_ts": "TEXT",
    "deleted_ts": "TEXT",
}

# comments gained the same two after edit/delete arrived
_COMMENT_COLUMNS = {"edited_ts": "TEXT", "deleted_ts": "TEXT"}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_FULL_SCHEMA)
    _move_accounts_out(conn)
    have = {row["name"] for row in conn.execute("PRAGMA table_info(stories)")}
    if "station" in have and "from_station" not in have:
        conn.execute("ALTER TABLE stories RENAME COLUMN station TO from_station")
        have.add("from_station")
    for col, decl in _STORY_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE stories ADD COLUMN {col} {decl}")
    have = {row["name"] for row in conn.execute("PRAGMA table_info(comments)")}
    for col, decl in _COMMENT_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE comments ADD COLUMN {col} {decl}")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_story(
    from_station: str, to_station: str, departure: str, train: str,
    problems: list[str], problem_other: str,
    title: str, text: str, author: str,
) -> dict:
    """A story hangs off the leg it happened on, and lists what went wrong on
    it (see PROBLEMS; unknown codes are dropped rather than stored).
    Only the origin is required:
    "to" and "departure" are empty strings for a story that is about standing
    on one platform with nowhere to go, and for every story written before the
    journey fields existed. Blocking - run it off the event loop (as are all
    functions below)."""
    with closing(connect()) as conn, conn:
        # free text belongs to the "other" chip; without it there is nothing
        # for it to specify, so it is not kept
        if "other" not in problems:
            problem_other = ""
        cur = conn.execute(
            "INSERT INTO stories (ts, from_station, to_station, departure, train,"
            " problem_other, title, text, author)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), from_station, to_station, departure, train, problem_other,
             title, text, author),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO story_problems (story_id, code) VALUES (?, ?)",
            [(cur.lastrowid, code) for code in problems if code in _PROBLEM_RANK],
        )
        row = conn.execute(
            f"SELECT {_STORY_COLS} FROM stories s WHERE s.id = ?",
            (None, cur.lastrowid),
        ).fetchone()
    return _story_dict(row)


# "top" feeds the strip above the list, the rest are the list's own sort
# tabs; ties fall back to newest first so paging stays stable
SORTS = {
    "new": "id DESC",
    "top": "score DESC, id DESC",
    "liked": "score DESC, id DESC",
    "commented": "comments DESC, id DESC",
}


def list_stories(sort: str, limit: int, offset: int, uid: str | None = None) -> list[dict]:
    inner = f"SELECT {_STORY_COLS} FROM stories s"
    # a tombstone keeps its votes and thread but has nothing to rank; it stays
    # on the new list only, where the replies hang off it. And an unvoted story
    # is not "top rated" - it is already on the new list.
    conds = []
    if sort != "new":
        conds.append("deleted_ts IS NULL")
    if sort == "top":
        conds.append("score > 0")
    where = f" WHERE {' AND '.join(conds)}" if conds else ""
    sql = f"SELECT * FROM ({inner}){where} ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?"
    with closing(connect()) as conn:
        rows = conn.execute(sql, (uid, limit, offset)).fetchall()
    return [_story_dict(r) for r in rows]


SPANS = ("week", "month", "year", "all")


def get_story(story_id: int, uid: str | None = None) -> dict | None:
    """One story by id, for its permalink and embed pages - tombstone included,
    the card knows how to render one; None when it never existed."""
    with closing(connect()) as conn:
        row = conn.execute(
            f"SELECT {_STORY_COLS} FROM stories s WHERE s.id = ?", (uid, story_id)
        ).fetchone()
    return _story_dict(row) if row else None


def _span_start(span: str) -> str | None:
    """Calendar boundaries, not rolling windows: "month" means "this month",
    the way a passenger would say it. UTC on both sides, so the ISO strings
    compare as strings against the ts column."""
    now = datetime.now(timezone.utc)
    day_one = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if span == "week":
        start = day_one - timedelta(days=now.weekday())
    elif span == "month":
        start = day_one.replace(day=1)
    elif span == "year":
        start = day_one.replace(month=1, day=1)
    else:
        return None
    return start.isoformat(timespec="seconds")


def count_problems(span: str = "month") -> dict[str, int]:
    """How often each problem code was reported within the span - through a
    story or with a tap on the board - zeros included so every board row
    exists in every span. Tombstoned stories have no story_problems rows
    left, so they drop out on their own."""
    since = _span_start(span)
    where = "" if since is None else " WHERE ts >= ?"
    params: tuple = () if since is None else (since, since)
    sql = (
        "SELECT code, COUNT(*) AS n FROM ("
        "  SELECT p.code, s.ts FROM story_problems p"
        "  JOIN stories s ON s.id = p.story_id" + where +
        "  UNION ALL"
        "  SELECT code, ts FROM problem_reports" + where +
        ") GROUP BY code"
    )
    counts = dict.fromkeys(PROBLEMS, 0)
    with closing(connect()) as conn:
        for row in conn.execute(sql, params):
            if row["code"] in counts:
                counts[row["code"]] = row["n"]
    return counts


def _today() -> str:
    return _now()[:10]


def my_reports(uid: str) -> list[str]:
    """The codes this account has tapped today, in board order, so the tiles
    can show which counters already carry the viewer's one."""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT code FROM problem_reports WHERE uid = ? AND day = ?",
            (uid, _today()),
        ).fetchall()
    return sorted((r["code"] for r in rows if r["code"] in _PROBLEM_RANK),
                  key=_PROBLEM_RANK.get)


def set_report(
    uid: str, code: str, vote: bool,
    from_station: str = "", to_station: str = "", departure: str = "", train: str = "",
    problem_other: str = "",
) -> bool | None:
    """Idempotent set/clear of this account's tap on a board tile for today;
    None for a code the board doesn't have. Like set_vote, a repeated request
    is a no-op, not a second report - the day's one row keeps the leg that
    was named last. The free text belongs to the "other" tile alone, as on
    a story."""
    if code not in _PROBLEM_RANK:
        return None
    if code != "other":
        problem_other = ""
    with closing(connect()) as conn, conn:
        if vote:
            conn.execute(
                "INSERT OR REPLACE INTO problem_reports"
                " (uid, code, day, ts, from_station, to_station, departure, train,"
                " problem_other)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, code, _today(), _now(),
                 from_station, to_station, departure, train, problem_other),
            )
        else:
            conn.execute(
                "DELETE FROM problem_reports WHERE uid = ? AND code = ? AND day = ?",
                (uid, code, _today()),
            )
    return vote


def set_vote(story_id: int, uid: str, vote: int) -> dict | None:
    """Idempotent set of this user's vote, Reddit-style: 1 up, -1 down, 0
    clears it. None when the story doesn't exist. Score is the net sum, so a
    repeated request is a no-op, not a second vote."""
    with closing(connect()) as conn, conn:
        if conn.execute("SELECT 1 FROM stories WHERE id = ?", (story_id,)).fetchone() is None:
            return None
        if vote:
            conn.execute(
                "INSERT OR REPLACE INTO votes (story_id, uid, value) VALUES (?, ?, ?)",
                (story_id, uid, vote),
            )
        else:
            conn.execute(
                "DELETE FROM votes WHERE story_id = ? AND uid = ?", (story_id, uid)
            )
        score = conn.execute(
            "SELECT COALESCE(SUM(value), 0) FROM votes WHERE story_id = ?", (story_id,)
        ).fetchone()[0]
    return {"score": score, "voted": vote}


_COMMENT_COLS = (
    "c.id, c.parent_id, c.ts, c.author, c.text, c.edited_ts, c.deleted_ts,"
    " (SELECT COALESCE(SUM(v.value), 0) FROM comment_votes v"
    "   WHERE v.comment_id = c.id) AS score,"
    " (SELECT v.value FROM comment_votes v"
    "   WHERE v.comment_id = c.id AND v.uid = ?) AS voted"
)


def _comment_dict(row: sqlite3.Row) -> dict:
    comment = dict(row)
    comment["voted"] = int(comment["voted"] or 0)
    comment["edited"] = comment.pop("edited_ts") is not None
    comment["deleted"] = comment.pop("deleted_ts") is not None
    return comment


def list_comments(story_id: int, uid: str | None = None) -> list[dict] | None:
    """All comments of a story, oldest first; the client builds the tree from
    parent_id. Removed comments are still listed - a tombstone is what keeps
    the replies under it attached to something. None when the story doesn't
    exist."""
    with closing(connect()) as conn:
        if conn.execute("SELECT 1 FROM stories WHERE id = ?", (story_id,)).fetchone() is None:
            return None
        rows = conn.execute(
            f"SELECT {_COMMENT_COLS} FROM comments c WHERE c.story_id = ? ORDER BY c.id",
            (uid, story_id),
        ).fetchall()
    return [_comment_dict(r) for r in rows]


def add_comment(story_id: int, parent_id: int | None, author: str, text: str) -> dict | None:
    """None when the story doesn't exist; ValueError when parent_id is not a
    comment on that story (a cross-story parent would silently reparent the
    reply into a thread the client never showed)."""
    with closing(connect()) as conn, conn:
        if conn.execute("SELECT 1 FROM stories WHERE id = ?", (story_id,)).fetchone() is None:
            return None
        if parent_id is not None:
            parent = conn.execute(
                "SELECT story_id FROM comments WHERE id = ?", (parent_id,)
            ).fetchone()
            if parent is None or parent["story_id"] != story_id:
                raise ValueError("parent comment not on this story")
        cur = conn.execute(
            "INSERT INTO comments (story_id, parent_id, ts, author, text)"
            " VALUES (?, ?, ?, ?, ?)",
            (story_id, parent_id, _now(), author, text),
        )
        row = conn.execute(
            f"SELECT {_COMMENT_COLS} FROM comments c WHERE c.id = ?",
            (None, cur.lastrowid),
        ).fetchone()
    return _comment_dict(row)


# Authorship is the stored username, so an anonymized row (author '') can
# never be claimed: '' == '' would otherwise hand every erased post to anyone
# whose session name is empty. Removed rows are frozen - nothing to edit, and
# re-deleting is a no-op the caller should hear about as a refusal.
_MINE = " AND author = ? AND author <> '' AND deleted_ts IS NULL"


def edit_story(story_id: int, author: str, title: str, text: str) -> dict | None:
    """The updated story, or None when it isn't this author's to edit. One
    conditional UPDATE rather than a SELECT and then an UPDATE, so a delete
    landing in between cannot slip past the check."""
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE stories SET title = ?, text = ?, edited_ts = ?"
            " WHERE id = ?" + _MINE,
            (title, text, _now(), story_id, author),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            f"SELECT {_STORY_COLS} FROM stories s WHERE s.id = ?", (None, story_id)
        ).fetchone()
    return _story_dict(row)


def delete_story(story_id: int, author: str) -> bool:
    """Remove this author's story. A story nobody replied to goes entirely
    (votes, problems and all, by cascade); one with comments keeps an empty
    row, because the thread hanging off it belongs to other people and a
    cascade would take their words with it."""
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT 1 FROM stories WHERE id = ?" + _MINE, (story_id, author)
        ).fetchone()
        if row is None:
            return False
        replied = conn.execute(
            "SELECT 1 FROM comments WHERE story_id = ? LIMIT 1", (story_id,)
        ).fetchone()
        if replied is None:
            conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
        else:
            conn.execute(
                "UPDATE stories SET deleted_ts = ?, author = '', title = '',"
                " text = '', from_station = '', to_station = '', departure = '',"
                " train = '', problem_other = '' WHERE id = ?",
                (_now(), story_id),
            )
            conn.execute("DELETE FROM story_problems WHERE story_id = ?", (story_id,))
    return True


def set_comment_vote(comment_id: int, uid: str, vote: int) -> dict | None:
    """Same contract as set_vote, for a comment. None when the comment is gone
    or removed - a tombstone is not something to vote on."""
    with closing(connect()) as conn, conn:
        live = conn.execute(
            "SELECT 1 FROM comments WHERE id = ? AND deleted_ts IS NULL", (comment_id,)
        ).fetchone()
        if live is None:
            return None
        if vote:
            conn.execute(
                "INSERT OR REPLACE INTO comment_votes (comment_id, uid, value)"
                " VALUES (?, ?, ?)",
                (comment_id, uid, vote),
            )
        else:
            conn.execute(
                "DELETE FROM comment_votes WHERE comment_id = ? AND uid = ?",
                (comment_id, uid),
            )
        score = conn.execute(
            "SELECT COALESCE(SUM(value), 0) FROM comment_votes WHERE comment_id = ?",
            (comment_id,),
        ).fetchone()[0]
    return {"score": score, "voted": vote}


def edit_comment(comment_id: int, author: str, text: str) -> dict | None:
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE comments SET text = ?, edited_ts = ? WHERE id = ?" + _MINE,
            (text, _now(), comment_id, author),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            f"SELECT {_COMMENT_COLS} FROM comments c WHERE c.id = ?", (None, comment_id)
        ).fetchone()
    return _comment_dict(row)


def delete_comment(comment_id: int, author: str) -> bool:
    """Same rule as delete_story: gone entirely when it is a leaf, an empty
    tombstone when replies hang off it."""
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT 1 FROM comments WHERE id = ?" + _MINE, (comment_id, author)
        ).fetchone()
        if row is None:
            return False
        replied = conn.execute(
            "SELECT 1 FROM comments WHERE parent_id = ? LIMIT 1", (comment_id,)
        ).fetchone()
        if replied is None:
            conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        else:
            conn.execute(
                "UPDATE comments SET deleted_ts = ?, author = '', text = ''"
                " WHERE id = ?",
                (_now(), comment_id),
            )
            conn.execute(
                "DELETE FROM comment_votes WHERE comment_id = ?", (comment_id,)
            )
    return True


def forget_account(uid: str, name: str | None) -> None:
    """The local half of an account erasure (auth.delete_account): its votes
    and taps go, its posts stay with the name removed."""
    with closing(connect()) as conn, conn:
        for table in _ACCOUNT_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE uid = ?", (uid,))
        if name:
            conn.execute("UPDATE stories SET author = '' WHERE author = ?", (name,))
            conn.execute("UPDATE comments SET author = '' WHERE author = ?", (name,))


_warned_no_topic = False


async def notify(title: str, body: str) -> None:
    """Push a new post to ntfy so moderation-worthy content is seen the moment
    it lands. Never raises and stays a no-op when NTFY_TOPIC is unset - same
    contract as feedback.notify."""
    global _warned_no_topic
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        if not _warned_no_topic:
            _warned_no_topic = True
            log.warning("NTFY_TOPIC is unset: stories are stored but not pushed")
        return
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    try:
        resp = await _ntfy.post(
            f"{base}/{topic}",
            content=body.encode("utf-8"),
            headers={"Title": title, "Tags": "speech_balloon"},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("ntfy push failed: %s", exc)


async def close() -> None:
    await _ntfy.aclose()
