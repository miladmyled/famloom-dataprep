from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from psycopg.errors import LockNotAvailable
from src.db.janitor import DatabaseJanitor
from janitor import run_janitor


def test_janitor_purge_query_execution():
    with patch("src.db.janitor.init_db_schema"):
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 42
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_pool.connection.return_value.__enter__.return_value = mock_conn

        janitor = DatabaseJanitor(pool=mock_pool, lock_timeout_seconds=5, statement_timeout_seconds=15)
        deleted_count = janitor.purge_expired_events()

        assert deleted_count == 42
        assert mock_cursor.execute.call_count == 3  # SET lock_timeout, SET statement_timeout, DELETE

        calls = mock_cursor.execute.call_args_list
        assert "SET lock_timeout = '5s'" in calls[0][0][0]
        assert "SET statement_timeout = '15s'" in calls[1][0][0]

        delete_call = calls[2]
        sql = delete_call[0][0]
        params = delete_call[0][1]

        assert "DELETE FROM city_events" in sql
        assert "end_date < %(current_date)s" in sql or "date < %(current_date)s" in sql
        assert params["current_date"] == datetime.now(timezone.utc).date()
        mock_conn.commit.assert_called_once()


def test_janitor_lock_timeout_retry():
    with patch("src.db.janitor.init_db_schema"):
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_pool.connection.return_value.__enter__.return_value = mock_conn

        # First attempt raises LockNotAvailable, second attempt succeeds with 10 rows deleted
        lock_err = LockNotAvailable()
        mock_cursor.execute.side_effect = [
            None, None, lock_err,  # Attempt 1 fails on DELETE
            None, None, None       # Attempt 2 succeeds
        ]
        mock_cursor.rowcount = 10

        with patch("time.sleep") as mock_sleep:
            janitor = DatabaseJanitor(pool=mock_pool, max_retries=2)
            deleted_count = janitor.purge_expired_events()

            assert deleted_count == 10
            mock_sleep.assert_called_once()


def test_run_janitor_entrypoint():
    with patch("janitor.DatabaseJanitor") as mock_janitor_cls:
        mock_janitor_instance = MagicMock()
        mock_janitor_instance.purge_expired_events.return_value = 5
        mock_janitor_cls.return_value = mock_janitor_instance

        exit_code = run_janitor()

        assert exit_code == 0
        mock_janitor_instance.purge_expired_events.assert_called_once()
        mock_janitor_instance.close.assert_called_once()
