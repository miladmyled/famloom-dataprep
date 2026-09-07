from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytest
from src.etl.eventbrite import EventbriteScraper
from src.etl.base import BaseEventScraper


def test_base_scraper_inheritance():
    scraper = EventbriteScraper(city="Vancouver, BC", api_token="test_token")
    assert isinstance(scraper, BaseEventScraper)
    assert scraper.city == "Vancouver, BC"


def test_eventbrite_normalization():
    scraper = EventbriteScraper(city="Coquitlam, BC")
    future_utc = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_date_str = (datetime.now(timezone.utc) + timedelta(days=8)).strftime("%Y-%m-%d")

    raw_eventbrite_response = [
        {
            "id": "123456789",
            "name": {"text": "Junior Astronomy Night"},
            "url": "https://www.eventbrite.com/e/junior-astronomy-night-123456789",
            "start": {"utc": future_utc, "timezone": "America/Vancouver"},
            "end": {"utc": future_utc, "timezone": "America/Vancouver"},
            "description": {"text": "A fun night of stargazing for the whole family."},
            "venue": {
                "name": "Town Centre Park",
                "address": {"localized_address_display": "1299 Pinetree Way, Coquitlam"},
            },
            "status": "live",
            "is_cancelled": False,
        },
        {
            "id": "987654321",
            "name": "Canceled Kids Puppet Theatre",
            "url": "https://www.eventbrite.com/e/kids-puppet-987654321",
            "start_date": future_date_str,
            "start_time": "14:00",
            "timezone": "America/Vancouver",
            "status": "canceled",
            "is_cancelled": True,
        }
    ]

    normalized = scraper.normalize_data(raw_eventbrite_response)

    assert len(normalized) == 2

    # Verify event 1
    ev1 = normalized[0]
    assert ev1["event_id"] == "eventbrite_123456789"
    assert ev1["city"] == "Coquitlam, BC"
    assert ev1["title"] == "Junior Astronomy Night"
    assert ev1["source"] == "Eventbrite"
    assert ev1["start_date"] == future_utc
    assert ev1["location_summary"] == "Town Centre Park - 1299 Pinetree Way, Coquitlam"
    assert ev1["status"] == "live"
    assert ev1["is_canceled"] is False

    # Verify event 2 (canceled tombstone)
    ev2 = normalized[1]
    assert ev2["event_id"] == "eventbrite_987654321"
    assert ev2["status"] == "canceled"
    assert ev2["is_canceled"] is True


def test_pagination_circuit_breaker():
    scraper = EventbriteScraper(city="Vancouver, BC", api_token="valid_token", max_pages=2)

    # Mock responses where has_more_items is True indefinitely
    mock_page_data = {
        "events": {
            "results": [{"id": f"ev_{i}", "name": f"Event {i}", "url": f"https://eb.com/{i}"} for i in range(5)],
            "pagination": {"has_more_items": True, "page_count": 100},
        }
    }

    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_page_data
        mock_post.return_value = mock_response

        raw_events = scraper.fetch_raw_events()

        # Should only fetch 2 pages (10 events) because max_pages=2
        assert len(raw_events) == 10
        assert mock_post.call_count == 2


def test_rate_limit_retry_handling():
    scraper = EventbriteScraper(city="Surrey, BC", api_token="valid_token", max_pages=1)

    with patch("requests.post") as mock_post, patch("time.sleep") as mock_sleep:
        # First call: 429 Rate Limit with Retry-After header
        rate_limit_resp = MagicMock()
        rate_limit_resp.status_code = 429
        rate_limit_resp.headers = {"Retry-After": "2"}

        # Second call: 200 OK
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {
            "events": {
                "results": [{"id": "ev_retry", "name": "Post Retry Event", "url": "https://eb.com/1"}],
                "pagination": {"has_more_items": False, "page_count": 1},
            }
        }

        mock_post.side_effect = [rate_limit_resp, success_resp]

        events = scraper.fetch_raw_events()

        assert len(events) == 1
        assert events[0]["id"] == "ev_retry"
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once_with(2.0)


def test_pagination_iteration_until_blank_page():
    scraper = EventbriteScraper(city="Vancouver, BC", api_token="valid_token", max_pages=5)

    # Page 1: 2 items, continuation "tok_p2"
    page1_data = {
        "events": {
            "results": [{"id": "ev_1", "name": "Event 1", "url": "https://eb.com/1"}],
            "pagination": {"has_more_items": True, "page_count": 3, "continuation": "tok_p2"},
        }
    }
    # Page 2: 2 items, continuation "tok_p3"
    page2_data = {
        "events": {
            "results": [{"id": "ev_2", "name": "Event 2", "url": "https://eb.com/2"}],
            "pagination": {"has_more_items": True, "page_count": 3, "continuation": "tok_p3"},
        }
    }
    # Page 3: Blank/empty results
    page3_data = {
        "events": {
            "results": [],
            "pagination": {"has_more_items": False, "page_count": 3},
        }
    }

    with patch("requests.post") as mock_post:
        resp1 = MagicMock(status_code=200, json=lambda: page1_data)
        resp2 = MagicMock(status_code=200, json=lambda: page2_data)
        resp3 = MagicMock(status_code=200, json=lambda: page3_data)
        mock_post.side_effect = [resp1, resp2, resp3]

        events = scraper.fetch_raw_events()

        assert len(events) == 2
        assert events[0]["id"] == "ev_1"
        assert events[1]["id"] == "ev_2"
        assert mock_post.call_count == 3

        # Verify page numbers and continuation tokens passed in payloads
        call1_payload = mock_post.call_args_list[0][1]["json"]["event_search"]
        assert call1_payload["page"] == 1
        assert "continuation" not in call1_payload

        call2_payload = mock_post.call_args_list[1][1]["json"]["event_search"]
        assert call2_payload["page"] == 2
        assert call2_payload["continuation"] == "tok_p2"

        call3_payload = mock_post.call_args_list[2][1]["json"]["event_search"]
        assert call3_payload["page"] == 3
        assert call3_payload["continuation"] == "tok_p3"


def test_pagination_cycle_detection_circuit_breaker():
    scraper = EventbriteScraper(city="Richmond, BC", api_token="valid_token", max_pages=10)

    # API loops back with duplicate continuation token
    loop_page_data = {
        "events": {
            "results": [{"id": "ev_loop", "name": "Loop Event", "url": "https://eb.com/loop"}],
            "pagination": {"has_more_items": True, "page_count": 100, "continuation": "repeat_token"},
        }
    }

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock(status_code=200, json=lambda: loop_page_data)
        mock_post.return_value = mock_resp

        events = scraper.fetch_raw_events()

        # Should halt on duplicate continuation token on page 3 (making only 2 calls, not looping to 10)
        assert mock_post.call_count == 2
        assert len(events) == 2


def test_source_date_filtering_parameters():
    scraper = EventbriteScraper(city="Vancouver, BC", api_token="valid_token", max_pages=1)

    mock_resp_data = {
        "events": {
            "results": [{"id": "ev_date_check", "name": "Date Check", "url": "https://eb.com/date"}],
            "pagination": {"has_more_items": False, "page_count": 1},
        }
    }

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, json=lambda: mock_resp_data)

        scraper.fetch_raw_events()

        payload = mock_post.call_args[1]["json"]["event_search"]
        assert "start_date.range_start" in payload
        assert "date_range" in payload
        assert payload["dates"] == "current_future"
        assert payload["start_date.range_start"].endswith("T00:00:00Z")


def test_multi_city_dynamic_support():
    scraper = EventbriteScraper(cities=["Vancouver, BC", "Burnaby, BC"], api_token="valid_token", max_pages=1)

    vanc_data = {
        "events": {
            "results": [{"id": "vanc_1", "name": "Vancouver Event", "url": "https://eb.com/vanc"}],
            "pagination": {"has_more_items": False, "page_count": 1},
        }
    }
    burn_data = {
        "events": {
            "results": [{"id": "burn_1", "name": "Burnaby Event", "url": "https://eb.com/burn"}],
            "pagination": {"has_more_items": False, "page_count": 1},
        }
    }

    with patch("requests.post") as mock_post:
        resp_vanc = MagicMock(status_code=200, json=lambda: vanc_data)
        resp_burn = MagicMock(status_code=200, json=lambda: burn_data)
        mock_post.side_effect = [resp_vanc, resp_burn]

        raw_events = scraper.fetch_raw_events()

        assert len(raw_events) == 2
        assert mock_post.call_count == 2
        assert raw_events[0]["_city"] == "Vancouver, BC"
        assert raw_events[1]["_city"] == "Burnaby, BC"

        normalized = scraper.normalize_data(raw_events)
        assert normalized[0]["city"] == "Vancouver, BC"
        assert normalized[1]["city"] == "Burnaby, BC"

