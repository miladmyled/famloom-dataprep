import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytest

from src.etl.base import BaseExtractor, BaseEventScraper
from src.etl.meetup_public import MeetupExtractor
from src.etl.transformer import clean_and_validate_event


def test_base_extractor_alias():
    assert BaseExtractor is BaseEventScraper
    extractor = MeetupExtractor(city="Coquitlam, BC")
    assert isinstance(extractor, BaseExtractor)
    assert isinstance(extractor, BaseEventScraper)


def test_meetup_extractor_init_defaults():
    extractor = MeetupExtractor()
    assert extractor.city == "Coquitlam, BC"
    assert "coquitlam" in extractor.target_url.lower()
    assert extractor.headless is True


def test_meetup_extractor_init_custom():
    extractor = MeetupExtractor(
        city="Vancouver, BC",
        target_url="https://www.meetup.com/find/?location=ca--bc--vancouver&source=EVENTS",
        headless=False,
        timeout_seconds=15,
    )
    assert extractor.city == "Vancouver, BC"
    assert extractor.target_url == "https://www.meetup.com/find/?location=ca--bc--vancouver&source=EVENTS"
    assert extractor.headless is False
    assert extractor.timeout_ms == 15000


def test_meetup_extractor_normalize_data_standard_event():
    future_time = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    raw_events = [
        {
            "@context": "https://schema.org",
            "@type": "Event",
            "name": "Kids Coding and Robotics Club",
            "url": "https://www.meetup.com/coquitlam-stem-kids/events/301234567/",
            "startDate": future_time,
            "endDate": (datetime.now(timezone.utc) + timedelta(days=3, hours=2)).isoformat(),
            "description": "Fun beginner coding robotics workshop for kids.",
            "eventStatus": "https://schema.org/EventScheduled",
            "location": {
                "@type": "Place",
                "name": "Coquitlam Public Library",
                "address": {
                    "@type": "PostalAddress",
                    "streetAddress": "1169 Pinetree Way",
                    "addressLocality": "Coquitlam",
                    "addressRegion": "BC",
                },
            },
        },
        {
            "@context": "https://schema.org",
            "@type": "Event",
            "name": "Canceled Outdoor Family Hike",
            "url": "https://www.meetup.com/coquitlam-hikers/events/309876543/",
            "startDate": future_time,
            "description": "Hike at Minnekhada Regional Park.",
            "eventStatus": "https://schema.org/EventCancelled",
            "location": "Minnekhada Regional Park, Coquitlam",
        },
    ]

    extractor = MeetupExtractor(city="Coquitlam, BC")
    normalized = extractor.normalize_data(raw_events)

    assert len(normalized) == 2

    # Event 1 Checks
    ev1 = normalized[0]
    assert ev1["event_id"] == "meetup_301234567"
    assert ev1["city"] == "Coquitlam, BC"
    assert ev1["title"] == "Kids Coding and Robotics Club"
    assert ev1["source"] == "Meetup"
    assert ev1["status"] == "live"
    assert ev1["is_canceled"] is False
    assert "Coquitlam Public Library" in ev1["location_summary"]
    assert "1169 Pinetree Way" in ev1["location_summary"]

    # Verify integration with transformer / CityEvent
    validated = clean_and_validate_event(ev1)
    assert validated is not None
    assert validated.event_id == "meetup_301234567"
    assert validated.title == "Kids Coding and Robotics Club"
    assert validated.is_canceled is False

    # Event 2 Checks
    ev2 = normalized[1]
    assert ev2["event_id"] == "meetup_309876543"
    assert ev2["status"] == "canceled"
    assert ev2["is_canceled"] is True
    assert ev2["location_summary"] == "Minnekhada Regional Park, Coquitlam"

    validated_canceled = clean_and_validate_event(ev2)
    assert validated_canceled is not None
    assert validated_canceled.is_canceled is True


def test_meetup_extractor_extract_events_from_payload():
    extractor = MeetupExtractor()

    # Test list payload with Event and non-Event
    list_payload = [
        {"@type": "Organization", "name": "Meetup"},
        {"@type": "Event", "name": "Family Fun Day", "url": "https://meetup.com/events/111"},
    ]
    extracted = []
    extractor._extract_events_from_payload(list_payload, extracted)
    assert len(extracted) == 1
    assert extracted[0]["name"] == "Family Fun Day"

    # Test ItemList payload
    itemlist_payload = {
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "item": {"@type": "Event", "name": "Storytime in the Park"}},
        ],
    }
    extracted_itemlist = []
    extractor._extract_events_from_payload(itemlist_payload, extracted_itemlist)
    assert len(extracted_itemlist) == 1
    assert extracted_itemlist[0]["name"] == "Storytime in the Park"

    # Test @graph payload
    graph_payload = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "Meetup"},
            {"@type": "Event", "name": "Parent & Toddler Playdate"},
        ],
    }
    extracted_graph = []
    extractor._extract_events_from_payload(graph_payload, extracted_graph)
    assert len(extracted_graph) == 1
    assert extracted_graph[0]["name"] == "Parent & Toddler Playdate"


def test_meetup_extractor_fetch_raw_events_mocked():
    future_time = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    mock_event_ldjson = json.dumps([
        {
            "@type": "Event",
            "name": "Coquitlam Family Board Games",
            "url": "https://www.meetup.com/coquitlam-games/events/305555555/",
            "startDate": future_time,
        }
    ])

    mock_elem = MagicMock()
    mock_elem.inner_text.return_value = mock_event_ldjson

    mock_page = MagicMock()
    mock_page.query_selector_all.return_value = [mock_elem]

    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page

    mock_browser = MagicMock()
    mock_browser.new_context.return_value = mock_context

    mock_playwright = MagicMock()
    mock_playwright.chromium.launch.return_value = mock_browser

    mock_sync_pw = MagicMock()
    mock_sync_pw.__enter__.return_value = mock_playwright

    with patch("playwright.sync_api.sync_playwright", return_value=mock_sync_pw):
        extractor = MeetupExtractor()
        raw_events = extractor.fetch_raw_events()

        assert len(raw_events) == 1
        assert raw_events[0]["name"] == "Coquitlam Family Board Games"
        mock_page.goto.assert_called_once_with(extractor.target_url, timeout=extractor.timeout_ms)
        mock_page.wait_for_load_state.assert_called_once_with("networkidle", timeout=extractor.timeout_ms)
        mock_browser.close.assert_called_once()
