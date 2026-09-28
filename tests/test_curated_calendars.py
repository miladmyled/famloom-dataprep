from datetime import timezone

from src.etl.curated_calendars import CuratedCalendarSource, load_calendars, stable_event_id_and_url
from src.etl.transformer import clean_and_validate_event
from src.net.http import PoliteHttpClient
from tests.web_fakes import FakeExtractor, FakeResponse, FakeScreener, FakeSession, fixture, ics_fixture, public_resolver


def _source(calendars, routes, screener=None, extractor=None, city="Coquitlam, BC, Canada"):
    session = FakeSession(routes)
    http = PoliteHttpClient(session=session, min_interval_seconds=0, resolver=public_resolver, sleep=lambda s: None)
    return CuratedCalendarSource(city, http=http, screener=screener or FakeScreener(), extractor=extractor or FakeExtractor(),
                                 calendars=calendars), session


def _cal(url, kind, city="Coquitlam, BC, Canada", label="City of Coquitlam", enabled=True):
    return {"city": city, "name": label, "url": url, "kind": kind, "source_label": label, "enabled": enabled}


def test_ical_feed_parsed_with_local_time_zone_and_cancellation():
    url = "https://www.coquitlam.ca/cal.ics"
    source, _ = _source([_cal(url, "ical")], {url: FakeResponse(url, 200, ics_fixture(), {"Content-Type": "text/calendar"})})
    events = source.normalize_data(source.fetch_raw_events())
    by_title = {e["title"]: e for e in events}
    movie = by_title["Family Movie Night in the Park"]
    assert movie["start_date"].tzinfo == timezone.utc
    assert movie["start_date"].hour in (2, 3)  # 19:00 Vancouver = 02:00/03:00 UTC next day
    assert movie["location_summary"].startswith("Town Centre Park")
    assert movie["url"] == "https://www.coquitlam.ca/Calendar.aspx?EID=101"
    assert movie["source"] == "City of Coquitlam" and movie["origin"] == "curated"
    assert movie["event_id"].startswith("city_of_coquitlam_")
    assert movie["pictureurl"] is None
    assert by_title["Canceled Lantern Walk"]["is_canceled"] is True
    assert clean_and_validate_event(movie) is not None


def test_jsonld_page_used_without_ai():
    url = "https://harbour.example/events"
    screener, extractor = FakeScreener(), FakeExtractor()
    source, _ = _source([_cal(url, "html", city="Vancouver, BC, Canada", label="Harbour Science")],
                        {url: FakeResponse(url, 200, fixture("venue_jsonld.html"))}, screener, extractor, city="Vancouver, BC, Canada")
    events = source.normalize_data(source.fetch_raw_events())
    assert [e["title"] for e in events] == ["Toddler Science Morning"]  # online webinar skipped
    assert events[0]["url"] == "https://harbour.example/events/toddler-science"
    assert "1455 Quebec St" in events[0]["location_summary"]
    assert screener.calls == [] and extractor.calls == []


def test_html_page_screened_then_extracted_with_fragment_url():
    url = "https://riverside.example/whats-on"
    screener, extractor = FakeScreener(), FakeExtractor()
    source, _ = _source([_cal(url, "html", label="Riverside CC")], {url: FakeResponse(url, 200, fixture("venue_text.html"))}, screener, extractor)
    events = source.normalize_data(source.fetch_raw_events())
    assert len(screener.calls) == 1 and len(extractor.calls) == 1
    assert "pancake breakfast" in extractor.calls[0]["text"] and "Home | About" not in extractor.calls[0]["text"]
    assert events[0]["url"].startswith(url + "#fl-")
    assert events[0]["event_id"] == "riverside_cc_" + events[0]["url"].split("#fl-")[1]


def test_page_failing_screen_is_not_extracted():
    url = "https://riverside.example/whats-on"
    screener, extractor = FakeScreener(default=0.1), FakeExtractor()
    source, _ = _source([_cal(url, "html")], {url: FakeResponse(url, 200, fixture("venue_text.html"))}, screener, extractor)
    assert source.fetch_raw_events() == []
    assert extractor.calls == [] and source.metrics["screened_out"] == 1


def test_ids_are_stable_and_listing_urls_unique():
    a = stable_event_id_and_url("Riverside CC", "https://r.example/p", None, "2026-10-03T16:00:00+00:00", "Breakfast")
    b = stable_event_id_and_url("Riverside CC", "https://r.example/p", None, "2026-10-03T16:00:00+00:00", "Breakfast")
    c = stable_event_id_and_url("Riverside CC", "https://r.example/p", None, "2026-10-04T16:00:00+00:00", "Breakfast")
    assert a == b and a[1] != c[1]


def test_only_enabled_calendars_of_the_city_are_read():
    cals = [_cal("https://a.example/x", "ical"), _cal("https://b.example/x", "ical", enabled=False),
            _cal("https://c.example/x", "ical", city="Vancouver, BC, Canada")]
    source, _ = _source(cals, {})
    assert [c["url"] for c in source.calendars] == ["https://a.example/x"]


def test_failed_page_does_not_stop_other_calendars():
    ok = "https://www.coquitlam.ca/cal.ics"
    source, _ = _source([_cal("https://down.example/x", "html"), _cal(ok, "ical")],
                        {ok: FakeResponse(ok, 200, ics_fixture(), {"Content-Type": "text/calendar"})})
    assert len(source.fetch_raw_events()) == 2
    assert source.metrics["pages_failed"] == 1


def test_shipped_config_is_valid():
    cals = load_calendars()
    assert len(cals) >= 13
    for c in cals:
        assert {"city", "name", "url", "kind", "source_label", "enabled", "robots_checked", "terms_checked"} <= set(c)
        assert c["url"].startswith("https://")
    blocked = [c for c in cals if not c["enabled"]]
    assert any("bibliocommons" in c["url"] for c in blocked)
