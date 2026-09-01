import json
import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from confluent_kafka import TopicPartition, OFFSET_BEGINNING
from src.consumer.event_consumer import EventKafkaConsumer


def test_consumer_configuration():
    with patch("src.consumer.event_consumer.Consumer") as mock_consumer_cls, \
         patch("src.consumer.event_consumer.init_db_schema"), \
         patch.dict("os.environ", {"KAFKA_DIRECT_ASSIGN": "false"}):

        mock_consumer_inst = MagicMock()
        mock_consumer_cls.return_value = mock_consumer_inst
        mock_db_pool = MagicMock()

        consumer = EventKafkaConsumer(
            kafka_config={"bootstrap.servers": "localhost:9092", "group.id": "test-group", "enable.auto.commit": False},
            topic="raw-events-ingestion",
            db_pool=mock_db_pool,
        )

        assert consumer.config["enable.auto.commit"] is False
        assert consumer.config["group.id"] == "test-group"
        mock_consumer_inst.subscribe.assert_called_once()
        call_args, call_kwargs = mock_consumer_inst.subscribe.call_args
        assert call_args[0] == ["raw-events-ingestion"]
        assert "on_assign" in call_kwargs
        assert "on_revoke" in call_kwargs


def test_consumer_direct_assignment_mode():
    with patch("src.consumer.event_consumer.Consumer") as mock_consumer_cls, \
         patch("src.consumer.event_consumer.init_db_schema"), \
         patch.dict("os.environ", {"KAFKA_DIRECT_ASSIGN": "true", "KAFKA_RESET_OFFSET_ON_START": "true"}):

        mock_consumer_inst = MagicMock()
        mock_consumer_cls.return_value = mock_consumer_inst
        mock_db_pool = MagicMock()

        consumer = EventKafkaConsumer(
            kafka_config={"bootstrap.servers": "localhost:9092", "group.id": "test-group", "enable.auto.commit": False},
            topic="raw-events-ingestion",
            db_pool=mock_db_pool,
        )

        mock_consumer_inst.assign.assert_called_once()
        assigned_pts = mock_consumer_inst.assign.call_args[0][0]
        assert len(assigned_pts) == 1
        assert assigned_pts[0].topic == "raw-events-ingestion"
        assert assigned_pts[0].partition == 0
        assert assigned_pts[0].offset == OFFSET_BEGINNING


def test_consumer_rebalance_on_assign_auto_reset():
    with patch("src.consumer.event_consumer.Consumer") as mock_consumer_cls, \
         patch("src.consumer.event_consumer.init_db_schema"), \
         patch.dict("os.environ", {"KAFKA_DIRECT_ASSIGN": "false", "KAFKA_RESET_OFFSET_ON_START": "true"}):

        mock_consumer_inst = MagicMock()
        mock_consumer_cls.return_value = mock_consumer_inst
        mock_db_pool = MagicMock()

        consumer = EventKafkaConsumer(
            kafka_config={"bootstrap.servers": "localhost:9092", "group.id": "test-group", "enable.auto.commit": False},
            topic="raw-events-ingestion",
            db_pool=mock_db_pool,
        )

        call_args, call_kwargs = mock_consumer_inst.subscribe.call_args
        on_assign = call_kwargs["on_assign"]

        partition = TopicPartition("raw-events-ingestion", 0)

        # Trigger on_assign callback
        on_assign(mock_consumer_inst, [partition])

        # Verify that auto-reset set offset to OFFSET_BEGINNING and assigned
        assert partition.offset == OFFSET_BEGINNING
        mock_consumer_inst.assign.assert_called_once_with([partition])


def test_process_valid_message_commits_offset(caplog):
    with patch("src.consumer.event_consumer.Consumer") as mock_consumer_cls, \
         patch("src.consumer.event_consumer.init_db_schema"), \
         patch("src.consumer.event_consumer.upsert_city_event") as mock_upsert:

        mock_consumer_inst = MagicMock()
        mock_consumer_cls.return_value = mock_consumer_inst
        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_db_pool.connection.return_value.__enter__.return_value = mock_conn

        consumer = EventKafkaConsumer(
            kafka_config={"bootstrap.servers": "localhost:9092", "group.id": "test-group", "enable.auto.commit": False},
            topic="raw-events-ingestion",
            db_pool=mock_db_pool,
        )

        future_date = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        payload = {
            "event_id": "eb_consumer_123",
            "city": "Vancouver, BC, Canada",
            "title": "Science World Family Fair",
            "source": "Eventbrite",
            "url": "https://eventbrite.ca/e/science-world-123",
            "start_date": future_date,
            "status": "live",
            "is_canceled": False,
        }

        mock_msg = MagicMock()
        mock_msg.error.return_value = None
        mock_msg.value.return_value = json.dumps(payload).encode("utf-8")
        mock_msg.key.return_value = b"eb_consumer_123"
        mock_msg.topic.return_value = "raw-events-ingestion"
        mock_msg.partition.return_value = 0
        mock_msg.offset.return_value = 100

        with caplog.at_level(logging.INFO):
            result = consumer.process_message(mock_msg)

        assert result is True
        mock_upsert.assert_called_once()
        # Verify verbose debug metrics log appeared in telemetry
        assert "[CONSUMER DEBUG] Received message: topic=raw-events-ingestion partition=0 offset=100 key=eb_consumer_123" in caplog.text
        # Ensure manual commit was executed with exact message
        mock_consumer_inst.commit.assert_called_once_with(message=mock_msg, asynchronous=True)


def test_process_poison_pill_commits_offset():
    with patch("src.consumer.event_consumer.Consumer") as mock_consumer_cls, \
         patch("src.consumer.event_consumer.init_db_schema"), \
         patch("src.consumer.event_consumer.upsert_city_event") as mock_upsert:

        mock_consumer_inst = MagicMock()
        mock_consumer_cls.return_value = mock_consumer_inst
        mock_db_pool = MagicMock()

        consumer = EventKafkaConsumer(
            kafka_config={"bootstrap.servers": "localhost:9092", "group.id": "test-group", "enable.auto.commit": False},
            topic="raw-events-ingestion",
            db_pool=mock_db_pool,
        )

        # Corrupted JSON / invalid schema message (poison pill)
        mock_msg = MagicMock()
        mock_msg.error.return_value = None
        mock_msg.value.return_value = b"{corrupt_json_garbage: 123"
        mock_msg.key.return_value = None
        mock_msg.topic.return_value = "raw-events-ingestion"
        mock_msg.partition.return_value = 0
        mock_msg.offset.return_value = 101

        result = consumer.process_message(mock_msg)

        assert result is True
        # Ensure DB upsert was NOT called
        mock_upsert.assert_not_called()
        # Ensure offset was committed so consumer is not blocked
        mock_consumer_inst.commit.assert_called_once_with(message=mock_msg, asynchronous=True)


def test_database_error_skips_commit():
    with patch("src.consumer.event_consumer.Consumer") as mock_consumer_cls, \
         patch("src.consumer.event_consumer.init_db_schema"), \
         patch("src.consumer.event_consumer.upsert_city_event", side_effect=Exception("DB Connection Timeout")):

        mock_consumer_inst = MagicMock()
        mock_consumer_cls.return_value = mock_consumer_inst
        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_db_pool.connection.return_value.__enter__.return_value = mock_conn

        consumer = EventKafkaConsumer(
            kafka_config={"bootstrap.servers": "localhost:9092", "group.id": "test-group", "enable.auto.commit": False},
            topic="raw-events-ingestion",
            db_pool=mock_db_pool,
        )

        future_date = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        payload = {
            "event_id": "eb_consumer_124",
            "city": "Vancouver, BC, Canada",
            "title": "Stanley Park Walk",
            "url": "https://eventbrite.ca/e/stanley-park-124",
            "start_date": future_date,
        }

        mock_msg = MagicMock()
        mock_msg.error.return_value = None
        mock_msg.value.return_value = json.dumps(payload).encode("utf-8")
        mock_msg.key.return_value = None
        mock_msg.topic.return_value = "raw-events-ingestion"
        mock_msg.partition.return_value = 0
        mock_msg.offset.return_value = 102

        result = consumer.process_message(mock_msg)

        # Should return False indicating retry needed
        assert result is False
        # Offset must NOT be committed on DB failure
        mock_consumer_inst.commit.assert_not_called()


def test_consumer_close():
    with patch("src.consumer.event_consumer.Consumer") as mock_consumer_cls, \
         patch("src.consumer.event_consumer.init_db_schema"):

        mock_consumer_inst = MagicMock()
        mock_consumer_cls.return_value = mock_consumer_inst
        mock_db_pool = MagicMock()

        consumer = EventKafkaConsumer(
            kafka_config={"bootstrap.servers": "localhost:9092", "group.id": "test-group", "enable.auto.commit": False},
            topic="raw-events-ingestion",
            db_pool=mock_db_pool,
        )

        consumer.close()

        mock_consumer_inst.close.assert_called_once()
        mock_db_pool.close.assert_called_once()
