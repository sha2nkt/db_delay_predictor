"""Move the SQLite-era accounts of the stories board into Firebase.

Run once, on the box that holds data/stories/stories.db, after the app has
started on the Firebase build (its schema migration sets the old users table
aside as users_legacy) and with FIREBASE_SA_FILE set:

    uv run python scripts/import_users_to_firebase.py [--dry-run]

Every confirmed account becomes a Firebase user with uid 'legacy-<id>' - the
uid the migration already wrote onto the account's votes and taps - carrying
its address (marked verified), its username as display name and as the
`handle` claim, plus the registry entry that keeps the name unique. Accounts
that never confirmed their address are skipped, as the 7-day purge would have
done. Idempotent: a uid Firebase already knows is left alone. Once nothing
failed, users_legacy is dropped (--keep leaves it).

Legacy accounts have no password. Their owners sign in with "forgot password"
on the same address, or with Google/Apple on it, which Firebase links to the
imported account.
"""

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, stories  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    parser.add_argument("--db", type=Path, default=stories.DB_PATH,
                        help="stories SQLite file (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be imported; write nothing anywhere")
    parser.add_argument("--keep", action="store_true",
                        help="leave users_legacy in place after a clean import")
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"no database at {args.db}; nothing to import")
        return 0
    with closing(sqlite3.connect(args.db)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        table = next((t for t in ("users_legacy", "users") if t in tables), None)
        if table is None:
            print("no users table left; nothing to import")
            return 0
        rows = conn.execute(
            f"SELECT id, name, email, verified_ts FROM {table} ORDER BY id"
        ).fetchall()

    fb_auth, _db = auth._firebase()
    registry = auth._registry()
    imported = existing = skipped = failed = 0
    for row in rows:
        uid = f"legacy-{row['id']}"
        if not row["verified_ts"] or not row["email"]:
            skipped += 1
            continue
        try:
            fb_auth.get_user(uid, app=auth._app)
            existing += 1
            continue
        except fb_auth.UserNotFoundError:
            pass
        print(f"{uid}: {row['name']} <{row['email']}>", end="")
        if args.dry_run:
            print(" (dry run)")
            imported += 1
            continue
        record = fb_auth.ImportUserRecord(
            uid=uid, email=row["email"], email_verified=True,
            display_name=row["name"], custom_claims={"handle": row["name"]},
        )
        result = fb_auth.import_users([record], app=auth._app)
        if result.failure_count:
            failed += 1
            print(f" FAILED: {result.errors[0].reason}")
            continue
        outcome = registry.claim(uid, row["name"])
        print(f" imported, registry {outcome}")
        if outcome == "taken":
            failed += 1  # somebody claimed the name on Firebase first; sort out by hand
        else:
            imported += 1

    print(f"imported {imported}, already in Firebase {existing}, "
          f"skipped (unconfirmed) {skipped}, failed {failed}")
    if failed or args.dry_run or args.keep or table != "users_legacy":
        return 1 if failed else 0
    with closing(sqlite3.connect(args.db)) as conn, conn:
        conn.execute("DROP TABLE users_legacy")
    print("dropped users_legacy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
