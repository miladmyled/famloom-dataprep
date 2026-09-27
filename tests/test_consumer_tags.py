import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.consumer.event_consumer import EventKafkaConsumer
from src.db.events import upsert_city_event
from tests.conftest import make_event


def _conn_with_id(event_id=42):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"id": event_id}
    return conn, cursor


def test_replace_tags_deletes_stale_managed_tags_only_then_inserts():
    conn, cursor = _conn_with_id(42)
    upsert_city_event(conn, make_event("e1", tag_ids=[503, 42], replace_tags=True))

    delete_sql, params = cursor.execute.call_args_list[1][0]
    assert "DELETE FROM event_interest_tags" in delete_sql
    assert "q.code = ANY(%(managed_codes)s)" in delete_sql
    assert "NOT (t.question_value_id = ANY(%(keep_ids)s))" in delete_sql
    assert params == {"event_id": 42, "managed_codes": ["interests", "languages"], "keep_ids": [42, 503]}
    insert_sql, rows = cursor.executemany.call_args[0]
    assert rows == [(42, 42), (42, 503)]


def test_replace_with_empty_tags_clears_managed_tags():
    conn, cursor = _conn_with_id(7)
    upsert_city_event(conn, make_event("e1", tag_ids=[], replace_tags=True))
    _, params = cursor.execute.call_args_list[1][0]
    assert params["keep_ids"] == []
    cursor.executemany.assert_not_called()


def test_message_without_replace_flag_keeps_existing_tags():
    conn, cursor = _conn_with_id(9)
    upsert_city_event(conn, make_event("e1", tag_ids=[]))
    assert len(cursor.execute.call_args_list) == 1  # only the upsert
    cursor.executemany.assert_not_called()


@pytest.fixture
def consumer():
    with patch("src.consumer.event_consumer.get_db_pool"), patch("src.consumer.event_consumer.Consumer") as k:
        k.return_value = MagicMock()
        yield EventKafkaConsumer()


def _msg(payload, offset=5):
    msg = MagicMock()
    msg.error.return_value = None
    msg.value.return_value = json.dumps(payload).encode("utf-8")
    msg.partition.return_value = 0
    msg.offset.return_value = offset
    msg.topic.return_value = "raw-events-ingestion"
    return msg


def _payload(**kw):
    p = {
        "event_id": "eventbrite_1",
        "city": "Vancouver, BC",
        "title": "Storytime",
        "url": "https://eventbrite.ca/e/1",
        "start_date": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    }
    p.update(kw)
    return p


def test_legacy_canceled_message_is_skipped_not_written(consumer):
    with patch("src.consumer.event_consumer.upsert_city_event") as upsert:
        assert consumer.process_message(_msg(_payload(is_canceled=True, status="canceled"))) is True
        upsert.assert_not_called()
    consumer.consumer.commit.assert_called_once()


def test_batch_skips_canceled_and_commits_highest_offset(consumer):
    live, canceled = _msg(_payload(), offset=10), _msg(_payload(event_id="x", url="https://e/x", is_canceled=True), offset=11)
    with patch("src.consumer.event_consumer.upsert_city_event") as upsert:
        assert consumer.process_batch([live, canceled]) == 1
        assert upsert.call_count == 1
    assert consumer.consumer.commit.call_args[1]["message"] is canceled


def test_consumer_reads_producer_topic_variable(monkeypatch):
    monkeypatch.setenv("KAFKA_TOPIC", "dev-topic")
    monkeypatch.delenv("KAFKA_TOPIC_NAME", raising=False)
    with patch("src.consumer.event_consumer.get_db_pool"), patch("src.consumer.event_consumer.Consumer"):
        assert EventKafkaConsumer().topic == "dev-topic"
