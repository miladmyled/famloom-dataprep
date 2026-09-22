from datetime import datetime, timezone, timedelta
import pytest
from pydantic import ValidationError
from src.models.event import CityEvent


def test_city_event_valid_future():
    future_date = datetime.now(timezone.utc) + timedelta(days=5)
    event = CityEvent(
        event_id="eventbrite_999",
        city="Vancouver, BC",
        title="Family Science Fair",
        url="https://eventbrite.com/e/family-science-fair-999",
        start_date=future_date,
    )
    assert event.event_id == "eventbrite_999"
    assert event.city == "Vancouver, BC"
    assert event.title == "Family Science Fair"
    assert event.source == "Eventbrite"
    assert event.status == "live"
    assert event.is_canceled is False
    assert event.start_date == future_date
    assert event.date == future_date


def test_city_event_13_days_future_passes():
    """Assert that an event 13 days in the future passes validation within the 14-day window."""
    future_13_days = datetime.now(timezone.utc) + timedelta(days=13)
    event = CityEvent(
        event_id="eventbrite_13days",
        city="Vancouver, BC",
        title="13-Day Future Event",
        url="https://eventbrite.com/e/future-13",
        start_date=future_13_days,
    )
    assert event.start_date.date() == future_13_days.date()


def test_city_event_15_days_future_rejected():
    """Assert that an event 15 days in the future is successfully rejected outside the 14-day window."""
    future_15_days = datetime.now(timezone.utc) + timedelta(days=15)
    with pytest.raises(ValidationError) as exc_info:
        CityEvent(
            event_id="eventbrite_15days",
            city="Vancouver, BC",
            title="15-Day Future Event",
            url="https://eventbrite.com/e/future-15",
            start_date=future_15_days,
        )
    err_msg = str(exc_info.value)
    assert "14-day ingestion window" in err_msg or "strictly greater than" in err_msg


def test_city_event_iso_string_conversion():
    future_date_iso = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = CityEvent(
        event_id="eventbrite_888",
        city="Coquitlam, BC",
        title="Outdoor Community Picnic",
        url="https://eventbrite.com/e/picnic-888",
        start_date=future_date_iso,
    )
    assert event.start_date.tzinfo is not None
    assert event.start_date.tzinfo == timezone.utc


def test_city_event_today_event_survives():
    # An event scheduled for today should survive
    today_now = datetime.now(timezone.utc)
    event = CityEvent(
        event_id="eventbrite_today",
        city="Burnaby, BC",
        title="Today's Puppet Show",
        url="https://eventbrite.com/e/puppet-show",
        start_date=today_now,
    )
    assert event.event_id == "eventbrite_today"
    assert event.start_date.date() == today_now.date()


def test_city_event_past_date_rejected():
    past_date = datetime.now(timezone.utc) - timedelta(days=2)
    with pytest.raises(ValidationError) as exc_info:
        CityEvent(
            event_id="eventbrite_old",
            city="Surrey, BC",
            title="Past Music Camp",
            url="https://eventbrite.com/e/music-camp-1",
            start_date=past_date,
        )
    assert "is strictly before CURRENT_DATE" in str(exc_info.value)


def test_city_event_invalid_url():
    future_date = datetime.now(timezone.utc) + timedelta(days=1)
    with pytest.raises(ValidationError):
        CityEvent(
            event_id="eventbrite_bad_url",
            city="Richmond, BC",
            title="Kids Play Day",
            url="not-a-valid-http-url",
            start_date=future_date,
        )


def test_city_event_title_max_length():
    future_date = datetime.now(timezone.utc) + timedelta(days=1)
    long_title = "A" * 241
    with pytest.raises(ValidationError):
        CityEvent(
            event_id="eventbrite_long_title",
            city="Richmond, BC",
            title=long_title,
            url="https://eventbrite.com/e/long-title",
            start_date=future_date,
        )


def test_city_event_tombstone_canceled():
    future_date = datetime.now(timezone.utc) + timedelta(days=10)
    event = CityEvent(
        event_id="eventbrite_canceled_1",
        city="Vancouver, BC",
        title="Canceled Park Festival",
        url="https://eventbrite.com/e/canceled-festival",
        start_date=future_date,
        status="canceled",
        is_canceled=True,
    )
    assert event.is_canceled is True
    assert event.status == "canceled"


def test_city_event_tag_ids_default():
    future_date = datetime.now(timezone.utc) + timedelta(days=5)
    event = CityEvent(
        event_id="eventbrite_notags",
        city="Vancouver, BC",
        title="Family Gathering",
        url="https://eventbrite.com/e/gathering",
        start_date=future_date,
    )
    assert event.tag_ids == []


def test_city_event_with_tag_ids_serialization():
    future_date = datetime.now(timezone.utc) + timedelta(days=5)
    event = CityEvent(
        event_id="eventbrite_withtags",
        city="Vancouver, BC",
        title="Kids Soccer and Art Camp",
        url="https://eventbrite.com/e/soccer-art",
        start_date=future_date,
        tag_ids=[101, 102, 103],
    )
    assert event.tag_ids == [101, 102, 103]

    # JSON serialization round-trip
    dumped_json = event.model_dump_json()
    reloaded = CityEvent.model_validate_json(dumped_json)
    assert reloaded.tag_ids == [101, 102, 103]


def test_city_event_timezone_leeway_few_hours_difference():
    """
    Asserts that an event occurring a few hours before current UTC date
    (e.g., in a western timezone such as PDT UTC-7 where local time is still today
    or within the 14-hour grace window) passes validation cleanly without false poison pill drops.
    """
    leeway_date = datetime.now(timezone.utc) - timedelta(hours=6)
    event = CityEvent(
        event_id="eventbrite_tz_leeway",
        city="Vancouver, BC",
        title="Evening Family Concert",
        url="https://eventbrite.com/e/concert-tz",
        start_date=leeway_date,
    )
    assert event.event_id == "eventbrite_tz_leeway"
    assert event.start_date.tzinfo == timezone.utc


def test_city_event_pictureurl():
    future_date = datetime.now(timezone.utc) + timedelta(days=3)
    event = CityEvent(
        event_id="event_pic_1",
        city="Vancouver, BC",
        title="Art Festival",
        url="https://eventbrite.com/e/art-fest",
        start_date=future_date,
        pictureurl="https://img.evbuc.com/images/123/original.jpg",
    )
    assert event.pictureurl == "https://img.evbuc.com/images/123/original.jpg"

    # Test alias mapping (picture_url, image_url)
    event_alias = CityEvent(
        event_id="event_pic_2",
        city="Vancouver, BC",
        title="Art Festival 2",
        url="https://eventbrite.com/e/art-fest-2",
        start_date=future_date,
        picture_url="https://secure.meetupstatic.com/photos/highres_123.jpeg",
    )
    assert event_alias.pictureurl == "https://secure.meetupstatic.com/photos/highres_123.jpeg"

    # Test invalid URL falls back to None
    event_invalid = CityEvent(
        event_id="event_pic_3",
        city="Vancouver, BC",
        title="Art Festival 3",
        url="https://eventbrite.com/e/art-fest-3",
        start_date=future_date,
        pictureurl="not-a-valid-url",
    )
    assert event_invalid.pictureurl is None



