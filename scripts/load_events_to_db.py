"""
Direct ETL Loader: Scrapes Eventbrite events for all active cities in Azure PostgreSQL,
validates them with Pydantic & business rules, and performs idempotent upserts into city_events.
"""

import sys
import time
import logging
from typing import List
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("DirectDBLoader")

from src.config.database import get_db_pool
from src.etl.extractor import get_active_cities
from src.etl.eventbrite import EventbriteScraper
from src.etl.transformer import clean_and_validate_event
from src.db.events import init_db_schema, upsert_city_event


def load_events_for_active_cities() -> int:
    start_time = time.time()
    logger.info("==================================================")
    logger.info("🚀 Starting Live Event Ingestion into Azure PostgreSQL")
    logger.info("==================================================")

    # 1. Initialize DB Pool and verify schema
    pool = get_db_pool()
    try:
        init_db_schema(pool)
    except Exception as e:
        logger.error(f"❌ Error initializing DB schema: {e}")

    # 2. Fetch active cities from Azure PostgreSQL
    raw_cities = get_active_cities()
    # Filter valid geographical locations (skip test values like 'milad joon')
    active_cities = [c for c in raw_cities if "," in c or len(c.strip()) > 3 and c.strip().lower() != "milad joon"]

    logger.info(f"📍 Target Active Cities ({len(active_cities)}): {active_cities}")

    metrics = {
        "cities_processed": 0,
        "raw_events_scraped": 0,
        "valid_events": 0,
        "upserted_to_db": 0,
        "dropped_events": 0,
    }

    try:
        with pool.connection() as conn:
            for city in active_cities:
                logger.info(f"\n--------------------------------------------------")
                logger.info(f"🏙️  Processing City: '{city}'")
                logger.info(f"--------------------------------------------------")

                scraper = EventbriteScraper(city=city, max_pages=2)
                raw_events = scraper.fetch_raw_events()
                metrics["raw_events_scraped"] += len(raw_events)

                normalized = scraper.normalize_data(raw_events)
                logger.info(f"Normalized {len(normalized)} raw event payloads for '{city}'.")

                city_valid = 0
                for raw_dict in normalized:
                    event = clean_and_validate_event(raw_dict)

                    if event is not None:
                        metrics["valid_events"] += 1
                        city_valid += 1
                        try:
                            upsert_city_event(conn, event)
                            conn.commit()
                            metrics["upserted_to_db"] += 1
                        except Exception as upsert_err:
                            conn.rollback()
                            logger.error(f"❌ Failed to upsert event '{event.title}': {upsert_err}")
                            metrics["dropped_events"] += 1
                    else:
                        metrics["dropped_events"] += 1

                logger.info(f"✅ City '{city}': Successfully validated & upserted {city_valid} events.")
                metrics["cities_processed"] += 1

    except Exception as e:
        logger.error(f"❌ Pipeline error during database load: {e}", exc_info=True)
        return 1

    # 3. Query database summary
    logger.info("\n==================================================")
    logger.info("📊 DATABASE VERIFICATION: CURRENT CITY_EVENTS ROWS")
    logger.info("==================================================")

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM city_events;")
            total_rows = cur.fetchone()["total"]
            logger.info(f"📦 Total Rows in city_events Table: {total_rows}")

            cur.execute("""
                SELECT id, city, title, source, url, date, updated_at 
                FROM city_events 
                ORDER BY updated_at DESC 
                LIMIT 10;
            """)
            rows = cur.fetchall()
            logger.info(f"\n--- Latest 10 Upserted Events in PostgreSQL ---")
            for idx, r in enumerate(rows, 1):
                logger.info(
                    f"[{idx}] (ID: {r['id']}) {r['title']} | City: {r['city']} | Date: {r['date']}"
                )

    pool.close()

    elapsed = time.time() - start_time
    logger.info("\n==================================================")
    logger.info("🎉 INGESTION PIPELINE COMPLETED")
    logger.info("==================================================")
    logger.info(f"⏱️ Total Duration       : {elapsed:.2f}s")
    logger.info(f"🏙️ Cities Processed     : {metrics['cities_processed']}/{len(active_cities)}")
    logger.info(f"📥 Raw Scraped Events   : {metrics['raw_events_scraped']}")
    logger.info(f"✅ Validated Events     : {metrics['valid_events']}")
    logger.info(f"💾 Persisted to Postgres: {metrics['upserted_to_db']}")
    logger.info(f"🗑️ Dropped/Filtered     : {metrics['dropped_events']}")
    logger.info("==================================================")
    return 0


if __name__ == "__main__":
    sys.exit(load_events_for_active_cities())
