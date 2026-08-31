import sys
import logging
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


def run_janitor() -> int:
    """
    Main entrypoint for the PostgreSQL Event Janitor CronJob.
    Purges past single-day and multi-day events from city_events and logs telemetry.
    """
    logger.info("==================================================")
    logger.info("[START] Famloom Event Janitor (Daily Cleanup Run)")
    logger.info("==================================================")

    janitor = None
    try:
        janitor = DatabaseJanitor()
        purged_count = janitor.purge_expired_events()
        logger.info(f"[SUCCESS] Janitor execution finished successfully. Total events purged: {purged_count}")
        return 0

    except Exception as err:
        logger.error(f"[ERROR] Fatal error during Janitor execution: {err}", exc_info=True)
        return 1

    finally:
        if janitor:
            janitor.close()


if __name__ == "__main__":
    exit_code = run_janitor()
    sys.exit(exit_code)
