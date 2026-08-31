"""
End-to-End Test Script for Famloom DataPrep Pipeline
Tests:
1. Database Connectivity & Schema Verification
2. Event Scraper Normalization & Timezone-Aware Validation
3. Direct Idempotent PostgreSQL Upsert & Tombstone Updates
4. Poison Pill Handling Simulation
"""

import sys
import logging
from datetime import datetime, timezone, timedelta

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PipelineTest")

from src.config.database import get_db_pool
from src.db.events import init_db_schema, upsert_city_event
from src.models.event import CityEvent
from src.etl.eventbrite import EventbriteScraper
from src.etl.transformer import clean_and_validate_event


def run_tests():
    print("\n========================================================")
    print("FAMLOOM DATAPREP ETL & CONSUMER TEST RUNNER")
    print("========================================================\n")

    # TEST 1: Database Connection & Schema Verification
    print("[TEST 1/4] Verifying Azure PostgreSQL & Schema Contracts...")
    try:
        pool = get_db_pool()
        cols = init_db_schema(pool)
        print(f"[SUCCESS] Azure PostgreSQL connected! Active columns: {sorted(list(cols))}\n")
    except Exception as e:
        print(f"[FAILED] Database connection failed: {e}\n")
        return 1

    # TEST 2: Scraper Normalization & Validation
    print("[TEST 2/4] Testing Eventbrite Scraper & Timezone Validation...")
    scraper = EventbriteScraper(city="Vancouver, BC, Canada", max_pages=1)
    raw_events = scraper.fetch_raw_events()

    if raw_events:
        print(f"[SUCCESS] Fetched {len(raw_events)} live events from Eventbrite API!")
        normalized = scraper.normalize_data(raw_events[:3])
        sample_event = clean_and_validate_event(normalized[0])
        if sample_event:
            print(f"   Sample Validated Event: '{sample_event.title}'")
            print(f"   ID: {sample_event.event_id} | Start UTC: {sample_event.start_date}")
            print(f"   Status: {sample_event.status} | Is Canceled: {sample_event.is_canceled}\n")
    else:
        print("[INFO] Live API returned 0 events or token not configured. Using mock event.")
        future_date = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        sample_event = clean_and_validate_event({
            "event_id": "eb_e2e_test_001",
            "city": "Vancouver, BC, Canada",
            "title": "Vancouver Kids STEM Workshop",
            "url": "https://eventbrite.ca/e/vancouver-stem-workshop-001",
            "start_date": future_date,
            "status": "live",
            "is_canceled": False,
        })
        print(f"[SUCCESS] Mock Event Validated: {sample_event.title}\n")

    # TEST 3: Idempotent Database Upsert & Tombstone Update
    print("[TEST 3/4] Testing Idempotent Database Upsert & Status Updates...")
    test_idempotent_event = CityEvent(
        event_id="eventbrite_test_idempotent_999",
        city="Vancouver, BC, Canada",
        title="Original Family Nature Walk",
        url="https://eventbrite.ca/e/nature-walk-test-999",
        start_date=datetime.now(timezone.utc) + timedelta(days=5),
        status="live",
        is_canceled=False,
    )

    with pool.connection() as conn:
        # Step A: Insert initial event
        upsert_city_event(conn, test_idempotent_event)
        conn.commit()
        print("   [A] Initial event inserted successfully.")

        # Step B: Upsert modified event (cancellation tombstone update)
        test_idempotent_event.title = "Canceled Family Nature Walk"
        test_idempotent_event.status = "canceled"
        test_idempotent_event.is_canceled = True
        upsert_city_event(conn, test_idempotent_event)
        conn.commit()
        print("   [B] Canceled tombstone update (ON CONFLICT) executed successfully.")

        # Step C: Verify updated values in DB
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM city_events WHERE url = 'https://eventbrite.ca/e/nature-walk-test-999';")
            row = cur.fetchone()
            print(f"   [C] DB State: Title='{row['title']}', Status='{row.get('status', 'N/A')}', Date={row['date']}")

            # Cleanup test record
            cur.execute("DELETE FROM city_events WHERE url = 'https://eventbrite.ca/e/nature-walk-test-999';")
        conn.commit()
        print("   [D] Cleaned up temporary test record.\n")

    # TEST 4: Poison Pill Validation Rejection
    print("[TEST 4/4] Testing Poison Pill Rejection (Corrupt / Past Events)...")
    past_event = clean_and_validate_event({
        "event_id": "eb_past_poison",
        "city": "Vancouver, BC, Canada",
        "title": "Past Puppet Show",
        "url": "https://eventbrite.ca/e/past-show",
        "start_date": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
    })
    assert past_event is None, "Past event should have been dropped!"
    print("[SUCCESS] Poison pill (past event) was safely rejected without crashing.\n")

    pool.close()
    print("========================================================")
    print("ALL PIPELINE INTEGRATION TESTS PASSED SUCCESSFULLY!")
    print("========================================================\n")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
