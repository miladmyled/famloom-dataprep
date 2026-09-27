from datetime import timedelta

from src.etl.dedupe import ExistingEvent, dedupe_events
from tests.conftest import make_event


def _ev(event_id, title, origin, minutes=0, city="Vancouver, BC, Canada", source=None):
    e = make_event(event_id, title=title, city=city, source=source or origin, origin=origin)
    return e.model_copy(update={"start_date": e.start_date.replace(microsecond=0, second=0, minute=0) + timedelta(minutes=minutes)})


def test_same_event_from_lower_priority_source_is_dropped():
    events = [
        _ev("web_1", "Toddler Science Morning!", "web", minutes=15),
        _ev("curated_1", "Toddler Science Morning", "curated"),
        _ev("fbsnip_1", "toddler science morning at the centre", "facebook_snippet", minutes=-20),
    ]
    kept, dups = dedupe_events(events)
    assert [e.event_id for e in kept] == ["curated_1"]
    assert dups == {"web": 1, "facebook_snippet": 1}


def test_different_time_or_city_or_title_is_kept():
    events = [
        _ev("a", "Toddler Science Morning", "curated"),
        _ev("b", "Toddler Science Morning", "web", minutes=45),
        _ev("c", "Toddler Science Morning", "web", city="Coquitlam, BC, Canada"),
        _ev("d", "Adult Pottery Night", "web"),
    ]
    kept, _ = dedupe_events(events)
    assert {e.event_id for e in kept} == {"a", "b", "c", "d"}


def test_same_url_is_a_duplicate():
    a = _ev("eventbrite_1", "Storytime", "eventbrite")
    b = _ev("meetup_1", "Different title", "meetup").model_copy(update={"url": a.url})
    kept, dups = dedupe_events([a, b])
    assert len(kept) == 1 and sum(dups.values()) == 1


def test_lower_priority_duplicate_of_published_event_is_not_published():
    new = _ev("web_1", "Family Pancake Breakfast", "web")
    existing = [ExistingEvent("https://eventbrite.ca/e/1", "Family pancake breakfast", new.start_date, "Vancouver, BC, Canada", "Eventbrite")]
    kept, dups = dedupe_events([new], existing)
    assert kept == [] and dups == {"web": 1}


def test_primary_sources_are_never_dropped_against_the_database():
    new = _ev("eventbrite_2", "Family Pancake Breakfast", "eventbrite")
    existing = [ExistingEvent("https://science.example/x", "Family pancake breakfast", new.start_date, "Vancouver, BC, Canada", "Science World")]
    kept, _ = dedupe_events([new], existing)
    assert [e.event_id for e in kept] == ["eventbrite_2"]


def test_republished_same_url_is_not_a_duplicate_of_itself():
    new = _ev("curated_1", "Storytime", "curated")
    existing = [ExistingEvent(str(new.url), "Storytime", new.start_date, "Vancouver, BC, Canada", "Library")]
    kept, _ = dedupe_events([new], existing)
    assert len(kept) == 1
