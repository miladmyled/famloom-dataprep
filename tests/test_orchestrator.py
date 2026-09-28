from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from main import run_etl_pipeline
from src.classify.factory import ClassifierChain
from tests.conftest import FakeCache, FakeProvider, make_taxonomy


def _future():
    return (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scraper(city, normalized):
    inst = MagicMock()
    inst.city = city
    inst.fetch_raw_events.return_value = [{}] * len(normalized)
    inst.normalize_data.return_value = normalized
    return inst


def _run(eb_events, meetup_events, provider):
    producer = MagicMock()
    producer.publish_event.return_value = True
    producer.flush.return_value = 0
    producer.get_delivery_metrics.return_value = {"delivered": 0, "failed": 0, "buffered": 0}
    with patch("main.get_active_cities", return_value=["Vancouver, BC, Canada"]), \
         patch("main.get_db_pool", return_value=MagicMock()), \
         patch("main.get_active_taxonomy", return_value=make_taxonomy()), \
         patch("main.get_classifier", return_value=ClassifierChain([provider])), \
         patch("main.ClassificationCache", return_value=FakeCache()), \
         patch("main.EventKafkaProducer", return_value=producer), \
         patch("main.EventbriteScraper", return_value=_scraper("Vancouver, BC, Canada", eb_events)), \
         patch("main.MeetupExtractor", return_value=_scraper("Vancouver, BC, Canada", meetup_events)):
        exit_code = run_etl_pipeline()
    return exit_code, producer


def _ev(event_id, title, source="Eventbrite"):
    return {
        "event_id": event_id,
        "city": "Vancouver, BC, Canada",
        "title": title,
        "source": source,
        "url": f"https://example.com/{event_id}",
        "pictureurl": f"https://example.com/img/{event_id}.jpg",
        "start_date": _future(),
    }


def test_pipeline_publishes_only_accepted_events_with_tags():
    provider = FakeProvider(
        "jev",
        decisions={
            "eventbrite_1": {"decision": "accept", "interests": [45], "languages": [503]},
            "meetup_2": {"decision": "reject", "family": 0.1},
        },
    )
    exit_code, producer = _run(
        [_ev("eventbrite_1", "Family hike in Persian")],
        [_ev("meetup_2", "Wine bar social", source="Meetup")],
        provider,
    )

    assert exit_code == 0
    assert producer.publish_event.call_count == 1
    published = producer.publish_event.call_args[0][0]
    assert published.event_id == "eventbrite_1"
    assert published.tag_ids == [45, 503]
    assert published.replace_tags is True


def test_pipeline_does_not_publish_new_events_when_all_providers_fail():
    provider = FakeProvider("jev", fail={"eventbrite_1"})
    exit_code, producer = _run([_ev("eventbrite_1", "Toddler storytime")], [], provider)
    assert exit_code == 0
    producer.publish_event.assert_not_called()


def test_pipeline_deduplicates_same_url_across_sources():
    provider = FakeProvider("jev")
    same = _ev("eventbrite_1", "Toddler storytime")
    dup = dict(same, event_id="meetup_9", source="Meetup")
    exit_code, producer = _run([same], [dup], provider)
    assert exit_code == 0
    assert producer.publish_event.call_count == 1


def test_run_etl_pipeline_no_cities():
    with patch("main.get_active_cities", return_value=[]):
        assert run_etl_pipeline() == 0
