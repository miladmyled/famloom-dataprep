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

from src.consumer.event_consumer import EventKafkaConsumer


def main() -> int:
    """
    Main entrypoint for the long-running Kafka to PostgreSQL Event Consumer.
    Deploys as a Kubernetes Deployment background worker.
    """
    logger.info("==================================================")
    logger.info("🚀 Starting Famloom Event Kafka-to-PostgreSQL Consumer")
    logger.info("==================================================")

    consumer = None

    def handle_shutdown_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info(f"⚠️ Received shutdown signal: {sig_name} ({signum}). Initiating graceful termination...")
        if consumer:
            consumer.running = False

    # Register OS signal handlers for graceful Kubernetes pod scaling and termination
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)

    try:
        consumer = EventKafkaConsumer()
        poll_timeout = float(os.getenv("KAFKA_POLL_TIMEOUT_SECONDS", "1.0"))
        consumer.run(poll_timeout=poll_timeout)
        return 0

    except Exception as fatal_err:
        logger.error(f"❌ Fatal error in consumer worker: {fatal_err}", exc_info=True)
        return 1

    finally:
        if consumer:
            consumer.close()
        logger.info("👋 Consumer process shutdown complete.")


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
