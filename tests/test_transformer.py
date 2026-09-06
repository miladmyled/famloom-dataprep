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


def test_match_interest_tags_whole_word_and_case_insensitive():
    from src.etl.transformer import match_interest_tags

    mapping = {
        "art": 1,
        "sports": 2,
        "soccer": 3,
        "martial arts": 4,
        "music": 5,
    }

    # Test 1: "art" must NOT match "party"
    matches_party = match_interest_tags("Kids Birthday Party at the Park", None, mapping)
    assert 1 not in matches_party, "'art' should NOT match inside 'party'"

    # Test 2: "art" matches exact whole word in title
    matches_art = match_interest_tags("Kids Art & Painting Workshop", None, mapping)
    assert 1 in matches_art

    # Test 3: Case-insensitive matching (uppercase / mixed)
    matches_case = match_interest_tags("YOUTH SOCCER LEAGUE", "Great SPORTS event", mapping)
    assert 2 in matches_case  # sports from description
    assert 3 in matches_case  # soccer from title

    # Test 4: Multi-word interest label
    matches_multi = match_interest_tags("After School Martial Arts Academy", None, mapping)
    assert 4 in matches_multi

    # Test 5: Empty text or empty mapping
    assert match_interest_tags("", "", mapping) == []
    assert match_interest_tags("Art and Sports", None, {}) == []


def test_clean_and_validate_event_with_interest_tags():
    future_date = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    raw = {
        "event_id": "eb_tagged_1",
        "city": "Vancouver, BC",
        "title": "Community Youth Soccer Tournament",
        "description": "An exciting weekend of outdoor sports and music for families.",
        "url": "https://eventbrite.com/e/soccer-tournament",
        "start_date": future_date,
    }

    interest_mapping = {
        "soccer": 10,
        "sports": 20,
        "music": 30,
        "art": 40,
    }

    event = clean_and_validate_event(raw, interest_tags=interest_mapping)
    assert event is not None
    assert event.event_id == "eb_tagged_1"
    # Should match soccer, sports, music (10, 20, 30), but not art (40)
    assert 10 in event.tag_ids
    assert 20 in event.tag_ids
    assert 30 in event.tag_ids
    assert 40 not in event.tag_ids

