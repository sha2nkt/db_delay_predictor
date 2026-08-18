"""Visitor feedback: a thumbs vote plus optional free text and screenshot, in its
own SQLite file.

The delays DuckDB is opened read-only and the file is swapped out from under the
process every night, so it cannot take writes - this is the one place the app owns
durable state of its own.

Nothing identifying is stored: no IP, no session id, no link to a search. The
per-IP rate limit it needs to survive contact with the internet lives in memory.
"""

import base64
import binascii
import logging
import os
import sqlite3
import time
from collections import OrderedDict, deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
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
  context TEXT NOT NULL,
  shot    BLOB
)
"""

SHOT_MAX_BYTES = 512 * 1024

# rolling 24 h ceiling on stored screenshot bytes: the per-IP throttle cannot
# stop a distributed bot, so this bounds what any flood can put on disk
SHOT_BUDGET_BYTES = 200 * 1024 * 1024
# while the budget stays tripped, repeat the warning this often at most
SHOT_BUDGET_WARN_EVERY = 6 * 3600

_budget_last_warn = float("-inf")

# magic bytes per declared mime, so the data-URL label can't smuggle another format
_SHOT_MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
_SHOT_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def decode_shot(data_url: str) -> tuple[bytes, str] | None:
    """base64 image data URL -> (bytes, file extension); None for the empty string.

    Raises ValueError on anything else: wrong scheme, unsupported mime, broken
    base64, oversized payload, or content that isn't the image type it claims.
    """
    if not data_url:
        return None
    header, sep, payload = data_url.partition(",")
    if not sep or not header.startswith("data:") or not header.endswith(";base64"):
        raise ValueError("not a base64 data URL")
    mime = header[len("data:") : -len(";base64")]
    if mime not in _SHOT_EXT:
        raise ValueError(f"unsupported image type {mime!r}")
    try:
        raw = base64.b64decode(payload, validate=True)
    except binascii.Error as exc:
        raise ValueError("broken base64 payload") from exc
    if len(raw) > SHOT_MAX_BYTES:
        raise ValueError("image too large")
    if mime == "image/webp":
        genuine = raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    else:
        genuine = raw.startswith(_SHOT_MAGIC[mime])
    if not genuine:
        raise ValueError("payload does not match its declared image type")
    return raw, _SHOT_EXT[mime]


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


def save(
    sid: str, vote: str, text: str, lang: str, context: str, shot: bytes | None
) -> bool:
    """Blocking - run it off the event loop.

    One row per prompt: the vote request creates it, the optional text and
    screenshot arrive in a second request carrying the same sid. Empty text and a
    missing screenshot never overwrite what is already stored, so a retried vote
    request cannot wipe a comment or its image.

    Returns True when the screenshot was dropped because the rolling 24 h
    screenshot budget is full; the vote and text are stored regardless.
    """
    dropped = False
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn, conn:
        conn.execute(_SCHEMA)
        try:
            conn.execute("ALTER TABLE feedback ADD COLUMN shot BLOB")
        except sqlite3.OperationalError:
            pass  # pre-existing db already migrated (or born with the column)
        if shot is not None:
            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(
                timespec="seconds"
            )
            used = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(shot)), 0) FROM feedback WHERE ts >= ?",
                (since,),
            ).fetchone()[0]
            if used + len(shot) > SHOT_BUDGET_BYTES:
                shot = None
                dropped = True
        conn.execute(
            "INSERT INTO feedback (sid, ts, vote, text, lang, context, shot)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(sid) DO UPDATE SET"
            " text = CASE WHEN excluded.text != '' THEN excluded.text ELSE feedback.text END,"
            " shot = COALESCE(excluded.shot, feedback.shot)",
            (
                sid,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                vote,
                text,
                lang,
                context,
                shot,
            ),
        )
    return dropped


def budget_warn_due() -> bool:
    """True at most once per SHOT_BUDGET_WARN_EVERY - called from the event loop
    only, so plain module state is race-free."""
    global _budget_last_warn
    now = time.monotonic()
    if now - _budget_last_warn < SHOT_BUDGET_WARN_EVERY:
        return False
    _budget_last_warn = now
    return True


_warned_no_topic = False


async def notify(
    vote: str, text: str, lang: str, context: str, shot: tuple[bytes, str] | None
) -> None:
    """Push a written comment - and its screenshot as a second, attachment-only
    message - to ntfy. Never raises - a submission must not fail because the
    notifier is unreachable, and stays a no-op when NTFY_TOPIC is unset.

    The screenshot goes in its own message because ntfy carries text alongside a
    binary body only via headers, which cannot hold arbitrary UTF-8 comments.

    Both no-op paths say so in the log: a missing topic or a topic ntfy rejects
    used to lose every comment silently, which is exactly the failure nobody
    notices - the comment is still in SQLite, but the phone never buzzes.
    """
    global _warned_no_topic
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        if not _warned_no_topic:
            _warned_no_topic = True
            log.warning(
                "NTFY_TOPIC is unset: feedback comments are stored but not pushed"
            )
        return
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": f"DelayBahn feedback ({vote}, {lang}, {context})",
        "Tags": "+1" if vote == "up" else "-1",
    }
    try:
        if text:
            resp = await _ntfy.post(
                f"{base}/{topic}", content=text.encode("utf-8"), headers=headers
            )
            # a rejected topic answers 4xx without raising, so ask explicitly
            resp.raise_for_status()
        if shot:
            raw, ext = shot
            resp = await _ntfy.post(
                f"{base}/{topic}",
                content=raw,
                # Filename makes ntfy treat the body as an attachment
                headers={**headers, "Filename": f"feedback.{ext}"},
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("ntfy push failed: %s", exc)


async def notify_budget() -> None:
    """Warn that the screenshot budget is full and images are being dropped.
    Same never-raises contract as notify; rate-limited via budget_warn_due."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    mb = SHOT_BUDGET_BYTES // (1024 * 1024)
    try:
        resp = await _ntfy.post(
            f"{base}/{topic}",
            content=(
                f"The {mb} MB/24h screenshot budget is full - feedback images are"
                " being dropped (text still stored). Possible upload flood."
            ).encode("utf-8"),
            headers={
                "Title": "DelayBahn screenshot budget reached",
                "Tags": "warning",
                "Priority": "high",
            },
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("ntfy budget warning failed: %s", exc)


async def close() -> None:
    await _ntfy.aclose()
