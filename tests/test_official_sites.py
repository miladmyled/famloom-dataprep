import json
from pathlib import Path
from unittest.mock import MagicMock

from src.etl.official_sites import OfficialSitesDiscoverySource, find_event_pages
from src.net.http import PoliteHttpClient
from src.net.wikidata import Venue, WikidataClient
from tests.test_web_search_discovery import MemoryStore
from tests.web_fakes import FakeExtractor, FakeResponse, FakeScreener, FakeSession, fixture, public_resolver

WD = Path(__file__).parent / "fixtures" / "wikidata"


def _wd_resp(name):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = json.loads((WD / name).read_text(encoding="utf-8"))
    return r


def _http(routes):
    session = FakeSession(routes)
    return PoliteHttpClient(session=session, min_interval_seconds=0, resolver=public_resolver, sleep=lambda s: None), session


def test_wikidata_client_queries_and_dedupes():
    session = MagicMock()
    session.get.side_effect = [_wd_resp("city.json"), _wd_resp("venues.json")]
    client = WikidataClient(session=session, min_interval_seconds=0, sleep=lambda s: None)
    sites = client.official_sites("North Vancouver, BC, Canada")
    assert [v.label for v in sites] == ["North Vancouver", "Harbour Science Centre", "Riverside Community Centre", "Science World", "Quiet Library"]
    first_query = session.get.call_args_list[0][1]["params"]["query"]
    assert '"North Vancouver"@en' in first_query and "wdt:P17 wd:Q16" in first_query
    assert "FamLoomBot" in session.get.call_args_list[0][1]["headers"]["User-Agent"]
    assert "wdt:P856" in session.get.call_args_list[1][1]["params"]["query"]


def test_find_event_pages_prefers_events_links_on_same_site():
    html = (
        '<nav><a href="/about">About</a><a href="/whats-on">What&#39;s On</a>'
        '<a href="https://other.example/events">Partner events</a><a href="/programs/kids">Kids programs</a></nav>'
    )
    pages = find_event_pages(html, "https://venue.example/")
    assert pages[0] == "https://venue.example/whats-on"
    assert all(p.startswith("https://venue.example/") for p in pages)


class FakeWikidata:
    def official_sites(self, city):
        return [
            Venue("Q9", "City Hall", "https://www.cityhall.example/", "city"),
            Venue("Q1", "Harbour Science Centre", "https://www.harbourscience.example/", "museum"),
            Venue("Q2", "Riverside Community Centre", "http://riverside.example", "community centre"),
            Venue("Q3", "Science World", "https://www.scienceworld.ca/", "museum"),
        ]


def test_official_sites_flow_finds_events_pages_and_remembers_with_wikidata_label():
    routes = {
        "https://www.cityhall.example/": FakeResponse("https://www.cityhall.example/", 200, "<main>Welcome</main>"),
        "https://www.harbourscience.example/": FakeResponse("https://www.harbourscience.example/", 200, '<a href="/events">Events</a>'),
        "https://www.harbourscience.example/events": FakeResponse("https://www.harbourscience.example/events", 200, fixture("venue_jsonld.html")),
        "https://www.harbourscience.example/terms-of-use": FakeResponse("https://www.harbourscience.example/terms-of-use", 200, "<main>ok</main>"),
        "https://riverside.example": FakeResponse("https://riverside.example/", 200, "<p>no links</p>"),
        "https://riverside.example/events": FakeResponse("https://riverside.example/events", 200, fixture("venue_text.html")),
        "https://riverside.example/legal/terms": FakeResponse("https://riverside.example/legal/terms", 200, "<main>ok</main>"),
    }
    http, session = _http(routes)
    store = MemoryStore()
    src = OfficialSitesDiscoverySource(
        "North Vancouver, BC, Canada", wikidata=FakeWikidata(), http=http, screener=FakeScreener(answers={"forbids": 0.05}),
        extractor=FakeExtractor(), store=store, blocked=set(),
        calendars=[{"url": "https://www.scienceworld.ca/events/", "enabled": False}],
    )
    events = src.normalize_data(src.fetch_raw_events())
    assert {r["url"] for r in store.remembered} == {"https://www.harbourscience.example/events", "https://riverside.example/events"}
    assert all(r["via"] == "wikidata" for r in store.remembered)
    assert {e["source"] for e in events} == {"Harbour Science Centre", "Riverside Community Centre"}
    assert all(e["origin"] == "official" and e["event_id"].startswith("site_") for e in events)
    assert src.metrics["skipped_blocked"] == 1           # Science World is disabled in the curated list
    assert src.metrics["no_events_page"] == 1            # City Hall: no events link and no /events page
    assert not any("scienceworld" in u for u in session.requested)


def test_remembered_official_sites_skip_wikidata_candidates_of_same_domain():
    store = MemoryStore(sites=[{"id": 1, "url": "https://www.harbourscience.example/events", "source_label": "Harbour",
                                "kind": "jsonld", "via": "wikidata"}])
    http, session = _http({"https://www.harbourscience.example/events": FakeResponse(
        "https://www.harbourscience.example/events", 200, fixture("venue_jsonld.html"))})

    class OneVenue:
        def official_sites(self, city):
            return [Venue("Q1", "Harbour Science Centre", "https://www.harbourscience.example/", "museum")]

    src = OfficialSitesDiscoverySource("North Vancouver, BC, Canada", wikidata=OneVenue(), http=http, screener=FakeScreener(),
                                       extractor=FakeExtractor(), store=store, blocked=set(), calendars=[])
    assert src.fetch_raw_events()
    assert store.successes == [1] and src.metrics["skipped_remembered"] == 1
    assert "https://www.harbourscience.example/" not in session.requested


def test_wikidata_failure_does_not_crash():
    class Broken:
        def official_sites(self, city):
            raise RuntimeError("down")

    src = OfficialSitesDiscoverySource("X, BC, Canada", wikidata=Broken(), http=MagicMock(), screener=FakeScreener(),
                                       extractor=FakeExtractor(), store=MemoryStore(), blocked=set(), calendars=[])
    assert src.fetch_raw_events() == []
