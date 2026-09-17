"""
Backfill utility to populate event_interest_tags for all existing records in city_events.
Usage:
    python scripts/backfill_interest_tags.py
"""
import os
import sys
import logging

# Ensure project root is in sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..")) if "__file__" in locals() else "/app"
sys.path.insert(0, project_root)

from src.config.database import get_db_pool
from src.db.events import get_active_interests
from src.etl.transformer import match_interest_tags

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_interest_tags")


def backfill_interest_tags():
    logger.info("🚀 Starting interest tags backfill for existing city_events...")
    pool = get_db_pool()

    try:
        # 1. Fetch active interests
        interests_map = get_active_interests(pool)
        if not interests_map:
            logger.error(
                "❌ No active interest tags loaded from database. "
                "Ensure 'dataprep_worker' has SELECT permissions on 'questions' and 'question_values'."
            )
            return

        logger.info(f"🏷️ Loaded {len(interests_map)} active interest tags.")

        # 2. Fetch all events from city_events
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'city_events';
                """)
                existing_cols = {row["column_name"] for row in cur.fetchall()}
                has_desc = "description" in existing_cols

                query = "SELECT id, title" + (", description" if has_desc else "") + " FROM city_events;"
                cur.execute(query)
                events = cur.fetchall()

            logger.info(f"📋 Found {len(events)} events in city_events to evaluate (has_description={has_desc}).")

            total_tagged_events = 0
            batch_records = []

            for row in events:
                event_id = row["id"]
                title = row.get("title", "") or ""
                description = row.get("description", "") or "" if has_desc else ""

                matched_tag_ids = match_interest_tags(
                    title=title,
                    description=description,
                    interest_mapping=interests_map,
                )

                if matched_tag_ids:
                    total_tagged_events += 1
                    for tag_id in matched_tag_ids:
                        batch_records.append((event_id, tag_id))

            logger.info(
                f"🎯 Matched {len(batch_records)} tag associations across {total_tagged_events} events."
            )

            # 3. Batch insert into event_interest_tags
            if batch_records:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO event_interest_tags (event_id, question_value_id)
                        VALUES (%s, %s)
                        ON CONFLICT (event_id, question_value_id) DO NOTHING;
                        """,
                        batch_records,
                    )
                conn.commit()
                logger.info("✅ Successfully committed interest tag associations to event_interest_tags!")
            else:
                logger.info("ℹ️ No tag associations to insert.")

    except Exception as e:
        logger.error(f"❌ Error during backfill: {e}", exc_info=True)
    finally:
        pool.close()
        logger.info("🏁 Backfill process completed.")


if __name__ == "__main__":
    backfill_interest_tags()
