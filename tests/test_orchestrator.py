from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from main import run_etl_pipeline


def test_run_etl_pipeline_success():
    future_utc = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_raw_eb_events = [
        {
            "id": "1001",
            "name": {"text": "Family Kayaking"},
            "url": "https://eventbrite.com/e/kayak-1001",
            "start": {"utc": future_utc},
        }
    ]

    mock_raw_meetup_events = [
        {
            "@type": "Event",
            "name": "Coquitlam Board Games",
            "url": "https://meetup.com/events/3001",
            "startDate": future_utc,
        }
    ]

    with patch("main.get_active_cities", return_value=["Vancouver, BC"]), \
         patch("main.EventKafkaProducer") as mock_producer_cls, \
         patch("main.EventbriteScraper") as mock_eb_scraper_cls, \
         patch("main.MeetupExtractor") as mock_meetup_scraper_cls:

        mock_producer_instance = MagicMock()
        mock_producer_instance.publish_event.return_value = True
        mock_producer_instance.flush.return_value = 0
        mock_producer_instance.get_delivery_metrics.return_value = {"delivered": 2, "failed": 0, "buffered": 0}
        mock_producer_cls.return_value = mock_producer_instance

        mock_eb_instance = MagicMock()
        mock_eb_instance.city = "Vancouver, BC"
        mock_eb_instance.fetch_raw_events.return_value = mock_raw_eb_events
        mock_eb_instance.normalize_data.return_value = [
            {
                "event_id": "eventbrite_1001",
                "city": "Vancouver, BC",
                "title": "Family Kayaking",
                "source": "Eventbrite",
                "url": "https://eventbrite.com/e/kayak-1001",
                "start_date": future_utc,
            }
        ]
        mock_eb_scraper_cls.return_value = mock_eb_instance

        mock_meetup_instance = MagicMock()
        mock_meetup_instance.city = "Coquitlam, BC"
        mock_meetup_instance.fetch_raw_events.return_value = mock_raw_meetup_events
        mock_meetup_instance.normalize_data.return_value = [
            {
                "event_id": "meetup_3001",
                "city": "Coquitlam, BC",
                "title": "Coquitlam Board Games",
                "source": "Meetup",
                "url": "https://meetup.com/events/3001",
                "start_date": future_utc,
            }
        ]
        mock_meetup_scraper_cls.return_value = mock_meetup_instance

        exit_code = run_etl_pipeline()

        assert exit_code == 0
        mock_eb_instance.fetch_raw_events.assert_called_once()
        mock_meetup_instance.fetch_raw_events.assert_called_once()
        assert mock_producer_instance.publish_event.call_count == 2
        assert mock_producer_instance.flush.called


def test_run_etl_pipeline_no_cities():
    with patch("main.get_active_cities", return_value=[]):
        exit_code = run_etl_pipeline()
        assert exit_code == 0


def test_run_etl_pipeline_with_interests():
    future_utc = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_raw_events = [
        {
            "id": "2001",
            "name": {"text": "Kids Soccer Clinic and Sports"},
            "url": "https://eventbrite.com/e/soccer-2001",
            "start": {"utc": future_utc},
        }
    ]

    with patch("main.get_active_cities", return_value=["Vancouver, BC"]), \
         patch("main.get_active_interests", return_value={"soccer": 77, "sports": 88}) as mock_interests, \
         patch("main.EventKafkaProducer") as mock_producer_cls, \
         patch("main.EventbriteScraper") as mock_eb_scraper_cls, \
         patch("main.MeetupExtractor") as mock_meetup_scraper_cls:

        mock_producer_instance = MagicMock()
        mock_producer_instance.publish_event.return_value = True
        mock_producer_instance.flush.return_value = 0
        mock_producer_instance.get_delivery_metrics.return_value = {"delivered": 1, "failed": 0, "buffered": 0}
        mock_producer_cls.return_value = mock_producer_instance

        mock_eb_instance = MagicMock()
        mock_eb_instance.city = "Vancouver, BC"
        mock_eb_instance.fetch_raw_events.return_value = mock_raw_events
        mock_eb_instance.normalize_data.return_value = [
            {
                "event_id": "eventbrite_2001",
                "city": "Vancouver, BC",
                "title": "Kids Soccer Clinic and Sports",
                "source": "Eventbrite",
                "url": "https://eventbrite.com/e/soccer-2001",
                "start_date": future_utc,
            }
        ]
        mock_eb_scraper_cls.return_value = mock_eb_instance

        mock_meetup_instance = MagicMock()
        mock_meetup_instance.city = "Coquitlam, BC"
        mock_meetup_instance.fetch_raw_events.return_value = []
        mock_meetup_instance.normalize_data.return_value = []
        mock_meetup_scraper_cls.return_value = mock_meetup_instance

        exit_code = run_etl_pipeline()

        assert exit_code == 0
        mock_interests.assert_called_once()
        mock_producer_instance.publish_event.assert_called_once()
        produced_event = mock_producer_instance.publish_event.call_args[0][0]
        # Assert interest tags were populated on produced event
        assert 77 in produced_event.tag_ids
        assert 88 in produced_event.tag_ids
