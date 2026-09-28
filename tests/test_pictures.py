from src.etl.enrich import enrich_events
from src.etl.web_extract import clean_image_url, jsonld_image, link_cards, match_card, normalize_jsonld_event
from src.net.http import PoliteHttpClient
from tests.web_fakes import FakeResponse, FakeSession, fixture, public_resolver

PAGE = "https://cards.example/events"


def _http(routes):
    session = FakeSession(routes)
    return PoliteHttpClient(session=session, min_interval_seconds=0, resolver=public_resolver, sleep=lambda s: None), session


def _ev(title, **kw):
    return dict({"title": title, "event_url": None, "picture": None}, **kw)


def test_clean_image_url_filters_non_pictures():
    base = "https://cards.example/events"
    assert clean_image_url("/img/pancakes.jpg", base) == "https://cards.example/img/pancakes.jpg"
    assert clean_image_url("/img/logo.png", base) is None
    assert clean_image_url("/icons/x.png", base) is None
    assert clean_image_url("/a/b.svg", base) is None
    assert clean_image_url("data:image/png;base64,AAAA", base) is None
    assert clean_image_url("http://insecure.example/a.jpg", base) is None
    assert clean_image_url("https://scontent.facebook.com/p.jpg", base) is None
    assert clean_image_url("/img/a.jpg 2x", base) == "https://cards.example/img/a.jpg"


def test_jsonld_image_variants():
    base = "https://h.example/e"
    assert jsonld_image({"image": "https://h.example/a.jpg"}, base) == "https://h.example/a.jpg"
    assert jsonld_image({"image": [{"@type": "ImageObject", "url": "/b.jpg"}]}, base) == "https://h.example/b.jpg"
    assert jsonld_image({"image": "/logo.png"}, base) is None
    node = {"@type": "Event", "name": "X", "startDate": "2026-10-03T10:00", "location": "Park", "image": "/c.jpg"}
    assert normalize_jsonld_event(node, base)["picture"] == "https://h.example/c.jpg"


def test_link_cards_and_title_matching():
    cards = link_cards(fixture("listing_cards.html"), PAGE)
    card = match_card("Family pancake breakfast!", cards)
    assert card["href"] == "https://cards.example/events/pancake-breakfast"
    assert card["image"] == "https://cards.example/img/pancakes.jpg"
    assert match_card("Completely different adult seminar", cards) is None
    assert all("logo" not in (c["image"] or "") for c in cards)


def test_enrich_uses_card_link_and_picture_then_detail_page():
    http, session = _http({"https://cards.example/events/pottery": FakeResponse("https://cards.example/events/pottery", 200, fixture("detail_og.html"))})
    events = [_ev("Family Pancake Breakfast"), _ev("Kids Pottery Drop-in"), _ev("Something not on the page")]
    enrich_events(events, fixture("listing_cards.html"), PAGE, http)
    pancake, pottery, other = events
    assert pancake["event_url"].endswith("/events/pancake-breakfast") and pancake["picture"].endswith("/img/pancakes.jpg")
    assert pottery["event_url"].endswith("/events/pottery") and pottery["picture"] == "https://cdn.cards.example/pottery-wheel.jpg"
    assert other["event_url"] is None and other["picture"] is None
    assert "https://cards.example/events/pancake-breakfast" not in session.requested  # card picture was enough


def test_enrich_keeps_structured_picture_and_caps_detail_fetches():
    http, session = _http({})
    events = [_ev("A", event_url="https://x.example/a", picture="https://x.example/a.jpg"),
              _ev("B", event_url="https://x.example/b"), _ev("C", event_url="https://x.example/c")]
    enrich_events(events, "", PAGE, http, max_detail_fetches=1)
    assert events[0]["picture"] == "https://x.example/a.jpg"
    detail_requests = [u for u in session.requested if not u.endswith("robots.txt")]
    assert detail_requests == ["https://x.example/b"]


def test_link_to_the_listing_itself_is_not_an_event_url():
    events = [_ev("Pancakes", event_url=PAGE)]
    enrich_events(events, "", PAGE, None)
    assert events[0]["event_url"] is None


def test_curated_html_events_get_pictures_end_to_end():
    from src.etl.curated_calendars import CuratedCalendarSource
    from tests.web_fakes import FakeExtractor, FakeScreener
    from datetime import datetime, timedelta, timezone

    extracted = [{"title": "Family Pancake Breakfast", "start_date": datetime.now(timezone.utc) + timedelta(days=2),
                  "end_date": None, "location_summary": "Gym", "description": None, "event_url": None, "confidence": 0.9}]
    http, _ = _http({PAGE: FakeResponse(PAGE, 200, fixture("listing_cards.html"))})
    cal = {"city": "Coquitlam, BC, Canada", "name": "Cards", "url": PAGE, "kind": "html", "source_label": "Cards", "enabled": True}
    src = CuratedCalendarSource("Coquitlam, BC, Canada", http=http, screener=FakeScreener(), extractor=FakeExtractor(extracted), calendars=[cal])
    event = src.normalize_data(src.fetch_raw_events())[0]
    assert event["url"] == "https://cards.example/events/pancake-breakfast"
    assert event["pictureurl"] == "https://cards.example/img/pancakes.jpg"
