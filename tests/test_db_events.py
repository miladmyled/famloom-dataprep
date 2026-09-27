from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from src.config.database import get_db_pool
from src.db.events import CITY_EVENTS_COLUMNS, upsert_city_event
from src.models.event import CityEvent


def _mock_conn(fetchone):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    if isinstance(fetchone, list):
        mock_cursor.fetchone.side_effect = fetchone
    else:
        mock_cursor.fetchone.return_value = fetchone
    return mock_conn, mock_cursor


def _event(**kw) -> CityEvent:
    defaults = dict(
        event_id="eb_sql_test_101",
        city="Vancouver, BC",
        title="Family Puppet Show",
        url="https://eventbrite.ca/e/puppet-101",
        start_date=datetime.now(timezone.utc) + timedelta(days=3),
    )
    defaults.update(kw)
    return CityEvent(**defaults)


def test_db_pool_configuration():
    pool = get_db_pool(min_size=2, max_size=8)
    assert pool._min_size == 2
    assert pool._max_size == 8
    pool.close()


def test_city_events_columns_are_the_fixed_app_schema():
    assert CITY_EVENTS_COLUMNS == (
        "id", "city", "title", "source", "url", "date", "pictureurl", "created_at", "updated_at",
    )


def test_upsert_writes_only_existing_columns_and_conflicts_on_url():
    mock_conn, mock_cursor = _mock_conn({"id": 101})
    event = _event(description="not persisted", location_summary="not persisted", status="live")

    upsert_city_event(mock_conn, event)

    sql, params = mock_cursor.execute.call_args_list[0][0]
    assert "INSERT INTO city_events (id, city, title, source, url, date, pictureurl, created_at, updated_at)" in sql
    assert "ON CONFLICT (url) DO UPDATE SET" in sql
    assert "IS DISTINCT FROM" in sql and "RETURNING id" in sql
    for absent in ("event_id", "start_date", "end_date", "description", "location_summary", "status", "is_canceled"):
        assert absent not in sql
    assert set(params) == {"city", "title", "source", "url", "date", "pictureurl"}
    assert params["date"] == event.start_date
    assert params["url"] == "https://eventbrite.ca/e/puppet-101"


def test_upsert_never_runs_ddl():
    mock_conn, mock_cursor = _mock_conn({"id": 1})
    upsert_city_event(mock_conn, _event(tag_ids=[1], replace_tags=True))
    executed = " ".join(str(c[0][0]) for c in mock_cursor.execute.call_args_list).upper()
    assert "CREATE TABLE" not in executed and "ALTER TABLE" not in executed


def test_upsert_unchanged_row_uses_url_lookup():
    mock_conn, mock_cursor = _mock_conn([None, {"id": 205}])

    result_id = upsert_city_event(mock_conn, _event(url="https://eventbrite.ca/e/dup-205"))

    assert result_id == 205
    lookup_sql, lookup_params = mock_cursor.execute.call_args_list[1][0]
    assert "SELECT id FROM city_events WHERE url = %(url)s" in lookup_sql
    assert lookup_params == {"url": "https://eventbrite.ca/e/dup-205"}


def test_legacy_message_tags_are_add_only():
    mock_conn, mock_cursor = _mock_conn({"id": 42})

    upsert_city_event(mock_conn, _event(tag_ids=[20, 10]))

    assert all("DELETE" not in str(c[0][0]) for c in mock_cursor.execute.call_args_list)
    tag_sql, tag_records = mock_cursor.executemany.call_args[0]
    assert "INSERT INTO event_interest_tags" in tag_sql
    assert "ON CONFLICT (event_id, question_value_id) DO NOTHING" in tag_sql
    assert tag_records == [(42, 10), (42, 20)]


def test_upsert_with_pictureurl():
    mock_conn, mock_cursor = _mock_conn({"id": 105})
    upsert_city_event(mock_conn, _event(pictureurl="https://img.evbuc.com/images/999/original.jpg"))
    sql, params = mock_cursor.execute.call_args_list[0][0]
    assert "pictureurl = COALESCE(EXCLUDED.pictureurl, city_events.pictureurl)" in sql
    assert params["pictureurl"] == "https://img.evbuc.com/images/999/original.jpg"


def test_get_active_interests_success():
    from src.db.events import get_active_interests

    mock_pool = MagicMock()
    mock_cursor = MagicMock()
    mock_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchall.return_value = [
        {"value_id": 1, "code": "interests", "value_code": "sports", "label": "Sports"},
        {"value_id": 2, "code": "interests", "value_code": "art", "label": "art & crafts"},
        {"value_id": 3, "code": "interests", "value_code": "music", "label": "music"},
    ]

    assert get_active_interests(mock_pool) == {"sports": 1, "art & crafts": 2, "music": 3}
    assert mock_cursor.execute.call_args[0][1] == {"codes": ["interests"]}


def test_get_active_interests_graceful_error_handling():
    from src.db.events import get_active_interests

    mock_pool = MagicMock()
    mock_pool.connection.side_effect = Exception("Database Connection Refused")
    assert get_active_interests(mock_pool) == {}
