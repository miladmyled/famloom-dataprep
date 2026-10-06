import logging
from typing import Dict, List, Optional, Tuple
from psycopg_pool import ConnectionPool
from psycopg import Connection
from src.models.event import CityEvent

logger = logging.getLogger(__name__)

# The city_events schema is owned by the app repo's migrations. Dataprep writes exactly these
# columns (id, created_at and updated_at are handled below) and never runs DDL on the table.
CITY_EVENTS_COLUMNS: Tuple[str, ...] = (
    "id",
    "city",
    "title",
    "source",
    "url",
    "date",
    "pictureurl",
    "created_at",
    "updated_at",
)

# Questions whose tags dataprep owns and may replace; tags of any other question are never touched.
MANAGED_TAG_QUESTION_CODES: Tuple[str, ...] = ("interests", "languages")


def get_active_interests(pool: Optional[ConnectionPool] = None) -> Dict[str, int]:
    """
    Backwards-compatible mapping of lowercase interest label -> question_value_id.
    Thin wrapper over the classification taxonomy; returns {} on any database error.
    """
    from src.classify.taxonomy import load_taxonomy_from_pool

    values = load_taxonomy_from_pool(pool, codes=("interests",))
    return {v.label.strip().lower(): v.value_id for v in values if v.label.strip()}


_UPSERT_SQL = """
    INSERT INTO city_events (id, city, title, source, url, date, pictureurl, created_at, updated_at)
    VALUES (
        (SELECT COALESCE(MAX(id), 0) + 1 FROM city_events),
        %(city)s, %(title)s, %(source)s, %(url)s, %(date)s, %(pictureurl)s, NOW(), NOW()
    )
    ON CONFLICT (url) DO UPDATE SET
        city = EXCLUDED.city,
        title = EXCLUDED.title,
        source = EXCLUDED.source,
        date = EXCLUDED.date,
        pictureurl = COALESCE(EXCLUDED.pictureurl, city_events.pictureurl),
        updated_at = NOW()
    WHERE
        city_events.city IS DISTINCT FROM EXCLUDED.city OR
        city_events.title IS DISTINCT FROM EXCLUDED.title OR
        city_events.source IS DISTINCT FROM EXCLUDED.source OR
        city_events.date IS DISTINCT FROM EXCLUDED.date OR
        city_events.pictureurl IS DISTINCT FROM COALESCE(EXCLUDED.pictureurl, city_events.pictureurl)
    RETURNING id;
"""

_DELETE_STALE_MANAGED_TAGS_SQL = """
    DELETE FROM event_interest_tags t
    USING question_values v
    JOIN questions q ON q.id = v.question_id
    WHERE t.event_id = %(event_id)s
      AND t.question_value_id = v.id
      AND q.code = ANY(%(managed_codes)s)
      AND NOT (t.question_value_id = ANY(%(keep_ids)s));
"""

_INSERT_TAG_SQL = """
    INSERT INTO event_interest_tags (event_id, question_value_id)
    VALUES (%s, %s)
    ON CONFLICT (event_id, question_value_id) DO NOTHING;
"""


def _row_id(row) -> Optional[int]:
    if row is None:
        return None
    try:
        return row["id"]
    except (KeyError, TypeError, IndexError):
        try:
            return row[0]
        except (KeyError, TypeError, IndexError):
            return None


def upsert_city_event(conn: Connection, event: CityEvent) -> Optional[int]:
    """
    Idempotent upsert of one event into city_events, matched by url (the table's unique key).
    Writes only the columns in CITY_EVENTS_COLUMNS; `date` is the event start (UTC).
    Then writes tags into event_interest_tags:
      - replace_tags=True: managed tags (interests/languages) not in tag_ids are removed first,
        so the stored set equals tag_ids; tags of other questions are left untouched.
      - replace_tags=False (legacy messages): tag_ids are only added, nothing is removed.
    Returns city_events.id, or None if it could not be resolved.
    """
    params = {
        "city": event.city,
        "title": event.title,
        "source": event.source,
        "url": str(event.url),
        "date": event.start_date,
        "pictureurl": event.pictureurl,
    }

    with conn.cursor() as cursor:
        cursor.execute(_UPSERT_SQL, params)
        db_event_id = _row_id(cursor.fetchone())

        # RETURNING yields nothing when the WHERE clause skipped an unchanged row
        if db_event_id is None:
            cursor.execute("SELECT id FROM city_events WHERE url = %(url)s LIMIT 1;", {"url": params["url"]})
            db_event_id = _row_id(cursor.fetchone())

        if db_event_id is not None and (event.tag_ids or event.replace_tags):
            _write_tags(conn, cursor, db_event_id, event)

    return db_event_id


def _write_tags(conn: Connection, cursor, db_event_id: int, event: CityEvent) -> None:
    tag_ids: List[int] = sorted({int(t) for t in event.tag_ids})
    try:
        # Savepoint: a bad tag id must not roll back the event row itself
        with conn.transaction():
            if event.replace_tags:
                cursor.execute(
                    _DELETE_STALE_MANAGED_TAGS_SQL,
                    {
                        "event_id": db_event_id,
                        "managed_codes": list(MANAGED_TAG_QUESTION_CODES),
                        "keep_ids": tag_ids,
                    },
                )
            if tag_ids:
                cursor.executemany(_INSERT_TAG_SQL, [(db_event_id, tag_id) for tag_id in tag_ids])
            logger.debug(f"[DB] Tags written for event id={db_event_id} (tag_ids={tag_ids}, replace={event.replace_tags})")
    except Exception as tag_err:
        logger.warning(f"[DB] Failed to write tags for event id={db_event_id}: {tag_err}")
