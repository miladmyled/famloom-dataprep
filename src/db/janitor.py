import os
import time
import logging
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime, timezone
from psycopg_pool import ConnectionPool
from psycopg import errors as pg_errors
from psycopg.errors import LockNotAvailable, QueryCanceled, OperationalError

from src.config.database import get_db_pool

logger = logging.getLogger(__name__)

# Expired: the event's start (city_events.date) is before today (UTC).
EXPIRED_WHERE = "date IS NOT NULL AND date < %(current_date)s"

# Classified for removal: the event's url is rejected or canceled in the classification cache,
# or in the review band while REVIEW_POLICY=drop.
CLASSIFIED_WHERE = """
    url IN (
        SELECT c.url FROM city_event_classifications c
        WHERE c.is_canceled
           OR c.decision = 'reject'
           OR (c.decision = 'review' AND %(drop_review)s)
    )
"""

PLAN_SQL = f"""
    SELECT ce.id, ce.url, ce.city, ce.source, ce.title, ce.date,
           c.decision, c.is_canceled, c.family_score, c.adult_score, c.provider,
           CASE WHEN c.is_canceled THEN 'canceled' ELSE c.decision END AS reason,
           (SELECT count(*) FROM activities a WHERE a.source_city_event_id = ce.id) AS linked_activities
    FROM city_events ce
    JOIN city_event_classifications c ON c.url = ce.url
    WHERE ce.{CLASSIFIED_WHERE.strip()}
      AND (c.is_canceled OR c.decision = 'reject' OR (c.decision = 'review' AND %(drop_review)s))
    ORDER BY ce.city, ce.source, ce.date;
"""

PRUNE_SQL = """
    DELETE FROM city_event_classifications c
    WHERE c.classified_at < NOW() - make_interval(days => %(retention_days)s)
      AND NOT EXISTS (SELECT 1 FROM city_events ce WHERE ce.url = c.url);
"""


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class DatabaseJanitor:
    """
    Nightly cleanup of city_events (daily CronJob):
      1. delete expired events (start date before today, UTC);
      2. delete events the classification cache marks rejected or canceled
         (JANITOR_REMOVE_CLASSIFIED, off by default);
      3. prune old classification rows whose event is no longer in city_events.
    Tags are removed by ON DELETE CASCADE; Activities keep their data because
    activities.source_city_event_id is ON DELETE SET NULL. Never runs DDL.
    """

    def __init__(
        self,
        pool: Optional[ConnectionPool] = None,
        max_retries: int = 3,
        lock_timeout_seconds: int = 10,
        statement_timeout_seconds: int = 30,
        remove_classified: Optional[bool] = None,
        drop_review: Optional[bool] = None,
        retention_days: Optional[int] = None,
    ):
        self.pool = pool or get_db_pool()
        self.max_retries = max_retries
        self.lock_timeout_seconds = lock_timeout_seconds
        self.statement_timeout_seconds = statement_timeout_seconds
        self.remove_classified = (
            _env_bool("JANITOR_REMOVE_CLASSIFIED", False) if remove_classified is None else remove_classified
        )
        self.drop_review = (
            os.getenv("REVIEW_POLICY", "drop").strip().lower() != "publish" if drop_review is None else drop_review
        )
        self.retention_days = int(
            retention_days if retention_days is not None else os.getenv("JANITOR_CLASSIFICATION_RETENTION_DAYS", "30")
        )

    def _execute_with_retry(self, label: str, work: Callable[[Any], int]) -> int:
        """Runs work(cursor) in its own transaction with lock/statement timeouts and retries."""
        for attempt in range(1, self.max_retries + 1):
            try:
                with self.pool.connection() as conn:
                    with conn.cursor() as cursor:
                        # Session-level timeouts so the janitor never blocks other writers for long
                        cursor.execute(f"SET lock_timeout = '{self.lock_timeout_seconds}s';")
                        cursor.execute(f"SET statement_timeout = '{self.statement_timeout_seconds}s';")
                        count = work(cursor)
                    conn.commit()
                return count

            except (LockNotAvailable, QueryCanceled) as lock_err:
                logger.warning(f"[JANITOR] {label}: lock contention (Attempt {attempt}/{self.max_retries}): {lock_err}")
                if attempt == self.max_retries:
                    logger.error(f"[JANITOR] {label}: max retries reached due to lock timeouts: {lock_err}")
                    raise
                time.sleep(1.5 * attempt)

            except OperationalError as op_err:
                logger.warning(f"[JANITOR] {label}: transient connection issue (Attempt {attempt}/{self.max_retries}): {op_err}")
                if attempt == self.max_retries:
                    logger.error(f"[JANITOR] {label}: failed after retries: {op_err}")
                    raise
                time.sleep(2.0 * attempt)

        return 0

    def purge_expired_events(self) -> int:
        """Deletes events whose start date (city_events.date) is before today (UTC)."""
        current_utc_date = datetime.now(timezone.utc).date()
        logger.info(f"[JANITOR] Initiating expired events purge (Reference Date: {current_utc_date} UTC)...")

        def work(cursor) -> int:
            cursor.execute(f"DELETE FROM city_events WHERE {EXPIRED_WHERE};", {"current_date": current_utc_date})
            return cursor.rowcount if cursor.rowcount is not None else 0

        deleted_count = self._execute_with_retry("expired", work)
        logger.info(f"Janitor run complete: {deleted_count} expired events purged.")
        return deleted_count

    def plan_classified_removal(self) -> Optional[List[Dict[str, Any]]]:
        """Rows that purge_classified_events would delete; None if the cache table is missing."""
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(PLAN_SQL, {"drop_review": self.drop_review})
                    return [dict(r) for r in cursor.fetchall()]
        except pg_errors.UndefinedTable:
            logger.warning("[JANITOR] city_event_classifications does not exist yet; classified removal skipped.")
            return None

    def purge_classified_events(self) -> int:
        """Deletes events whose url is rejected/canceled in the classification cache."""
        if not self.remove_classified:
            logger.info("[JANITOR] Classified removal disabled (JANITOR_REMOVE_CLASSIFIED=false).")
            return 0

        def work(cursor) -> int:
            cursor.execute(f"DELETE FROM city_events WHERE {CLASSIFIED_WHERE};", {"drop_review": self.drop_review})
            return cursor.rowcount if cursor.rowcount is not None else 0

        try:
            deleted = self._execute_with_retry("classified", work)
        except pg_errors.UndefinedTable:
            logger.warning("[JANITOR] city_event_classifications does not exist yet; classified removal skipped.")
            return 0
        logger.info(f"[JANITOR] {deleted} rejected/canceled events removed (drop_review={self.drop_review}).")
        return deleted

    def prune_classifications(self) -> int:
        """Deletes cache rows older than the retention whose event is no longer in city_events."""

        def work(cursor) -> int:
            cursor.execute(PRUNE_SQL, {"retention_days": self.retention_days})
            return cursor.rowcount if cursor.rowcount is not None else 0

        try:
            pruned = self._execute_with_retry("prune", work)
        except pg_errors.UndefinedTable:
            return 0
        logger.info(f"[JANITOR] {pruned} old classification rows pruned (retention {self.retention_days} days).")
        return pruned

    def close(self) -> None:
        """Closes the underlying connection pool cleanly."""
        try:
            self.pool.close()
            logger.info("[JANITOR] Database pool closed cleanly.")
        except Exception as e:
            logger.error(f"[JANITOR] Error closing DB pool: {e}")
