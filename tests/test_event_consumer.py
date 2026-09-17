import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest
from confluent_kafka import KafkaError

from src.consumer.event_consumer import EventKafkaConsumer


@pytest.fixture
def mock_consumer_deps():
    with patch("src.consumer.event_consumer.get_db_pool") as mock_get_pool, \
         patch("src.consumer.event_consumer.init_db_schema") as mock_init_schema, \
         patch("src.consumer.event_consumer.Consumer") as mock_kafka_consumer_cls:
        
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_k_consumer = MagicMock()
        mock_kafka_consumer_cls.return_value = mock_k_consumer

        yield mock_pool, mock_k_consumer


def test_consumer_initialization(mock_consumer_deps):
    mock_pool, mock_k_consumer = mock_consumer_deps
    consumer = EventKafkaConsumer()

    assert consumer.topic == "raw-events-ingestion"
    mock_k_consumer.subscribe.assert_called_once()
    assert consumer.running is False


def test_process_message_valid_event(mock_consumer_deps):
    mock_pool, mock_k_consumer = mock_consumer_deps
    mock_conn = MagicMock()
    mock_pool.connection.return_value.__enter__.return_value = mock_conn

    future_date = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    event_dict = {
        "event_id": "kafka_test_001",
        "city": "Vancouver, BC",
        "title": "Science World Kids Camp",
        "url": "https://eventbrite.ca/e/science-001",
        "start_date": future_date,
        "status": "live",
        "is_canceled": False,
        "tag_ids": [1, 2],
    }

    mock_msg = MagicMock()
    mock_msg.error.return_value = None
    mock_msg.value.return_value = json.dumps(event_dict).encode("utf-8")
    mock_msg.key.return_value = b"kafka_test_001"
    mock_msg.topic.return_value = "raw-events-ingestion"
    mock_msg.partition.return_value = 0
    mock_msg.offset.return_value = 100

    consumer = EventKafkaConsumer()
    with patch("src.consumer.event_consumer.upsert_city_event") as mock_upsert:
        success = consumer.process_message(mock_msg)

        assert success is True
        mock_upsert.assert_called_once()
        mock_k_consumer.commit.assert_called_once_with(message=mock_msg, asynchronous=True)


def test_process_message_tombstone(mock_consumer_deps):
    mock_pool, mock_k_consumer = mock_consumer_deps

    mock_msg = MagicMock()
    mock_msg.error.return_value = None
    mock_msg.value.return_value = None
    mock_msg.partition.return_value = 0
    mock_msg.offset.return_value = 101

    consumer = EventKafkaConsumer()
    success = consumer.process_message(mock_msg)

    assert success is True
    mock_k_consumer.commit.assert_called_once_with(message=mock_msg, asynchronous=False)


def test_process_message_poison_pill_corrupt_json(mock_consumer_deps):
    mock_pool, mock_k_consumer = mock_consumer_deps

    mock_msg = MagicMock()
    mock_msg.error.return_value = None
    mock_msg.value.return_value = b"INVALID NOT JSON {{"
    mock_msg.topic.return_value = "raw-events-ingestion"
    mock_msg.partition.return_value = 0
    mock_msg.offset.return_value = 102

    consumer = EventKafkaConsumer()
    success = consumer.process_message(mock_msg)

    assert success is True
    # Offsets should be committed past poison pills to prevent deadlocks
    mock_k_consumer.commit.assert_called_once_with(message=mock_msg, asynchronous=True)


def test_process_message_poison_pill_invalid_schema(mock_consumer_deps):
    mock_pool, mock_k_consumer = mock_consumer_deps

    # Missing mandatory event fields (title, url, start_date)
    corrupt_data = {"something": "else"}
    mock_msg = MagicMock()
    mock_msg.error.return_value = None
    mock_msg.value.return_value = json.dumps(corrupt_data).encode("utf-8")
    mock_msg.topic.return_value = "raw-events-ingestion"
    mock_msg.partition.return_value = 0
    mock_msg.offset.return_value = 103

    consumer = EventKafkaConsumer()
    success = consumer.process_message(mock_msg)

    assert success is True
    mock_k_consumer.commit.assert_called_once_with(message=mock_msg, asynchronous=True)


def test_process_message_db_transient_error(mock_consumer_deps):
    mock_pool, mock_k_consumer = mock_consumer_deps

    mock_conn = MagicMock()
    mock_pool.connection.return_value.__enter__.return_value = mock_conn

    future_date = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    event_dict = {
        "event_id": "kafka_test_004",
        "city": "Vancouver, BC",
        "title": "Aquarium Tour",
        "url": "https://eventbrite.ca/e/aqua-004",
        "start_date": future_date,
    }

    mock_msg = MagicMock()
    mock_msg.error.return_value = None
    mock_msg.value.return_value = json.dumps(event_dict).encode("utf-8")
    mock_msg.topic.return_value = "raw-events-ingestion"
    mock_msg.partition.return_value = 0
    mock_msg.offset.return_value = 104

    consumer = EventKafkaConsumer()
    with patch("src.consumer.event_consumer.upsert_city_event", side_effect=Exception("DB Connection Lost")):
        success = consumer.process_message(mock_msg)

        # Failure should return False so message is retried, NOT committed
        assert success is False
        mock_k_consumer.commit.assert_not_called()


def test_process_message_partition_eof(mock_consumer_deps):
    mock_pool, mock_k_consumer = mock_consumer_deps

    mock_error = MagicMock()
    mock_error.code.return_value = KafkaError._PARTITION_EOF

    mock_msg = MagicMock()
    mock_msg.error.return_value = mock_error
    mock_msg.topic.return_value = "raw-events-ingestion"
    mock_msg.partition.return_value = 0
    mock_msg.offset.return_value = 200

    consumer = EventKafkaConsumer()
    success = consumer.process_message(mock_msg)
    assert success is True


def test_process_batch_success(mock_consumer_deps):
    mock_pool, mock_k_consumer = mock_consumer_deps
    mock_conn = MagicMock()
    mock_pool.connection.return_value.__enter__.return_value = mock_conn

    future_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    events = [
        {"event_id": f"batch_{i}", "city": "Vancouver, BC", "title": f"Camp {i}", "url": f"https://e.com/{i}", "start_date": future_date, "status": "live", "is_canceled": False}
        for i in range(3)
    ]

    mock_msgs = []
    for idx, ev in enumerate(events):
        m = MagicMock()
        m.error.return_value = None
        m.value.return_value = json.dumps(ev).encode("utf-8")
        m.offset.return_value = 1000 + idx
        mock_msgs.append(m)

    consumer = EventKafkaConsumer()
    with patch("src.consumer.event_consumer.upsert_city_event") as mock_upsert:
        processed = consumer.process_batch(mock_msgs)
        assert processed == 3
        assert mock_upsert.call_count == 3
        mock_k_consumer.commit.assert_called_once_with(message=mock_msgs[-1], asynchronous=True)
