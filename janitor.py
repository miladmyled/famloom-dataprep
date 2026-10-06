import argparse
import csv
import json
import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

# Load environment configuration
load_dotenv(override=True)

# Configure structured enterprise logging for Kubernetes log aggregators
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("JanitorWorker")

from src.db.janitor import DatabaseJanitor

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
PRODUCTION_DB_NAMES = {"x3db"}


def _target() -> str:
    return f"{os.getenv('DB_HOST')} / {os.getenv('DB_NAME')}"


def _write_plan(rows, stamp: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"janitor_plan_{stamp}.csv"
    fields = ["id", "url", "city", "source", "title", "date", "reason", "decision", "is_canceled",
              "family_score", "adult_score", "provider", "linked_activities"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_backup(janitor: DatabaseJanitor, rows, stamp: str) -> Path:
    """Rows about to be deleted plus their tags, so they can be restored by hand if needed."""
    REPORTS_DIR.mkdir(exist_ok=True)
    ids = [r["id"] for r in rows]
    with janitor.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM city_events WHERE id = ANY(%(ids)s);", {"ids": ids})
            events = [dict(r) for r in cursor.fetchall()]
            cursor.execute("SELECT event_id, question_value_id FROM event_interest_tags WHERE event_id = ANY(%(ids)s);", {"ids": ids})
            tags = [dict(r) for r in cursor.fetchall()]
    path = REPORTS_DIR / f"janitor_backup_{stamp}.json"
    path.write_text(json.dumps({"city_events": events, "event_interest_tags": tags}, default=str, indent=2), encoding="utf-8")
    return path


def _summarize(rows) -> None:
    by_reason, by_city_source = {}, {}
    for r in rows:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
        key = f"{r['city']} | {r['source']}"
        by_city_source[key] = by_city_source.get(key, 0) + 1
    logger.info(f"[PLAN] {len(rows)} event(s) to remove; by reason: {by_reason}")
    for key, count in sorted(by_city_source.items()):
        logger.info(f"[PLAN]   {key}: {count}")
    linked = sum(int(r.get("linked_activities") or 0) for r in rows)
    if linked:
        logger.info(f"[PLAN] {linked} Activity link(s) will be cleared (source_city_event_id -> NULL); the Activities stay.")


def run_janitor(argv=None) -> int:
    """
    Entrypoint for the nightly PostgreSQL Event Janitor CronJob.
    Without flags (CronJob): purge expired events, remove rejected/canceled events when
    JANITOR_REMOVE_CLASSIFIED=true, prune old classification rows.
    --dry-run: report what classified removal would delete (CSV in reports/), delete nothing.
    --backup: write the rows to be removed (and their tags) to reports/ before deleting.
    """
    parser = argparse.ArgumentParser(description="FamLoom city events janitor")
    parser.add_argument("--dry-run", action="store_true", help="report classified removals only; delete nothing")
    parser.add_argument("--backup", action="store_true", help="back up rows to reports/ before classified removal")
    args = parser.parse_args(argv if argv is not None else [])

    logger.info("==================================================")
    logger.info("[START] Famloom Event Janitor (Daily Cleanup Run)")
    logger.info("==================================================")

    manual = args.dry_run or args.backup
    if manual:
        logger.info(f"[TARGET] {_target()}")
        if os.getenv("DB_NAME") in PRODUCTION_DB_NAMES:
            logger.error("[REFUSED] Manual janitor runs are for the dev database only.")
            return 1

    janitor = None
    try:
        janitor = DatabaseJanitor()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        if args.dry_run:
            rows = janitor.plan_classified_removal()
            if rows is not None:
                _summarize(rows)
                logger.info(f"[PLAN] Written to {_write_plan(rows, stamp)} (nothing deleted).")
            return 0

        purged_count = janitor.purge_expired_events()
        classified_count = 0
        if janitor.remove_classified:
            if args.backup:
                rows = janitor.plan_classified_removal() or []
                if rows:
                    logger.info(f"[BACKUP] {len(rows)} row(s) backed up to {_write_backup(janitor, rows, stamp)}")
            classified_count = janitor.purge_classified_events()
        else:
            logger.info("[JANITOR] Classified removal disabled (JANITOR_REMOVE_CLASSIFIED=false).")
        pruned = janitor.prune_classifications()
        logger.info(
            f"[SUCCESS] Janitor finished. Expired purged: {purged_count}; rejected/canceled removed: "
            f"{classified_count}; classification rows pruned: {pruned}"
        )
        return 0

    except Exception as err:
        logger.error(f"[ERROR] Fatal error during Janitor execution: {err}", exc_info=True)
        return 1

    finally:
        if janitor:
            janitor.close()


if __name__ == "__main__":
    exit_code = run_janitor(sys.argv[1:])
    sys.exit(exit_code)
