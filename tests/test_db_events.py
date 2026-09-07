from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from src.config.database import get_db_pool
from src.db.events import upsert_city_event
from src.models.event import CityEvent


def test_db_pool_configuration():
    pool = get_db_pool(min_size=2, max_size=8)
    assert pool._min_size == 2
    assert pool._max_size == 8
    pool.close()


def test_upsert_city_event_sql_generation():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchone.return_value = {"id": 101}

    future_date = datetime.now(timezone.utc) + timedelta(days=3)
    event = CityEvent(
        event_id="eb_sql_test_101",
        city="Vancouver, BC",
        title="Family Puppet Show",
        url="https://eventbrite.ca/e/puppet-101",
        start_date=future_date,
        status="live",
        is_canceled=False,
    )

    upsert_city_event(mock_conn, event)

    assert mock_cursor.execute.called
    call_args = mock_cursor.execute.call_args_list[0]
    sql = call_args[0][0]
    params = call_args[0][1]

    assert "INSERT INTO city_events" in sql
    assert "ON CONFLICT" in sql
    assert "DO UPDATE SET" in sql
    assert "WHERE" in sql
    assert "IS DISTINCT FROM" in sql
    assert "RETURNING id" in sql
    assert params["city"] == "Vancouver, BC"
    assert params["title"] == "Family Puppet Show"
    assert params["status"] == "live"
    assert params["is_canceled"] is False


def test_upsert_city_event_duplicate_data_skips_update_and_uses_fallback_lookup():
    """
    Verifies that when duplicate data matches existing row,
    PostgreSQL WHERE clause skips update (returning no id from INSERT),
    and the fallback SELECT lookup retrieves the existing id without crashing or re-writing.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    # First fetchone (INSERT ... RETURNING id) returns None because update was skipped
    # Second fetchone (SELECT id FROM city_events ...) returns existing row id 205
    mock_cursor.fetchone.side_effect = [None, {"id": 205}]

    future_date = datetime.now(timezone.utc) + timedelta(days=2)
    event = CityEvent(
        event_id="eb_sql_dup_205",
        city="Vancouver, BC",
        title="Duplicate Event Title",
        url="https://eventbrite.ca/e/dup-205",
        start_date=future_date,
        status="live",
        is_canceled=False,
    )

    result_id = upsert_city_event(mock_conn, event)
    assert result_id == 205
    assert mock_cursor.execute.call_count == 2

    # Verify fallback lookup query
    fallback_call = mock_cursor.execute.call_args_list[1]
    fallback_sql = fallback_call[0][0]
    fallback_params = fallback_call[0][1]
    assert "SELECT id FROM city_events WHERE" in fallback_sql
    assert fallback_params["conflict_val"] == "eb_sql_dup_205"



def test_upsert_city_event_with_tag_ids_batch_insert():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchone.return_value = {"id": 42}

    future_date = datetime.now(timezone.utc) + timedelta(days=3)
    event = CityEvent(
        event_id="eb_sql_test_102",
        city="Vancouver, BC",
        title="Youth Soccer Camp",
        url="https://eventbrite.ca/e/soccer-102",
        start_date=future_date,
        tag_ids=[10, 20],
    )

    result_id = upsert_city_event(mock_conn, event)
    assert result_id == 42

    # Verify batch insert for event_interest_tags
    assert mock_cursor.executemany.called
    tag_call_args = mock_cursor.executemany.call_args
    tag_sql = tag_call_args[0][0]
    tag_records = tag_call_args[0][1]

    assert "INSERT INTO event_interest_tags" in tag_sql
    assert "ON CONFLICT (event_id, question_value_id) DO NOTHING" in tag_sql
    assert tag_records == [(42, 10), (42, 20)]


def test_get_active_interests_success():
    from src.db.events import get_active_interests

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    mock_pool.connection.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchall.return_value = [
        {"question_value_id": 1, "label": "Sports"},
        {"question_value_id": 2, "label": "art & crafts"},
        {"question_value_id": 3, "label": "music"},
    ]

    result = get_active_interests(mock_pool)
    assert result == {
        "sports": 1,
        "art & crafts": 2,
        "music": 3,
    }


def test_get_active_interests_graceful_error_handling():
    from src.db.events import get_active_interests

    mock_pool = MagicMock()
    mock_pool.connection.side_effect = Exception("Database Connection Refused")

    # Should catch gracefully and return {} without crashing
    result = get_active_interests(mock_pool)
    assert result == {}

