import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from src.models.event import CityEvent
from src.etl.kafka_producer import EventKafkaProducer


def test_kafka_producer_publish_event():
    with patch("src.etl.kafka_producer.Producer") as mock_producer_cls:
        mock_producer_instance = MagicMock()
        mock_producer_cls.return_value = mock_producer_instance

        producer = EventKafkaProducer(
            config={"bootstrap.servers": "localhost:9092"},
            topic="raw-events-ingestion",
        )

        future_date = datetime.now(timezone.utc) + timedelta(days=2)
        event = CityEvent(
            event_id="eb_producer_test_1",
            city="Vancouver, BC",
            title="Family Puppet Show",
            url="https://eventbrite.com/e/puppet-show",
            start_date=future_date,
        )

        success = producer.publish_event(event)

        assert success is True
        assert mock_producer_instance.produce.called
        
        # Verify call arguments
        call_kwargs = mock_producer_instance.produce.call_args.kwargs
        assert call_kwargs["topic"] == "raw-events-ingestion"
        assert call_kwargs["key"] == b"eb_producer_test_1"

        payload_dict = json.loads(call_kwargs["value"].decode("utf-8"))
        assert payload_dict["event_id"] == "eb_producer_test_1"
        assert payload_dict["title"] == "Family Puppet Show"
        assert payload_dict["city"] == "Vancouver, BC"


def test_kafka_producer_delivery_report():
    # Test successful delivery callback
    mock_msg = MagicMock()
    mock_msg.topic.return_value = "raw-events-ingestion"
    mock_msg.partition.return_value = 0
    mock_msg.offset.return_value = 42
    mock_msg.key.return_value = b"eb_123"

    EventKafkaProducer.delivery_report(None, mock_msg)

    # Test error delivery callback
    mock_err = MagicMock()
    mock_err.__str__.return_value = "Broker: Message timed out"
    EventKafkaProducer.delivery_report(mock_err, mock_msg)


def test_kafka_producer_flush():
    with patch("src.etl.kafka_producer.Producer") as mock_producer_cls:
        mock_producer_instance = MagicMock()
        mock_producer_instance.flush.return_value = 0
        mock_producer_cls.return_value = mock_producer_instance

        producer = EventKafkaProducer(config={"bootstrap.servers": "localhost:9092"})
        remaining = producer.flush(timeout=5.0)

        assert remaining == 0
        mock_producer_instance.flush.assert_called_once_with(5.0)
