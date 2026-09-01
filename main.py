import sys
import time
import logging
from typing import List, Optional
from dotenv import load_dotenv

# Load environment configuration
load_dotenv(override=True)

# Configure structured enterprise logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ETLOrchestrator")

from src.etl.extractor import get_active_cities
from src.etl.eventbrite import EventbriteScraper
from src.etl.transformer import clean_and_validate_event
from src.etl.kafka_producer import EventKafkaProducer


def run_etl_pipeline() -> int:
    """
    Main ETL Orchestration routine executed by Kubernetes CronJob.
    1. Extracts active target cities from Azure PostgreSQL.
    2. Initializes Confluent Kafka Producer for raw-events-ingestion topic.
    3. Scrapes external family-friendly events for each city via Eventbrite API.
    4. Validates and filters events against strict business rules (future events only, timezone-aware, 14-day window).
    5. Streams valid and tombstone event records to Kafka with idempotency keys.
    6. Flushes Kafka producer buffer incrementally and guarantees final flush in finally block.
    """
    start_time = time.time()
    logger.info("==================================================")
    logger.info("🚀 Starting Famloom Event ETL Worker (CronJob Run)")
    logger.info("==================================================")

    # 1. Fetch active target cities from database
    logger.info("[STEP 1/4] Fetching active cities from Azure PostgreSQL...")
    try:
        active_cities: List[str] = get_active_cities()
    except Exception as db_err:
        logger.error(f"❌ Fatal database error fetching active cities: {db_err}")
        return 1

    if not active_cities:
        logger.warning("⚠️ No active cities returned from database. Terminating job successfully.")
        return 0

    logger.info(f"📍 Found {len(active_cities)} active target cities: {active_cities}")

    # 2. Initialize Kafka Producer
    logger.info("[STEP 2/4] Initializing Confluent Kafka Producer...")
    producer: Optional[EventKafkaProducer] = None
    try:
        producer = EventKafkaProducer()
    except Exception as k_err:
        logger.error(f"❌ Fatal Kafka initialization error: {k_err}")
        return 1

    # 3. Process events city by city
    logger.info("[STEP 3/4] Scraping, transforming, and producing events per city...")
    metrics = {
        "cities_processed": 0,
        "raw_events_scraped": 0,
        "valid_events": 0,
        "queued_to_kafka": 0,
        "dropped_events": 0,
    }
    unflushed = 0

    try:
        for city in active_cities:
            logger.info(f"\n--- Processing City: '{city}' ---")
            scraper = EventbriteScraper(city=city)

            # Extract raw events from Eventbrite
            raw_events = scraper.fetch_raw_events()
            metrics["raw_events_scraped"] += len(raw_events)

            # Normalize raw payloads
            normalized_events = scraper.normalize_data(raw_events)

            # Transform, Validate, and Stream
            city_queued = 0
            for raw_dict in normalized_events:
                valid_event = clean_and_validate_event(raw_dict)

                if valid_event is not None:
                    metrics["valid_events"] += 1
                    success = producer.publish_event(valid_event)
                    if success:
                        metrics["queued_to_kafka"] += 1
                        city_queued += 1
                    else:
                        metrics["dropped_events"] += 1
                else:
                    metrics["dropped_events"] += 1

            metrics["cities_processed"] += 1

            # Drain batch buffer incrementally per city
            if city_queued > 0:
                logger.info(f"⚡ Dispatched {city_queued} events for '{city}'. Draining network batch...")
                producer.flush(timeout=5.0, max_attempts=1)

    except KeyboardInterrupt:
        logger.warning("⚠️ Interrupted by signal. Commencing graceful shutdown...")
    except Exception as pipeline_err:
        logger.error(f"❌ Unexpected pipeline error during execution: {pipeline_err}", exc_info=True)

    finally:
        # 4. Guarantee Kafka Producer buffer flush before process termination
        if producer is not None:
            logger.info("\n[STEP 4/4] Executing guaranteed final Kafka producer buffer flush...")
            try:
                unflushed = producer.flush(timeout=30.0, max_attempts=3)
            except Exception as flush_err:
                logger.error(f"❌ Error during final producer flush: {flush_err}")
                unflushed = -1

    delivery_stats = producer.get_delivery_metrics() if producer else {}
    elapsed = time.time() - start_time

    logger.info("==================================================")
    logger.info("📊 ETL PIPELINE EXECUTION SUMMARY")
    logger.info("==================================================")
    logger.info(f"⏱️ Total Execution Time : {elapsed:.2f} seconds")
    logger.info(f"🏙️ Cities Processed     : {metrics['cities_processed']}/{len(active_cities)}")
    logger.info(f"📥 Raw Events Scraped   : {metrics['raw_events_scraped']}")
    logger.info(f"✅ Validated Events     : {metrics['valid_events']}")
    logger.info(f"📤 Queued to Kafka      : {metrics['queued_to_kafka']}")
    logger.info(f"🎯 Broker Acknowledged  : {delivery_stats.get('delivered', 0)}")
    logger.info(f"🗑️ Dropped Events       : {metrics['dropped_events']}")
    logger.info(f"⚠️ Unflushed Buffer Msg : {unflushed}")
    logger.info("==================================================")

    if unflushed > 0:
        logger.error(f"❌ Worker completed with {unflushed} unsent Kafka messages.")
        return 1

    logger.info("🎉 ETL CronJob execution completed successfully!")
    return 0


if __name__ == "__main__":
    exit_code = run_etl_pipeline()
    sys.exit(exit_code)
