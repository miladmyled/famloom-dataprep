from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from main import run_etl_pipeline


def test_run_etl_pipeline_success():
    future_utc = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_raw_events = [
        {
            "id": "1001",
            "name": {"text": "Family Kayaking"},
            "url": "https://eventbrite.com/e/kayak-1001",
            "start": {"utc": future_utc},
        }
    ]

    with patch("main.get_active_cities", return_value=["Vancouver, BC"]), \
         patch("main.EventKafkaProducer") as mock_producer_cls, \
         patch("main.EventbriteScraper") as mock_scraper_cls:

        mock_producer_instance = MagicMock()
        mock_producer_instance.publish_event.return_value = True
        mock_producer_instance.flush.return_value = 0
        mock_producer_instance.get_delivery_metrics.return_value = {"delivered": 1, "failed": 0, "buffered": 0}
        mock_producer_cls.return_value = mock_producer_instance

        mock_scraper_instance = MagicMock()
        mock_scraper_instance.fetch_raw_events.return_value = mock_raw_events
        mock_scraper_instance.normalize_data.return_value = [
            {
                "event_id": "eventbrite_1001",
                "city": "Vancouver, BC",
                "title": "Family Kayaking",
                "source": "Eventbrite",
                "url": "https://eventbrite.com/e/kayak-1001",
                "start_date": future_utc,
            }
        ]
        mock_scraper_cls.return_value = mock_scraper_instance

        exit_code = run_etl_pipeline()

        assert exit_code == 0
        mock_scraper_instance.fetch_raw_events.assert_called_once()
        mock_scraper_instance.normalize_data.assert_called_once_with(mock_raw_events)
        mock_producer_instance.publish_event.assert_called_once()
        # Verify that producer.flush was called (both incremental batch flush and final guaranteed flush)
        assert mock_producer_instance.flush.called
        assert mock_producer_instance.flush.call_count == 2


def test_run_etl_pipeline_no_cities():
    with patch("main.get_active_cities", return_value=[]):
        exit_code = run_etl_pipeline()
        assert exit_code == 0
