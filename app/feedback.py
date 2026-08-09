"""Visitor feedback: a thumbs vote plus optional free text, in its own SQLite file.

The delays DuckDB is opened read-only and the file is swapped out from under the
process every night, so it cannot take writes - this is the one place the app owns
durable state of its own.

Nothing identifying is stored: no IP, no session id, no link to a search. The
per-IP rate limit it needs to survive contact with the internet lives in memory.
"""

import logging
import os
import sqlite3
import time
from collections import OrderedDict, deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# own subdirectory: the pipeline rebuilds and prunes files directly under data/
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "feedback" / "feedback.db"

MAX_PER_HOUR = 5
WINDOW = 3600
# ceiling on tracked IPs so a spray of unique addresses can't grow this without bound
THROTTLE_MAX_IPS = 4096

_hits: OrderedDict[str, deque[float]] = OrderedDict()

_ntfy = httpx.AsyncClient(timeout=5)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
  id      INTEGER PRIMARY KEY,
  sid     TEXT NOT NULL UNIQUE,
  ts      TEXT NOT NULL,
  vote    TEXT NOT NULL,
  text    TEXT NOT NULL DEFAULT '',
  lang    TEXT NOT NULL,
  context TEXT NOT NULL
)
"""


def throttled(ip: str) -> bool:
    """True when this IP is over its hourly allowance; otherwise records the attempt."""
    now = time.monotonic()
    hits = _hits.get(ip)
    if hits is None:
        hits = _hits[ip] = deque()
    _hits.move_to_end(ip)
    while hits and now - hits[0] > WINDOW:
        hits.popleft()
    if len(hits) >= MAX_PER_HOUR:
        return True
    hits.append(now)
    while len(_hits) > THROTTLE_MAX_IPS:
        _hits.popitem(last=False)
    return False


def save(sid: str, vote: str, text: str, lang: str, context: str) -> None:
    """Blocking - run it off the event loop.

    One row per prompt: the vote request creates it, the optional text arrives in a
    second request carrying the same sid. Empty text never overwrites text already
    stored, so a retried vote request cannot wipe a comment.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn, conn:
        conn.execute(_SCHEMA)
        conn.execute(
            "INSERT INTO feedback (sid, ts, vote, text, lang, context)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(sid) DO UPDATE SET"
            " text = CASE WHEN excluded.text != '' THEN excluded.text ELSE feedback.text END",
            (
                sid,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                vote,
                text,
                lang,
                context,
            ),
        )


async def notify(vote: str, text: str, lang: str, context: str) -> None:
    """Push a written comment to ntfy. Never raises - a submission must not fail
    because the notifier is unreachable, and stays a no-op when NTFY_TOPIC is unset."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    try:
        await _ntfy.post(
            f"{base}/{topic}",
            content=text.encode("utf-8"),
            headers={
                "Title": f"DelayBahn feedback ({vote}, {lang}, {context})",
                "Tags": "+1" if vote == "up" else "-1",
            },
        )
    except httpx.HTTPError as exc:
        log.warning("ntfy push failed: %s", exc)


async def close() -> None:
    await _ntfy.aclose()
