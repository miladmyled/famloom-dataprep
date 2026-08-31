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
    call_args = mock_cursor.execute.call_args
    sql = call_args[0][0]
    params = call_args[0][1]

    assert "INSERT INTO city_events" in sql
    assert "ON CONFLICT" in sql
    assert "DO UPDATE SET" in sql
    assert params["city"] == "Vancouver, BC"
    assert params["title"] == "Family Puppet Show"
    assert params["status"] == "live"
    assert params["is_canceled"] is False
