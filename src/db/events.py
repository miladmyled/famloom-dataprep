import logging
from typing import Any, Dict, List, Optional, Set
from psycopg_pool import ConnectionPool
from psycopg import Connection
from src.config.database import get_db_pool
from src.models.event import CityEvent

logger = logging.getLogger(__name__)

ALL_SCHEMA_COLUMNS: Set[str] = {
    "id",
    "event_id",
    "city",
    "title",
    "source",
    "url",
    "date",
    "start_date",
    "end_date",
    "description",
    "location_summary",
    "status",
    "is_canceled",
    "created_at",
    "updated_at",
}

# Cached set of available columns on city_events table (defaults to full schema)
_AVAILABLE_COLUMNS: Set[str] = set(ALL_SCHEMA_COLUMNS)


def get_active_interests(pool: Optional[ConnectionPool] = None) -> Dict[str, int]:
    """
    Fetches active interest taxonomy labels and their corresponding question_value IDs
    from Azure PostgreSQL.
    Maps lowercase labels to question_value_id integers.
    Catches DB errors gracefully and returns an empty mapping to prevent pipeline crashes.
    """
    created_local_pool = False
    active_pool = pool
    if active_pool is None:
        try:
            active_pool = get_db_pool()
            created_local_pool = True
        except Exception as pool_err:
            logger.warning(f"[DB] Failed to acquire connection pool for active interests: {pool_err}")
            return {}

    try:
        with active_pool.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT v.id AS question_value_id, LOWER(v.value_label) AS label
                    FROM questions q
                    JOIN question_values v ON v.question_id = q.id
                    WHERE q.code = 'interests' AND q.is_active = true AND v.is_active = true;
                """)
                records = cursor.fetchall()
                interests_map: Dict[str, int] = {}
                for row in records:
                    label = str(row["label"]).strip().lower()
                    if label:
                        interests_map[label] = int(row["question_value_id"])
                logger.info(f"[DB] Successfully loaded {len(interests_map)} active interest tags.")
                return interests_map

    except Exception as e:
        logger.warning(f"[DB] Unable to fetch active interests from database: {e}. Defaulting to empty mapping.")
        return {}

    finally:
        if created_local_pool and active_pool is not None:
            active_pool.close()


def init_db_schema(pool: ConnectionPool) -> Set[str]:
    """
    Ensures the city_events table in Azure PostgreSQL has the required schema contracts,
    including strict TIMESTAMPTZ datetime columns and unique conflict targets.
    Attempts DDL upgrades if permitted, and caches available table columns for dynamic upserts.
    """
    global _AVAILABLE_COLUMNS
    logger.info("[DB] Verifying city_events schema contracts and columns...")

    try:
        with pool.connection() as conn:
            with conn.cursor() as cursor:
                # 1. Attempt table & column creation / DDL migration (if table owner / DDL permitted)
                try:
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS city_events (
                            id SERIAL PRIMARY KEY,
                            event_id TEXT UNIQUE,
                            city TEXT NOT NULL,
                            title TEXT NOT NULL,
                            source TEXT DEFAULT 'Eventbrite',
                            url TEXT UNIQUE,
                            date TIMESTAMPTZ,
                            start_date TIMESTAMPTZ,
                            end_date TIMESTAMPTZ,
                            description TEXT,
                            location_summary TEXT,
                            status TEXT DEFAULT 'live',
                            is_canceled BOOLEAN DEFAULT FALSE,
                            created_at TIMESTAMPTZ DEFAULT NOW(),
                            updated_at TIMESTAMPTZ DEFAULT NOW()
                        );
                    """)

                    alter_statements = [
                        "ALTER TABLE city_events ADD COLUMN IF NOT EXISTS event_id TEXT UNIQUE;",
                        "ALTER TABLE city_events ADD COLUMN IF NOT EXISTS start_date TIMESTAMPTZ;",
                        "ALTER TABLE city_events ADD COLUMN IF NOT EXISTS end_date TIMESTAMPTZ;",
                        "ALTER TABLE city_events ADD COLUMN IF NOT EXISTS description TEXT;",
                        "ALTER TABLE city_events ADD COLUMN IF NOT EXISTS location_summary TEXT;",
                        "ALTER TABLE city_events ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'live';",
                        "ALTER TABLE city_events ADD COLUMN IF NOT EXISTS is_canceled BOOLEAN DEFAULT FALSE;",
                        "ALTER TABLE city_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();",
                        "ALTER TABLE city_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();",
                    ]
                    for stmt in alter_statements:
                        try:
                            cursor.execute(stmt)
                        except Exception:
                            pass
                    conn.commit()
                except Exception as ddl_err:
                    logger.info(f"[DB] DDL migration skipped (managed permissions): {ddl_err}")
                    conn.rollback()

                # 2. Inspect active columns in city_events
                cursor.execute("""
                    SELECT column_name, data_type 
                    FROM information_schema.columns 
                    WHERE table_name = 'city_events';
                """)
                detected_cols = {row["column_name"] for row in cursor.fetchall()}
                if detected_cols:
                    _AVAILABLE_COLUMNS = detected_cols
                logger.info(f"[DB] Active city_events columns detected: {sorted(list(_AVAILABLE_COLUMNS))}")

        return _AVAILABLE_COLUMNS

    except Exception as e:
        logger.error(f"[DB] Error during schema verification: {e}")
        raise


def get_available_columns() -> Set[str]:
    global _AVAILABLE_COLUMNS
    return _AVAILABLE_COLUMNS if _AVAILABLE_COLUMNS else ALL_SCHEMA_COLUMNS


def upsert_city_event(conn: Connection, event: CityEvent) -> Optional[int]:
    """
    Executes an atomic idempotent SQL upsert into the city_events table.
    Uses ON CONFLICT (event_id) DO UPDATE (or ON CONFLICT (url) fallback),
    updating status, start_date, end_date, and metadata to handle event changes
    and cancellations (tombstone records) gracefully.
    Captures the resulting city_events.id via RETURNING id and batch inserts
    matched associations into event_interest_tags (event_id, question_value_id)
    with ON CONFLICT (event_id, question_value_id) DO NOTHING.
    """
    cols = get_available_columns()

    # Determine conflict target based on available columns
    if "event_id" in cols:
        conflict_target = "event_id"
    else:
        conflict_target = "url"

    fields = []
    values_placeholders = []
    update_assignments = [
        "city = EXCLUDED.city",
        "title = EXCLUDED.title",
        "source = EXCLUDED.source",
        "date = EXCLUDED.date",
        "updated_at = NOW()",
    ]

    params: Dict[str, Any] = {
        "city": event.city,
        "title": event.title,
        "source": event.source,
        "url": str(event.url),
        "date": event.start_date,
    }

    if "id" in cols:
        fields.append("id")
        values_placeholders.append("(SELECT COALESCE(MAX(id), 0) + 1 FROM city_events)")

    fields.extend(["city", "title", "source", "url", "date", "updated_at"])
    values_placeholders.extend(["%(city)s", "%(title)s", "%(source)s", "%(url)s", "%(date)s", "NOW()"])

    if "event_id" in cols:
        fields.append("event_id")
        values_placeholders.append("%(event_id)s")
        params["event_id"] = event.event_id

    if "start_date" in cols:
        fields.append("start_date")
        values_placeholders.append("%(start_date)s")
        update_assignments.append("start_date = EXCLUDED.start_date")
        params["start_date"] = event.start_date

    if "end_date" in cols:
        fields.append("end_date")
        values_placeholders.append("%(end_date)s")
        update_assignments.append("end_date = EXCLUDED.end_date")
        params["end_date"] = event.end_date

    if "status" in cols:
        fields.append("status")
        values_placeholders.append("%(status)s")
        update_assignments.append("status = EXCLUDED.status")
        params["status"] = event.status

    if "is_canceled" in cols:
        fields.append("is_canceled")
        values_placeholders.append("%(is_canceled)s")
        update_assignments.append("is_canceled = EXCLUDED.is_canceled")
        params["is_canceled"] = event.is_canceled

    if "description" in cols:
        fields.append("description")
        values_placeholders.append("%(description)s")
        update_assignments.append("description = COALESCE(EXCLUDED.description, city_events.description)")
        params["description"] = event.description

    if "location_summary" in cols:
        fields.append("location_summary")
        values_placeholders.append("%(location_summary)s")
        update_assignments.append("location_summary = COALESCE(EXCLUDED.location_summary, city_events.location_summary)")
        params["location_summary"] = event.location_summary

    if "created_at" in cols:
        fields.append("created_at")
        values_placeholders.append("NOW()")

    sql = f"""
        INSERT INTO city_events ({', '.join(fields)})
        VALUES ({', '.join(values_placeholders)})
        ON CONFLICT ({conflict_target}) DO UPDATE SET
            {', '.join(update_assignments)}
        RETURNING id;
    """

    db_event_id: Optional[int] = None

    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        if row is not None:
            try:
                db_event_id = row["id"]
            except (KeyError, TypeError, IndexError):
                try:
                    db_event_id = row[0]
                except (KeyError, TypeError, IndexError):
                    pass

        # Fallback to query id if RETURNING id wasn't captured
        if db_event_id is None:
            cursor.execute("SELECT id FROM city_events WHERE url = %(url)s;", {"url": str(event.url)})
            lookup_row = cursor.fetchone()
            if lookup_row is not None:
                db_event_id = lookup_row["id"] if isinstance(lookup_row, dict) else lookup_row[0]

        # Batch insert matched associations into event_interest_tags (event_id, question_value_id)
        if db_event_id is not None and event.tag_ids:
            try:
                with conn.transaction():
                    tag_records = [(db_event_id, int(tag_id)) for tag_id in event.tag_ids]
                    cursor.executemany(
                        """
                        INSERT INTO event_interest_tags (event_id, question_value_id)
                        VALUES (%s, %s)
                        ON CONFLICT (event_id, question_value_id) DO NOTHING;
                        """,
                        tag_records,
                    )
                    logger.debug(
                        f"[DB] Inserted {len(tag_records)} interest tags for event id={db_event_id} (tag_ids={event.tag_ids})"
                    )
            except Exception as tag_err:
                logger.warning(
                    f"[DB] Failed to insert interest tags for event id={db_event_id}: {tag_err}"
                )

    return db_event_id
