"""
Empty city_events (and, through ON DELETE CASCADE, event_interest_tags) on the DEV database so a
fresh load can be run. Dry run by default.

    python scripts/reset_dev_events.py                               # counts only
    python scripts/reset_dev_events.py --apply --confirm-db x3db_dev # backup, then delete

Uses DELETE, never TRUNCATE: activities.source_city_event_id references city_events, and a
TRUNCATE ... CASCADE would empty activities too. With DELETE, linked Activities stay and their
source_city_event_id becomes NULL. The classification cache and remembered sites are kept.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

import _common

from src.config.database import get_db_pool

DEV_DB = "x3db_dev"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-db", help=f"must be {DEV_DB} together with --apply")
    args = parser.parse_args()
    _common.require_dev_db()
    import os

    if os.getenv("DB_NAME") != DEV_DB:
        sys.exit(f"[REFUSED] only {DEV_DB} can be reset")
    pool = get_db_pool()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM city_events")
                events = cur.fetchone()["n"]
                cur.execute("SELECT count(*) AS n FROM event_interest_tags")
                tags = cur.fetchone()["n"]
                cur.execute("SELECT count(*) AS n FROM activities WHERE source_city_event_id IS NOT NULL")
                linked = cur.fetchone()["n"]
        print(f"city_events={events} event_interest_tags={tags} activities linked to an event={linked} (they stay; link set to NULL)")
        if not args.apply:
            print("Dry run: nothing deleted. Add --apply --confirm-db x3db_dev to reset.")
            return 0
        if args.confirm_db != DEV_DB:
            sys.exit(f"[REFUSED] --confirm-db {DEV_DB} is required with --apply")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = _common.reports_dir() / f"reset_backup_{stamp}.json"
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM city_events")
                rows = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT event_id, question_value_id FROM event_interest_tags")
                tag_rows = [dict(r) for r in cur.fetchall()]
            backup.write_text(json.dumps({"city_events": rows, "event_interest_tags": tag_rows}, default=str), encoding="utf-8")
            print(f"Backup: {backup} ({len(rows)} events, {len(tag_rows)} tags)")
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM event_interest_tags")
                    deleted_tags = cur.rowcount
                    cur.execute("DELETE FROM city_events")
                    deleted_events = cur.rowcount
        print(f"Deleted {deleted_events} events and {deleted_tags} tags from {DEV_DB}.")
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
