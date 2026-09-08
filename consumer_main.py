import os
import sys
import signal
import logging
from dotenv import load_dotenv

# Load environment configuration
load_dotenv(override=True)

# Configure structured enterprise logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("KafkaConsumerWorker")

# Legacy consumer imported EventKafkaConsumer which has been removed
# from src.consumer.event_consumer import EventKafkaConsumer


def main() -> int:
    """
    Legacy consumer entrypoint. Deprecated and scheduled for decommissioning.
    """
    logger.info("==================================================")
    logger.info("⚠️ Legacy Event Consumer is deprecated and inactive.")
    logger.info("==================================================")
    return 0


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
