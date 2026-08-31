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
