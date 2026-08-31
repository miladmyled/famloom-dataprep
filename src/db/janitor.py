import time
import logging
from typing import Optional
from datetime import datetime, timezone
from psycopg_pool import ConnectionPool
from psycopg.errors import LockNotAvailable, QueryCanceled, OperationalError

from src.config.database import get_db_pool
from src.db.events import init_db_schema, get_available_columns

logger = logging.getLogger(__name__)


class DatabaseJanitor:
    """
    Lightweight, production-grade event janitor for Azure PostgreSQL.
    Purges expired single-day and multi-day events from the city_events table
    while handling lock timeouts, transient drops, and emitting telemetry.
    """

    def __init__(
        self,
        pool: Optional[ConnectionPool] = None,
        max_retries: int = 3,
        lock_timeout_seconds: int = 10,
        statement_timeout_seconds: int = 30,
    ):
        self.pool = pool or get_db_pool()
        self.max_retries = max_retries
        self.lock_timeout_seconds = lock_timeout_seconds
        self.statement_timeout_seconds = statement_timeout_seconds

        # Ensure schema check has run and detected active columns
        try:
            init_db_schema(self.pool)
        except Exception as e:
            logger.debug(f"[JANITOR] Schema init note: {e}")

    def purge_expired_events(self) -> int:
        """
        Executes a DELETE statement on the city_events table to clean up past events.
        Business Logic:
          - Delete IF end_date is strictly before CURRENT_DATE (timezone-aware UTC)
          - OR IF end_date is NULL and start_date (or legacy date) is strictly before CURRENT_DATE.

        Returns:
            int: The exact number of rows deleted.
        """
        current_utc_date = datetime.now(timezone.utc).date()
        cols = get_available_columns()

        logger.info(
            f"[JANITOR] Initiating expired events purge (Reference Date: {current_utc_date} UTC)..."
        )

        # Construct SQL based on available columns in the database
        if "end_date" in cols and "start_date" in cols:
            delete_sql = """
                DELETE FROM city_events
                WHERE (end_date IS NOT NULL AND end_date < %(current_date)s)
                   OR (end_date IS NULL AND start_date < %(current_date)s);
            """
        elif "end_date" in cols and "date" in cols:
            delete_sql = """
                DELETE FROM city_events
                WHERE (end_date IS NOT NULL AND end_date < %(current_date)s)
                   OR (end_date IS NULL AND date < %(current_date)s);
            """
        else:
            delete_sql = """
                DELETE FROM city_events
                WHERE date IS NOT NULL AND date < %(current_date)s;
            """

        params = {"current_date": current_utc_date}

        for attempt in range(1, self.max_retries + 1):
            try:
                with self.pool.connection() as conn:
                    with conn.cursor() as cursor:
                        # Set session-level lock and statement timeouts to avoid blocking other writers
                        cursor.execute(f"SET lock_timeout = '{self.lock_timeout_seconds}s';")
                        cursor.execute(f"SET statement_timeout = '{self.statement_timeout_seconds}s';")

                        cursor.execute(delete_sql, params)
                        deleted_count = cursor.rowcount if cursor.rowcount is not None else 0

                    conn.commit()

                # Telemetry logging to standard output for Kubernetes logs
                logger.info(f"Janitor run complete: {deleted_count} expired events purged.")
                return deleted_count

            except (LockNotAvailable, QueryCanceled) as lock_err:
                logger.warning(
                    f"[JANITOR] Database lock contention (Attempt {attempt}/{self.max_retries}): {lock_err}"
                )
                if attempt == self.max_retries:
                    logger.error(f"[JANITOR] Max retries reached due to lock timeouts: {lock_err}")
                    raise
                time.sleep(1.5 * attempt)

            except OperationalError as op_err:
                logger.warning(
                    f"[JANITOR] Transient connection issue (Attempt {attempt}/{self.max_retries}): {op_err}"
                )
                if attempt == self.max_retries:
                    logger.error(f"[JANITOR] Failed to execute janitor query after retries: {op_err}")
                    raise
                time.sleep(2.0 * attempt)

            except Exception as e:
                logger.error(f"[JANITOR] Unexpected error during purge: {e}")
                raise

        return 0

    def close(self) -> None:
        """Closes the underlying connection pool cleanly."""
        try:
            self.pool.close()
            logger.info("[JANITOR] Database pool closed cleanly.")
        except Exception as e:
            logger.error(f"[JANITOR] Error closing DB pool: {e}")
