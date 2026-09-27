from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.etl.facebook_snippets import SOURCE_LABEL, FacebookSnippetSource, facebook_event_id
from tests.web_fakes import FakeBrave, FakeExtractor, FakeScreener


def _event(hour_local_offset=16):
    start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=23, minute=0, second=0, microsecond=0)
    return {"title": "Family Halloween Parade", "start_date": start, "end_date": None,
            "location_summary": "Lonsdale Quay, North Vancouver", "description": "A costume parade for families.",
            "event_url": None, "confidence": 0.9}


def _source(extractor=None, screener=None):
    return FacebookSnippetSource("North Vancouver, BC, Canada", search=FakeBrave("brave_facebook.json"),
                                 screener=screener or FakeScreener(), extractor=extractor or FakeExtractor([_event()]),
                                 queries=['site:facebook.com/events "{city}" family'])


def test_parses_event_ids():
    assert facebook_event_id("https://www.facebook.com/events/1234567890123/") == "1234567890123"
    assert facebook_event_id("https://m.facebook.com/events/555/") == "555"
    assert facebook_event_id("https://www.facebook.com/somegroup/posts/99") is None


def test_uses_only_search_results_and_never_requests_facebook():
    with patch("requests.Session.get") as http_get, patch("requests.get") as plain_get:
        src = _source()
        events = src.normalize_data(src.fetch_raw_events())
    http_get.assert_not_called()
    plain_get.assert_not_called()
    assert src.search.queries == ['site:facebook.com/events "North Vancouver" family']
    assert [e["event_id"] for e in events] == ["fbsnip_1234567890123", "fbsnip_555"]


def test_stored_fields_have_no_images_no_names_and_neutral_summary():
    src = _source()
    event = src.normalize_data(src.fetch_raw_events())[0]
    assert event["source"] == SOURCE_LABEL and event["origin"] == "facebook_snippet"
    assert event["pictureurl"] is None
    assert event["url"] == "https://www.facebook.com/events/1234567890123/"
    assert event["description"] == "A costume parade for families."
    assert "Hosted by" not in (event["description"] or "")


def test_snippet_text_is_what_gets_screened_and_extracted():
    extractor, screener = FakeExtractor([_event()]), FakeScreener()
    _source(extractor, screener).fetch_raw_events()
    first = extractor.calls[0]["text"]
    assert "Lonsdale Quay" in first and "<strong>" not in first
    assert extractor.calls[0]["source_url"] is None


def test_events_without_a_time_are_skipped():
    midnight_local = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0) + timedelta(days=3)
    src = _source(extractor=FakeExtractor([dict(_event(), start_date=midnight_local)]))
    assert src.fetch_raw_events() == []
    assert src.metrics["skipped_no_time"] >= 1


def test_screened_out_snippets_are_not_extracted():
    extractor = FakeExtractor([_event()])
    src = _source(extractor, FakeScreener(default=0.1))
    assert src.fetch_raw_events() == [] and extractor.calls == []
