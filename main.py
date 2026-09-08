import sys
import time
import logging
import threading
import concurrent.futures
from typing import List, Optional, Tuple
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
from src.etl.meetup_public import MeetupExtractor
from src.etl.transformer import clean_and_validate_event
from src.etl.kafka_producer import EventKafkaProducer
from src.db.events import get_active_interests
from src.etl.base import BaseEventScraper


def _process_scraper_task(
    scraper_name: str,
    scraper: BaseEventScraper,
    producer: EventKafkaProducer,
    active_interests: dict,
    metrics: dict,
    lock: threading.Lock,
) -> None:
    """
    Worker task executing extraction, normalization, transformation, and Kafka publishing
    for a specific scraper instance within a thread pool.
    """
    logger.info(f"⚡ [WORKER START] Running {scraper_name} for '{scraper.city}'...")
    try:
        # Extract raw events from provider
        raw_events = scraper.fetch_raw_events()
        with lock:
            metrics["raw_events_scraped"] += len(raw_events)

        # Normalize raw payloads to standard event contract
        normalized_events = scraper.normalize_data(raw_events)

        dispatched = 0
        for raw_dict in normalized_events:
            valid_event = clean_and_validate_event(raw_dict, interest_tags=active_interests)

            if valid_event is not None:
                with lock:
                    metrics["valid_events"] += 1
                success = producer.publish_event(valid_event)
                if success:
                    with lock:
                        metrics["queued_to_kafka"] += 1
                    dispatched += 1
                else:
                    with lock:
                        metrics["dropped_events"] += 1
            else:
                with lock:
                    metrics["dropped_events"] += 1

        with lock:
            metrics["cities_processed"] += 1

        # Drain batch buffer incrementally per worker task if events were queued
        if dispatched > 0:
            logger.info(f"⚡ [{scraper_name}] Dispatched {dispatched} events for '{scraper.city}'. Flushing buffer...")
            producer.flush(timeout=5.0, max_attempts=1)

        logger.info(f"✅ [WORKER FINISH] {scraper_name} completed successfully for '{scraper.city}'.")

    except Exception as exc:
        logger.error(f"❌ [{scraper_name}] Unexpected error during extraction for '{scraper.city}': {exc}", exc_info=True)


def run_etl_pipeline() -> int:
    """
    Main ETL Orchestration routine executed by Kubernetes CronJob.
    1. Extracts active target cities and active interests from Azure PostgreSQL.
    2. Initializes Confluent Kafka Producer for raw-events-ingestion topic.
    3. Runs event extractors concurrently (Eventbrite per city and Meetup for regional pool).
    4. Validates and filters events against strict business rules (future events only, timezone-aware, 14-day window).
    5. Streams valid and tombstone event records to Kafka with idempotency keys.
    6. Flushes Kafka producer buffer incrementally and guarantees final flush in finally block.
    """
    start_time = time.time()
    logger.info("==================================================")
    logger.info("🚀 Starting Famloom Event ETL Worker (CronJob Run)")
    logger.info("==================================================")

    # 1. Fetch active target cities and active interests from database
    logger.info("[STEP 1/4] Fetching active cities and interests from Azure PostgreSQL...")
    try:
        active_cities: List[str] = get_active_cities()
    except Exception as db_err:
        logger.error(f"❌ Fatal database error fetching active cities: {db_err}")
        return 1

    if not active_cities:
        logger.warning("⚠️ No active cities returned from database. Terminating job successfully.")
        return 0

    logger.info(f"📍 Found {len(active_cities)} active target cities: {active_cities}")

    # Cache active interest tags in memory during scraper run
    active_interests = get_active_interests()
    logger.info(f"🏷️ Cached {len(active_interests)} active interest tags for enrichment.")

    # 2. Initialize Kafka Producer
    logger.info("[STEP 2/4] Initializing Confluent Kafka Producer...")
    producer: Optional[EventKafkaProducer] = None
    try:
        producer = EventKafkaProducer()
    except Exception as k_err:
        logger.error(f"❌ Fatal Kafka initialization error: {k_err}")
        return 1

    # 3. Schedule and run extractors concurrently
    logger.info("[STEP 3/4] Scraping, transforming, and producing events concurrently...")
    metrics = {
        "cities_processed": 0,
        "raw_events_scraped": 0,
        "valid_events": 0,
        "queued_to_kafka": 0,
        "dropped_events": 0,
    }
    metrics_lock = threading.Lock()
    unflushed = 0

    # Build list of scraper tasks: Eventbrite for each active city, Meetup for regional activities pool
    scraper_tasks: List[Tuple[str, BaseEventScraper]] = []
    for city in active_cities:
        scraper_tasks.append((f"EventbriteScraper[{city}]", EventbriteScraper(city=city)))

    # Initial Meetup scraper targeting Coquitlam regional pool
    scraper_tasks.append(("MeetupExtractor[Coquitlam]", MeetupExtractor(city="Coquitlam, BC")))

    try:
        max_workers = min(8, max(len(scraper_tasks), 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    _process_scraper_task,
                    name,
                    scraper,
                    producer,
                    active_interests,
                    metrics,
                    metrics_lock,
                )
                for name, scraper in scraper_tasks
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as task_err:
                    logger.error(f"❌ Unhandled worker failure: {task_err}", exc_info=True)

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
    logger.info(f"🏙️ Tasks Completed      : {metrics['cities_processed']}/{len(scraper_tasks)}")
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
