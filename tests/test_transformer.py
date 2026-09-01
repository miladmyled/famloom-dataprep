from datetime import datetime, timezone, timedelta
from src.etl.transformer import clean_and_validate_event


def test_clean_and_validate_good_event_within_14_days():
    # 7 days in future: passes
    future_date = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    raw = {
        "event_id": "eb_good_1",
        "city": "Vancouver, BC",
        "title": "Kids Science Workshop",
        "url": "https://eventbrite.com/e/science-workshop-1",
        "start_date": future_date,
    }

    event = clean_and_validate_event(raw)
    assert event is not None
    assert event.event_id == "eb_good_1"
    assert event.title == "Kids Science Workshop"


def test_clean_and_validate_13_days_passes():
    # 13 days in future: passes
    future_13 = (datetime.now(timezone.utc) + timedelta(days=13)).isoformat()
    raw = {
        "event_id": "eb_13_pass",
        "city": "Vancouver, BC",
        "title": "Upcoming Craft Fair (13 days)",
        "url": "https://eventbrite.com/e/craft-13",
        "start_date": future_13,
    }

    event = clean_and_validate_event(raw)
    assert event is not None
    assert event.event_id == "eb_13_pass"


def test_clean_and_validate_15_days_rejected():
    # 15 days in future: outside 14-day window -> rejected
    future_15 = (datetime.now(timezone.utc) + timedelta(days=15)).isoformat()
    raw = {
        "event_id": "eb_15_reject",
        "city": "Vancouver, BC",
        "title": "Far Future Music Festival (15 days)",
        "url": "https://eventbrite.com/e/music-15",
        "start_date": future_15,
    }

    event = clean_and_validate_event(raw)
    assert event is None


def test_clean_and_validate_virtual_event():
    future_date = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    raw = {
        "event_id": "eb_virt_1",
        "city": "Vancouver, BC",
        "title": "Virtual Storytime on Zoom",
        "url": "https://eventbrite.com/e/zoom-storytime",
        "start_date": future_date,
    }

    event = clean_and_validate_event(raw)
    assert event is None


def test_clean_and_validate_past_event():
    past_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    raw = {
        "event_id": "eb_past_1",
        "city": "Burnaby, BC",
        "title": "Past Magic Show",
        "url": "https://eventbrite.com/e/magic-show",
        "start_date": past_date,
    }

    event = clean_and_validate_event(raw)
    assert event is None


def test_clean_and_validate_malformed_event():
    raw = {
        "event_id": "eb_bad_1",
        "city": "Burnaby, BC",
        "title": "Malformed Event",
        "url": "not-a-valid-url",
    }

    event = clean_and_validate_event(raw)
    assert event is None
