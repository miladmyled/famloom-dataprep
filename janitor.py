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

# Legacy DatabaseJanitor has been removed
# from src.db.janitor import DatabaseJanitor


def run_janitor() -> int:
    """
    Main entrypoint for the PostgreSQL Event Janitor CronJob.
    Deprecated and scheduled for decommissioning.
    """
    logger.info("==================================================")
    logger.info("⚠️ Legacy Event Janitor is deprecated and inactive.")
    logger.info("==================================================")
    return 0


if __name__ == "__main__":
    exit_code = run_janitor()
    sys.exit(exit_code)
