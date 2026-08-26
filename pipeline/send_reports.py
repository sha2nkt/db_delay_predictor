"""Send post-journey delay-report emails.

Resolves each open order's legs against the delay table once the data covers a
day past the travel date (in practice the morning of D+2, so late revisions are
already in), freezes the actuals into the row - the delay table only keeps a
rolling ~30 days - renders the comparison email and sends it through the site's
SMTP relay (app/mailer.py, the same path as the login codes). Runs as its own
process on purpose: a fresh delays.init() sees the freshly swapped
delays.duckdb and starts with empty lookup caches, unlike the web app, whose
_date_cache would pin "not yet ingested" answers until restart.

Dry run by default: prints every decision and changes nothing; --send makes it
real. Designed for a daily systemd timer at 07:45 Europe/Berlin, after the
05:30 pipeline has rebuilt the data and restarted the app.
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import delays, mailer, report_email, reports  # noqa: E402

parser = argparse.ArgumentParser(description="Send post-journey delay-report emails")
parser.add_argument("--send", action="store_true",
                    help="write state and send emails (default: dry run)")
parser.add_argument("--sample", metavar="FILE",
                    help="render the fixture report email to FILE and exit")
parser.add_argument("--test-to", metavar="ADDR",
                    help="send the fixture report to ADDR (requires --send) and exit")
parser.add_argument("--limit", type=int, default=100,
                    help="max reports per run (Brevo free tier: 300/day, shared with login codes)")
parser.add_argument("--base-url",
                    default=os.environ.get("REPORT_BASE_URL", "https://delaybahn.com"),
                    help="absolute URL prefix for links in the emails")
parser.add_argument("--today", metavar="YYYY-MM-DD",
                    help="override today (Berlin) to test the timeout path")
args = parser.parse_args()

SEND_RETRIES = 2
RETRY_SLEEP = (5, 25)


def alert(title: str, message: str) -> None:
    """ntfy push, fire-and-forget like app.feedback.notify; no-op without NTFY_TOPIC."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    try:
        httpx.post(f"{base}/{topic}", content=message.encode("utf-8"),
                   headers={"Title": title}, timeout=5)
    except httpx.HTTPError as exc:  # alerting must never kill the run
        print(f"ntfy push failed: {exc}", file=sys.stderr)


def resolve(sub: dict, parquet_max: str) -> tuple[dict, int, int]:
    """Actual delays for every tracked leg of one order.

    Returns (actuals, resolved count, tracked count); actuals holds one entry per
    tracked leg, keyed by the leg's index in the snapshot, None where the data has
    no matching observation."""
    snapshot = json.loads(sub["snapshot"])
    tracked = reports.tracked_leg_indices(snapshot.get("journey") or {})
    legs_out: dict = {}
    for i, leg in tracked:
        train, eva, arrival = reports.leg_lookup_key(leg)
        legs_out[str(i)] = delays.leg_delay_on_date(train, eva, arrival)
    actuals = {
        "resolvedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parquetMaxDay": parquet_max,
        "legs": legs_out,
    }
    return actuals, sum(1 for v in legs_out.values() if v is not None), len(tracked)


def send_with_retry(sub: dict, subject: str, html: str, text: str) -> bool:
    """Send one report, retrying a refused or unreachable relay within the run."""
    headers = {
        "List-Unsubscribe": f"<{args.base_url}/r/unsubscribe?token={sub['unsub_token']}>,"
        " <mailto:kontakt@delaybahn.com?subject=unsubscribe>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    for attempt in range(SEND_RETRIES + 1):
        if mailer.send_report(sub["email"], sub.get("name") or "", subject, text, html, headers):
            return True
        if attempt < SEND_RETRIES:
            time.sleep(RETRY_SLEEP[attempt])
    return False


def main() -> int:
    if args.sample:
        subject, html, _text = report_email.render_report(report_email.fixture_row(),
                                                          args.base_url)
        Path(args.sample).write_text(html, encoding="utf-8")
        print(f"subject: {subject}\nwrote {args.sample}")
        return 0
    if args.send and not mailer.configured():
        # mailer treats "no credentials" as sent, which is right for a dev
        # box's login codes and wrong for a job that would then mark every
        # report delivered
        print("SMTP_USER/SMTP_PASS unset: nothing can be sent", file=sys.stderr)
        return 2
    if args.test_to:
        if not args.send:
            print("--test-to needs --send", file=sys.stderr)
            return 2
        row = report_email.fixture_row()
        subject, html, text = report_email.render_report(row, args.base_url)
        ok = send_with_retry({"email": args.test_to, "name": "",
                              "unsub_token": row["unsub_token"]}, subject, html, text)
        print(f"test send to {args.test_to}: {'ok' if ok else 'failed'}")
        return 0 if ok else 1

    scrubbed = reports.scrub_old(apply=args.send)
    delays.init()
    parquet_max = delays.coverage()[1]
    if parquet_max is None:
        alert("delaybahn-reports", "no delay data available; aborting")
        print("no delay data available", file=sys.stderr)
        return 1
    today = args.today or datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    timeout_cutoff = (date.fromisoformat(today)
                      - timedelta(days=reports.RESOLVE_TIMEOUT_DAYS)).isoformat()
    rows = reports.due_rows(parquet_max.isoformat(), today, args.limit)
    print(f"{today}: data through {parquet_max}, {len(rows)} due, {scrubbed} settled rows scrubbed"
          + ("" if args.send else " (dry run)"))

    sent = waiting = failed = unresolved_sent = 0
    for sub in rows:
        try:
            actuals, resolved, tracked = resolve(sub, parquet_max.isoformat())
        except reports.SnapshotError as exc:
            reports.record_failure(sub["id"], f"bad snapshot: {exc}", give_up=True)
            print(f"  #{sub['id']}: bad snapshot ({exc})", file=sys.stderr)
            failed += 1
            continue
        label = f"#{sub['id']} travel {sub['travel_date']}, {resolved}/{tracked} legs resolved"
        if resolved == 0 and sub["travel_date"] >= timeout_cutoff:
            # nothing found yet and the timeout hasn't passed: late data may still come
            print(f"  {label}: waiting for late data")
            waiting += 1
            continue
        subject, html, text = report_email.render_report(dict(sub) | {"actuals": actuals},
                                                         args.base_url)
        if not args.send:
            print(f"  {label}: would send {subject!r}")
            sent += 1
            continue
        if send_with_retry(sub, subject, html, text):
            reports.mark_sent(sub["id"], actuals)
            print(f"  {label}: sent")
            sent += 1
            unresolved_sent += resolved == 0
        else:
            err = mailer.status().get("lastError") or "send failed"
            give_up = sub["attempts"] + 1 >= 3
            reports.record_failure(sub["id"], err, give_up)
            print(f"  {label}: send failed ({err})", file=sys.stderr)
            failed += 1
            if give_up:
                alert("delaybahn-reports", f"giving up on report #{sub['id']}: {err}")
    if unresolved_sent:
        alert("delaybahn-reports", f"{unresolved_sent} report(s) went out without a single"
              " resolved leg - check the pipeline or the leg keying")
    print(f"done: {sent} {'sent' if args.send else 'would send'},"
          f" {waiting} waiting, {failed} failed")
    return 0


sys.exit(main())
