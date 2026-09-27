from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from psycopg import errors as pg_errors
from psycopg.errors import LockNotAvailable

from src.db.janitor import DatabaseJanitor
from janitor import run_janitor


def _pool_with_cursor():
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_pool.connection.return_value.__enter__.return_value = mock_conn
    return mock_pool, mock_conn, mock_cursor


def test_janitor_purge_expired_uses_date_column_only():
    mock_pool, mock_conn, mock_cursor = _pool_with_cursor()
    mock_cursor.rowcount = 42

    janitor = DatabaseJanitor(pool=mock_pool, lock_timeout_seconds=5, statement_timeout_seconds=15)
    assert janitor.purge_expired_events() == 42

    calls = mock_cursor.execute.call_args_list
    assert len(calls) == 3  # SET lock_timeout, SET statement_timeout, DELETE
    assert "SET lock_timeout = '5s'" in calls[0][0][0]
    assert "SET statement_timeout = '15s'" in calls[1][0][0]
    sql, params = calls[2][0]
    assert "DELETE FROM city_events WHERE date IS NOT NULL AND date < %(current_date)s" in sql
    assert "start_date" not in sql and "end_date" not in sql
    assert params["current_date"] == datetime.now(timezone.utc).date()
    mock_conn.commit.assert_called_once()


def test_janitor_lock_timeout_retry():
    mock_pool, _, mock_cursor = _pool_with_cursor()
    mock_cursor.execute.side_effect = [None, None, LockNotAvailable(), None, None, None]
    mock_cursor.rowcount = 10

    with patch("time.sleep") as mock_sleep:
        janitor = DatabaseJanitor(pool=mock_pool, max_retries=2)
        assert janitor.purge_expired_events() == 10
        mock_sleep.assert_called_once()


def test_classified_removal_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JANITOR_REMOVE_CLASSIFIED", raising=False)
    mock_pool, _, mock_cursor = _pool_with_cursor()
    janitor = DatabaseJanitor(pool=mock_pool)
    assert janitor.remove_classified is False
    assert janitor.purge_classified_events() == 0
    mock_cursor.execute.assert_not_called()


def test_classified_removal_targets_reject_canceled_and_review_when_drop():
    mock_pool, _, mock_cursor = _pool_with_cursor()
    mock_cursor.rowcount = 7

    janitor = DatabaseJanitor(pool=mock_pool, remove_classified=True, drop_review=True)
    assert janitor.purge_classified_events() == 7

    sql, params = mock_cursor.execute.call_args_list[2][0]
    assert sql.startswith("DELETE FROM city_events WHERE")
    assert "city_event_classifications" in sql
    assert "c.is_canceled" in sql and "c.decision = 'reject'" in sql
    assert "c.decision = 'review' AND %(drop_review)s" in sql
    assert params == {"drop_review": True}


def test_classified_removal_keeps_review_when_policy_publish(monkeypatch):
    monkeypatch.setenv("REVIEW_POLICY", "publish")
    mock_pool, _, _ = _pool_with_cursor()
    assert DatabaseJanitor(pool=mock_pool, remove_classified=True).drop_review is False


def test_classified_removal_skipped_when_table_missing():
    mock_pool, _, mock_cursor = _pool_with_cursor()
    mock_cursor.execute.side_effect = [None, None, pg_errors.UndefinedTable("missing")]
    janitor = DatabaseJanitor(pool=mock_pool, remove_classified=True)
    assert janitor.purge_classified_events() == 0


def test_prune_classifications_uses_retention_and_keeps_live_rows():
    mock_pool, _, mock_cursor = _pool_with_cursor()
    mock_cursor.rowcount = 3
    janitor = DatabaseJanitor(pool=mock_pool, retention_days=30)
    assert janitor.prune_classifications() == 3
    sql, params = mock_cursor.execute.call_args_list[2][0]
    assert "DELETE FROM city_event_classifications" in sql
    assert "NOT EXISTS (SELECT 1 FROM city_events ce WHERE ce.url = c.url)" in sql
    assert params == {"retention_days": 30}


def test_run_janitor_entrypoint_runs_all_steps():
    with patch("janitor.DatabaseJanitor") as mock_cls:
        inst = MagicMock()
        inst.remove_classified = True
        inst.purge_expired_events.return_value = 5
        inst.purge_classified_events.return_value = 2
        inst.prune_classifications.return_value = 1
        mock_cls.return_value = inst

        assert run_janitor() == 0
        inst.purge_expired_events.assert_called_once()
        inst.purge_classified_events.assert_called_once()
        inst.prune_classifications.assert_called_once()
        inst.close.assert_called_once()


def test_run_janitor_dry_run_deletes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_NAME", "x3db_dev")
    monkeypatch.setattr("janitor.REPORTS_DIR", tmp_path)
    with patch("janitor.DatabaseJanitor") as mock_cls:
        inst = MagicMock()
        inst.plan_classified_removal.return_value = [
            {"id": 1, "url": "https://x/1", "city": "Vancouver", "source": "Meetup", "title": "Bar night",
             "date": None, "reason": "reject", "decision": "reject", "is_canceled": False,
             "family_score": 0.1, "adult_score": 0.9, "provider": "jev", "linked_activities": 0}
        ]
        mock_cls.return_value = inst

        assert run_janitor(["--dry-run"]) == 0
        inst.purge_expired_events.assert_not_called()
        inst.purge_classified_events.assert_not_called()
        assert len(list(tmp_path.glob("janitor_plan_*.csv"))) == 1


def test_run_janitor_manual_run_refuses_production(monkeypatch):
    monkeypatch.setenv("DB_NAME", "x3db")
    with patch("janitor.DatabaseJanitor") as mock_cls:
        assert run_janitor(["--dry-run"]) == 1
        mock_cls.assert_not_called()
